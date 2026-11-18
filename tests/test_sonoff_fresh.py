import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'iot'))
from iot import IoTManager


class SonoffFreshTest(unittest.TestCase):
    def test_fresh_read_bypasses_both_manager_and_cloud_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = IoTManager(Path(directory) / 'data', Path(directory) / 'missing.json')
            module = {'id': '012345abcdef', 'driver': 'sonoff_ewelink', 'enabled': True}
            manager._module = Mock(return_value=module)
            manager.ewelink_adapter.status = Mock(return_value={'online': True, 'power': True})
            manager.state(module['id'])
            manager.state(module['id'])
            self.assertEqual(manager.ewelink_adapter.status.call_count, 1)
            manager.state(module['id'], fresh=True)
            self.assertEqual(manager.ewelink_adapter.status.call_count, 2)
            manager.ewelink_adapter.status.assert_called_with(module, force=True)


if __name__ == '__main__':
    unittest.main()
