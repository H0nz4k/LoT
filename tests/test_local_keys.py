import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "iot"))
from local_keys import KeyFileError, local_key
from iot_driver import IoTError, LocalTuya


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setter = load("key_setter", "tools/set-local-key.py")
installer = load("lan_installer", "tools/install-hub2-panel-lan.py")
verifier = load("lan_verifier", "tools/verify-local-lan.py")


class LocalKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name) / "keys"
        self.module = {"device_id": "fixture-panel-1", "local_key": "old-database-key"}
        self.env = patch.dict(os.environ, {"HANZHUB_TUYA_KEY_DIR": str(self.directory)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def save(self, key="0123456789abcdef"):
        setter.save_key(self.directory, self.module["device_id"], key)

    def test_changed_file_is_used_without_restart_or_database_reimport(self):
        self.save()
        self.assertEqual(local_key(self.module), "0123456789abcdef")
        self.save("fedcba9876543210")
        self.assertEqual(local_key(self.module), "fedcba9876543210")
        self.assertEqual(self.module["local_key"], "old-database-key")

    def test_missing_file_never_falls_back_to_database(self):
        with self.assertRaises(KeyFileError):
            local_key(self.module)

    def test_other_device_file_cannot_be_used(self):
        self.save()
        with self.assertRaises(KeyFileError):
            local_key({**self.module, "device_id": "different-panel"})

    def test_invalid_key_does_not_replace_existing_secret(self):
        self.save()
        with self.assertRaises(KeyFileError):
            self.save("short")
        self.assertEqual(local_key(self.module), "0123456789abcdef")

    def test_legacy_config_unchanged_when_directory_not_configured(self):
        with patch.dict(os.environ, {"HANZHUB_TUYA_KEY_DIR": ""}):
            self.assertEqual(local_key(self.module), "old-database-key")

    def test_invalid_id_is_rejected(self):
        with self.assertRaises(KeyFileError):
            local_key({**self.module, "device_id": "../another"})

    @unittest.skipIf(os.name == "nt", "POSIX mode enforcement")
    def test_private_file_and_directory_modes(self):
        self.save()
        path = self.directory / "fixture-panel-1.key"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        path.chmod(0o644)
        with self.assertRaises(KeyFileError):
            local_key(self.module)

    @unittest.skipIf(os.name == "nt", "POSIX symlink enforcement")
    def test_symlink_secret_is_rejected(self):
        self.save()
        path = self.directory / "fixture-panel-1.key"
        target = self.directory / "original.key"
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaises(KeyFileError):
            local_key(self.module)

    def test_missing_key_prevents_any_connection_and_does_not_leak_secret(self):
        called = []
        driver = LocalTuya(factory=lambda *a, **kw: called.append(a))
        with self.assertRaises(IoTError) as error:
            driver._connect({**self.module, "ip": "192.168.1.100", "version": "3.4"})
        self.assertEqual(error.exception.code, "local_key_unavailable")
        self.assertNotIn(self.module["local_key"], str(error.exception))
        self.assertEqual(called, [])

    def test_driver_passes_file_key_to_direct_lan_client(self):
        self.save()
        class Device:
            def __getattr__(self, name):
                return lambda value: None
        calls = []
        def factory(*args, **kwargs):
            calls.append((args, kwargs))
            return Device()
        LocalTuya(factory=factory)._connect({**self.module, "ip": "192.168.1.100", "version": "3.4"})
        self.assertEqual(calls[0][0], ("fixture-panel-1", "192.168.1.100", "0123456789abcdef"))
        self.assertEqual(calls[0][1], {"version": 3.4})

    def test_overlay_preserves_other_live_driver_changes_and_is_idempotent(self):
        source = (ROOT / "iot/iot_driver.py").read_text()
        legacy = source.replace("from local_keys import KeyFileError, local_key\n", "")
        new_block = '''        try:
            key = local_key(module)
        except KeyFileError as error:
            raise IoTError(str(error), 503, "local_key_unavailable") from None
        try:
            device = factory(module["device_id"], module["ip"], key,'''
        old_block = '''        try:
            device = factory(module["device_id"], module["ip"], module["local_key"],'''
        legacy = legacy.replace(new_block, old_block) + "\n# preserved-live-change\n"
        result = installer.patched_driver(legacy)
        self.assertIn("# preserved-live-change", result)
        self.assertIn("key = local_key(module)", result)
        self.assertEqual(installer.patched_driver(result), result)
        with self.assertRaises(ValueError):
            installer.patched_driver("# unknown runtime")

    def test_update_guard_detects_overwrite_and_changed_key_configuration(self):
        runtime = Path(self.tmp.name) / "driver.py"
        runtime.write_text("verified")
        manifest = Path(self.tmp.name) / "manifest.json"
        manifest.write_text(json.dumps({"key_directory": str(self.directory), "files": {
            str(runtime): hashlib.sha256(runtime.read_bytes()).hexdigest()}}))
        verifier.verify(manifest)
        runtime.write_text("old driver after update")
        with self.assertRaises(ValueError):
            verifier.verify(manifest)
        with patch.dict(os.environ, {"HANZHUB_TUYA_KEY_DIR": "/different"}):
            with self.assertRaises(ValueError):
                verifier.verify(manifest)


if __name__ == "__main__":
    unittest.main()
