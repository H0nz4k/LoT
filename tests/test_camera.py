"""C320WS registration, RTSP snapshot and secret isolation (offline fixtures)."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "iot"))
from iot import IoTManager
from iot_driver import IoTError
from camera_driver import LocalCamera


class FakeCamera:
    def status(self, module):
        return {"online": True, "model": "C320WS", "ip": module["ip"]}
    def snapshot(self, module):
        return b"\xff\xd8test\xff\xd9"


class CameraTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = IoTManager(data_dir=self.temp.name, devices_file=Path(self.temp.name)/"missing.json",
                                  camera_adapter=FakeCamera())
        self.config = {"name":"Zahrada", "driver":"tapo_c320ws", "ip":"192.168.1.45",
                       "tapo_username":"camera-user", "tapo_password":"secret-camera-password"}
    def tearDown(self):
        self.temp.cleanup()
    def test_camera_can_be_registered_without_cloud_and_read(self):
        module = self.manager.add_module(self.config)
        self.assertEqual(module["driver"], "tapo_c320ws")
        self.assertTrue(module["has_credentials"])
        self.assertEqual(self.manager.snapshot(module["id"]), b"\xff\xd8test\xff\xd9")
        self.assertNotIn("secret-camera-password", str(self.manager.list_modules()))
        self.assertNotIn("camera-user", str(self.manager.list_modules()))
        self.assertTrue(self.manager.state(module["id"], fresh=True)["online"])
    def test_camera_has_no_power_control_and_pause_disables_snapshot(self):
        module = self.manager.add_module(self.config)
        with self.assertRaises(IoTError):
            self.manager.command(module["id"], {"control":"power","value":True})
        self.manager.update_module(module["id"], {"enabled":False})
        with self.assertRaises(IoTError):
            self.manager.snapshot(module["id"])
    def test_camera_uses_private_ip_only(self):
        with self.assertRaises(IoTError):
            self.manager.add_module({**self.config, "ip":"8.8.8.8"})

class RTSPTests(unittest.TestCase):
    def test_ffmpeg_url_quotes_credentials_and_does_not_log_them(self):
        camera=LocalCamera()
        config={"ip":"192.168.1.45", "tapo_username":"camera@name",
                "tapo_password":"pass:w@rd"}
        with patch.object(camera, "status"), patch("camera_driver.subprocess.run",
              return_value=SimpleNamespace(returncode=0,stdout=b"\xff\xd8jpeg")) as run:
            self.assertEqual(camera.snapshot(config), b"\xff\xd8jpeg")
        argv=run.call_args.args[0]
        self.assertIn("camera%40name:pass%3Aw%40rd@", argv[argv.index("-i")+1])
        self.assertEqual(run.call_args.kwargs["stderr"], -3)  # subprocess.DEVNULL

if __name__ == "__main__":
    unittest.main()
