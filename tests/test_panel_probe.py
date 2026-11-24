import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("panel_probe", ROOT / "tools/probe-panel-dps.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeOutputTests(unittest.TestCase):
    def test_unknown_string_and_structured_values_are_not_disclosed(self):
        result = probe.public_dps({"dps": {"1": False, "3": 22, "100": "private-value", "101": {"token": "private"}}})
        self.assertEqual(result["1"], {"type": "bool", "value": False})
        self.assertEqual(result["3"], {"type": "int", "value": 22})
        self.assertNotIn("private", str(result))

    def test_error_payload_is_rejected_without_echoing_its_contents(self):
        for response in (None, {}, {"Err": "914", "Payload": "private-value"}):
            with self.subTest(response=response), self.assertRaises(ValueError) as error:
                probe.public_dps(response)
            self.assertNotIn("private-value", str(error.exception))


if __name__ == "__main__":
    unittest.main()
