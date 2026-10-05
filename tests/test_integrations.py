import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lcd'))
import infrapanel_widget as widget


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dashboard = load_file('dashboard_integration', ROOT / 'integrations' / 'dashboard.py')
lcd_installer = load_file('lcd_installer', ROOT / 'lcd' / 'install_lcd.py')


class CaptureDraw:
    def __init__(self):
        self.rows = []

    def text(self, point, label, **kwargs):
        self.rows.append((label, kwargs['fill']))

    def line(self, *args, **kwargs):
        pass


class LCDTests(unittest.TestCase):
    def render(self, state):
        draw = CaptureDraw()
        font = type('Font', (), {'size':17})()
        widget.draw_panel_section(draw, 10, 10, 320, font, font, state)
        return draw.rows

    def test_on_green_with_both_temperatures_including_zero(self):
        rows = self.render({'online':True, 'power':True, 'current_temp_c':0, 'target_temp_c':26})
        self.assertIn(('ON', widget.GREEN), rows)
        self.assertIn(('0 °C / 26 °C', widget.WHITE), rows)

    def test_off_red_and_unavailable_distinct(self):
        self.assertIn(('OFF', widget.RED), self.render({'online':True, 'power':False}))
        for state in [None, {'online':False, 'power':False}, {'online':True, 'power':True}]:
            rows = self.render(state)
            self.assertIn(('NEDOSTUPNY', widget.AMBER), rows)
            self.assertNotIn(('OFF', widget.RED), rows)

    def test_stale_reading_does_not_remain_on(self):
        poller = widget.PanelPoller.__new__(widget.PanelPoller)
        poller.lock = threading.Lock()
        poller.value = {'online':True, 'power':True}
        poller.interval = 5
        poller.received = time.monotonic() - 60
        self.assertIsNone(poller.snapshot())

    def test_lcd_install_creates_backup_and_preserves_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'lcd_info.py'
            original = '# original\nFBIOGET_VSCREENINFO = 1\ndef read_last_valid_meteo(): pass\n'
            script.write_text(original)
            script.chmod(0o755)
            backup = lcd_installer.install_files(script)
            self.assertEqual((backup / 'lcd_info.py').read_text(), original)
            self.assertIn('PanelPoller', script.read_text())
            self.assertTrue((script.parent / 'infrapanel_widget.py').is_file())
            self.assertEqual(script.stat().st_mode & 0o777, 0o755)

    def test_actual_systemd_execstart_script_detection(self):
        result = type('Result', (), {'returncode':0, 'stdout':'{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 /opt/lcd/lcd_info.py --lora_www /opt/lora2/www ; }'})()
        with patch.object(lcd_installer.subprocess, 'run', return_value=result):
            self.assertEqual(lcd_installer.detect_script('lcd-info.service'), Path('/opt/lcd/lcd_info.py'))


class DashboardTests(unittest.TestCase):
    def test_card_created_concurrently_by_container_is_preserved(self):
        responses = [io.BytesIO(json.dumps({'services':[]}).encode()),
                     HTTPError('http://localhost/api/services', 400, 'exists', {}, None),
                     io.BytesIO(json.dumps({'services':[{'key':'iot','url':'http://custom:4011'}]}).encode())]
        with patch.object(dashboard, 'urlopen', side_effect=responses):
            self.assertFalse(dashboard.register_card('http://localhost:4010', 'http://192.168.1.3:4011', 4011))

    def test_idempotent_nav_preserves_existing_settings_and_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'site'
            directory.mkdir()
            original = '<style>.original{color:red}</style><div class="top-bar"><span class="top-bar-spacer"></span><a href="settings.html">Settings</a></div>'
            (directory / 'index.html').write_text(original)
            (directory / 'services.json').write_text('{"services":[{"key":"lora"}]}')
            backup = dashboard.install_navigation(directory)
            changed = (directory / 'index.html').read_text()
            self.assertEqual(backup.read_text(), original)
            self.assertIn('settings.html', changed)
            self.assertIn('.original{color:red}', changed)
            self.assertIn('iot.html', changed)
            self.assertEqual(dashboard.install_navigation(directory), None)
            self.assertEqual((directory / 'index.html').read_text(), changed)
            self.assertEqual((directory / 'services.json').read_text(), '{"services":[{"key":"lora"}]}')
            self.assertTrue((directory / 'icons' / 'iot.svg').is_file())

    def test_unknown_dashboard_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'index.html'
            script.write_text('<h1>Different project</h1>')
            with self.assertRaises(ValueError):
                dashboard.install_navigation(Path(tmp))
            self.assertEqual(script.read_text(), '<h1>Different project</h1>')


if __name__ == '__main__':
    unittest.main()
