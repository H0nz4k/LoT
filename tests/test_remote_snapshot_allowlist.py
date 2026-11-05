"""Remote allowlist must include camera snapshots (img tags have no custom headers)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "iot"))
from access import remote_route_allowed


class RemoteAllowlistTests(unittest.TestCase):
    def test_snapshot_is_allowed_remotely_like_state(self):
        path = "/api/iot/devices/238040bf6c4e/snapshot"
        self.assertTrue(remote_route_allowed("GET", path))
        self.assertTrue(remote_route_allowed("GET", "/api/iot/devices/238040bf6c4e/state"))
        self.assertFalse(remote_route_allowed("GET", "/api/iot/devices/238040bf6c4e"))
        self.assertFalse(remote_route_allowed("POST", path))
        self.assertFalse(remote_route_allowed("DELETE", "/api/iot/devices/238040bf6c4e"))


if __name__ == "__main__":
    unittest.main()
