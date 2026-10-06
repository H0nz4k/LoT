import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'iot'))
from iot import IoTManager, PANEL_MAC
from iot_driver import IoTError, LocalTuya
from server import make_server

# Výslovně testovací hodnota; nepoužívá se v produkční konfiguraci.
TEST_KEY = '0123456789abcdef'


class FakeDevice:
    def __init__(self):
        self.dps = {'1': True, '2': False, '3': 26, '4': 24, '5': 0, '6': 0}
        self.writes = []
        self.reject = False
        self.fail = False
        self.calls = 0
        self.config = None
        self.active = 0
        self.max_active = 0
        self.delay = 0

    def factory(self, device_id, ip, key, version):
        self.config = (device_id, ip, key, version)
        return self

    def set_socketPersistent(self, value):
        pass

    def set_socketTimeout(self, value):
        pass

    def set_socketRetryLimit(self, value):
        pass

    def set_socketRetryDelay(self, value):
        pass

    def status(self):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        time.sleep(self.delay)
        self.active -= 1
        if self.fail:
            return {'Error': 'transport error ' + TEST_KEY, 'Err': '914', 'Payload': TEST_KEY}
        return {'dps': dict(self.dps)}

    def set_value(self, dp, value):
        self.writes.append((dp, value))
        if not self.reject:
            self.dps[str(dp)] = value
        return {'data': {'dps': {str(dp): value}}}


def source_record(device_id='fixture-heater-001', key=TEST_KEY):
    return {'id': device_id, 'key': key, 'mac': PANEL_MAC, 'ip': '192.168.1.102', 'name': 'Panel Heater'}


class ManagerFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / 'devices.json'
        self.source.write_text(json.dumps([source_record()]))
        self.device = FakeDevice()
        self.adapter = LocalTuya(self.device.factory, sleeper=lambda _: None)
        self.manager = IoTManager(self.base / 'data', self.source, self.adapter)
        self.module = self.manager.list_modules()[0]
        self.id = self.module['id']

    def tearDown(self):
        self.tmp.cleanup()


class DriverTests(ManagerFixture):
    def test_confirmed_temperature_write_and_protocol(self):
        state = self.manager.command(self.id, {'control': 'target_temp_c', 'value': 26})
        self.assertEqual(self.device.writes, [(3, 26)])
        self.assertEqual(self.device.config[-1], 3.4)
        self.assertEqual(state['target_temp_c'], 26)
        self.assertEqual(state['current_temp_c'], 24)
        self.assertTrue(state['power'])
        self.assertNotIn('heating', state)
        self.assertTrue(self.manager.events(self.id)[0]['ok'])

    def test_read_only_values_and_invalid_types_never_write(self):
        bad = [('current_temp_c', 25), ('fault_code', 0), ('target_temp_c', True),
               ('target_temp_c', 38), ('target_temp_c', -1), ('target_temp_c', 26.0),
               ('power', 1), ('locked', 'false'), ('timer_minutes', 59), ('timer_minutes', 1500)]
        for control, value in bad:
            with self.subTest(control=control, value=value), self.assertRaises(IoTError):
                self.manager.command(self.id, {'control':control, 'value':value})
        self.assertEqual(self.device.writes, [])

    def test_lock_timer_and_power_dp_mapping(self):
        for control, value, dp in [('locked', True, 2), ('timer_minutes', 120, 5), ('power', False, 1)]:
            state = self.manager.command(self.id, {'control':control, 'value':value})
            self.assertEqual(state[control], value)
            self.assertEqual(self.device.writes[-1], (dp, value))

    def test_timer_read_allows_running_minutes(self):
        self.device.dps['5'] = 59
        self.assertEqual(self.manager.state(self.id)['timer_minutes'], 59)

    def test_unconfirmed_write_not_reported_as_success(self):
        self.device.reject = True
        with self.assertRaises(IoTError) as raised:
            self.manager.command(self.id, {'control':'target_temp_c', 'value':27})
        self.assertEqual(raised.exception.code, 'write_unconfirmed')
        self.assertFalse(self.manager.events(self.id)[0]['ok'])

    def test_offline_is_unknown_and_private_error_is_redacted(self):
        self.device.fail = True
        state = self.manager.state(self.id)
        self.assertFalse(state['online'])
        self.assertNotIn('power', state)
        self.assertNotIn(TEST_KEY, json.dumps(state))

    def test_fault_is_exposed_separately_from_power(self):
        self.device.dps['6'] = 1
        state = self.manager.state(self.id)
        self.assertTrue(state['power'])
        self.assertEqual(state['fault_code'], 1)
        self.assertIn('E1', state['faults'][0])

    def test_incomplete_response_is_unknown(self):
        del self.device.dps['3']
        state = self.manager.state(self.id)
        self.assertFalse(state['online'])
        self.assertNotIn('power', state)

    def test_generic_switch_has_configurable_power_dp(self):
        module = self.manager.add_module({'name':'Test switch', 'driver':'tuya_switch',
            'device_id':'fixture-switch-001', 'ip':'192.168.1.110', 'local_key':TEST_KEY, 'power_dp':7})
        self.device.dps['7'] = False
        state = self.manager.command(module['id'], {'control':'power', 'value':True})
        self.assertTrue(state['power'])
        self.assertEqual(self.device.writes[-1], (7, True))
        with self.assertRaises(IoTError):
            self.manager.command(module['id'], {'control':'locked', 'value':True})


class RegistryTests(ManagerFixture):
    def test_no_key_in_public_data_and_private_storage_permissions(self):
        self.manager.state(self.id)
        views = [self.manager.list_modules(), self.manager.get_module(self.id),
                 self.manager.candidates(), self.manager.panel(), self.manager.events()]
        self.assertNotIn(TEST_KEY, json.dumps(views))
        self.assertNotIn('local_key', json.dumps(views))
        self.assertEqual(self.manager.db_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.data_dir.stat().st_mode & 0o777, 0o700)

    def test_rename_ip_update_and_empty_key_preserve_key(self):
        module = self.manager.update_module(self.id, {'name':'Obývák', 'room':'Dole',
                                                     'ip':'192.168.1.120', 'local_key':''})
        self.assertEqual(module['name'], 'Obývák')
        self.manager.state(self.id)
        self.assertEqual(self.device.config[1:3], ('192.168.1.120', TEST_KEY))

    def test_import_updates_repaired_identity_by_mac_and_preserves_custom_ip(self):
        self.manager.update_module(self.id, {'name':'Můj panel', 'ip':'192.168.1.120'})
        fresh_key = 'fedcba9876543210'
        self.source.write_text(json.dumps([source_record('fixture-repaired-001', fresh_key)]))
        result = self.manager.import_source()
        self.assertEqual(result['updated'], 1)
        module = self.manager.get_module(self.id)
        self.assertEqual(module['device_id'], 'fixture-repaired-001')
        self.assertEqual(module['name'], 'Můj panel')
        self.manager.state(self.id)
        self.assertEqual(self.device.config[1:3], ('192.168.1.120', fresh_key))

    def test_disabled_device_never_polled_or_controlled(self):
        self.manager.update_module(self.id, {'enabled':False})
        self.assertTrue(self.manager.state(self.id)['paused'])
        with self.assertRaises(IoTError) as raised:
            self.manager.command(self.id, {'control':'power', 'value':True})
        self.assertEqual(raised.exception.code, 'paused')
        self.assertEqual(self.device.calls, 0)
        self.assertEqual(self.device.writes, [])

    def test_delete_stays_deleted_after_restart_until_explicit_import(self):
        self.manager.delete_module(self.id)
        restarted = IoTManager(self.base / 'data', self.source, self.adapter)
        self.assertEqual(restarted.list_modules(), [])
        self.assertEqual(restarted.import_source()['added'], 1)

    def test_duplicates_public_ips_and_unknown_configuration_rejected(self):
        with self.assertRaises(IoTError) as duplicate:
            self.manager.add_module({'source_id':'fixture-heater-001'})
        self.assertEqual(duplicate.exception.status, 409)
        for changes in [{'ip':'37.48.27.210'}, {'ip':'127.0.0.1'}, {'driver':[]},
                        {'enabled':1}, {'local_key':'short'}, {'unexpected':'value'}]:
            with self.subTest(changes=changes), self.assertRaises(IoTError):
                self.manager.update_module(self.id, changes)

    def test_cache_expires_and_fresh_request_bypasses_cache(self):
        self.manager.state(self.id)
        self.device.dps['3'] = 29
        self.assertEqual(self.manager.state(self.id)['target_temp_c'], 26)
        self.assertEqual(self.manager.state(self.id, fresh=True)['target_temp_c'], 29)
        self.manager._cache[self.id] = (time.monotonic() - 60, {'online':True,'power':True})
        self.assertIsNone(self.manager.list_modules()[0]['state'])

    def test_simultaneous_requests_are_serialized_for_one_device(self):
        self.device.delay = .05
        threads = [threading.Thread(target=self.manager.state, args=(self.id, True)) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(self.device.max_active, 1)
        self.assertEqual(self.device.calls, 3)


class RoamingDevice(FakeDevice):
    def __init__(self):
        super().__init__()
        self.live_ip = '192.168.1.102'
        self.connected_ips = []
        self.errors = {}

    def factory(self, device_id, ip, key, version):
        self.connected_ips.append(ip)
        return super().factory(device_id, ip, key, version)

    def status(self):
        address = self.config[1]
        if address in self.errors:
            return self.errors[address]
        if address != self.live_ip:
            self.calls += 1
            return {'Error':'unreachable ' + TEST_KEY, 'Err':'905', 'Payload':TEST_KEY}
        return super().status()

    def set_value(self, dp, value):
        if self.config[1] != self.live_ip:
            self.writes.append((dp, value))
            return {'Error':'unreachable ' + TEST_KEY, 'Err':'905', 'Payload':TEST_KEY}
        return super().set_value(dp, value)


class ReconnectionTests(ManagerFixture):
    def setUp(self):
        super().setUp()
        self.device = RoamingDevice()
        self.clock = [100]
        self.scans = []
        self.found = {'gwId': self.module['device_id'], 'ip':'192.168.1.150'}
        self.adapter = LocalTuya(self.device.factory, sleeper=lambda _: None,
                                 discoverer=self.discover, clock=lambda: self.clock[0])
        self.manager.adapter = self.adapter

    def discover(self, device_id):
        self.scans.append(device_id)
        return self.found

    def test_changed_dhcp_ip_recovers_read_only_and_persists_across_restart(self):
        self.manager.update_module(self.id, {'name':'Můj panel', 'room':'Obývák'})
        self.manager.command(self.id, {'control':'target_temp_c', 'value':26})
        writes, events = list(self.device.writes), self.manager.events(self.id)
        self.device.live_ip = '192.168.1.150'
        state = self.manager.state(self.id, fresh=True)
        self.assertTrue(state['online'])
        self.assertEqual(state['ip'], self.device.live_ip)
        self.assertEqual(self.scans, [self.module['device_id']])
        module = self.manager.get_module(self.id)
        self.assertEqual((module['ip'], module['name'], module['room']),
                         (self.device.live_ip, 'Můj panel', 'Obývák'))
        self.assertEqual(module['device_id'], self.module['device_id'])
        self.assertEqual(module['version'], '3.4')
        self.assertEqual(self.device.config[2], TEST_KEY)
        self.assertEqual(self.device.writes, writes)
        self.assertEqual(self.manager.events(self.id), events)
        self.assertNotIn(TEST_KEY, json.dumps([state, module]))
        restarted = IoTManager(self.base / 'data', self.source, self.adapter)
        self.assertTrue(restarted.state(self.id)['online'])
        self.assertEqual(self.device.connected_ips[-1], self.device.live_ip)
        self.assertEqual(len(self.scans), 1)

    def test_unavailable_device_is_unknown_and_scan_is_rate_limited(self):
        self.device.live_ip = '192.168.1.150'
        self.found = None
        for _ in range(3):
            state = self.manager.state(self.id, fresh=True)
            self.assertFalse(state['online'])
            self.assertNotIn('power', state)
            self.assertNotIn(TEST_KEY, json.dumps(state))
        self.assertEqual(len(self.scans), 1)
        self.assertEqual(self.manager.get_module(self.id)['ip'], '192.168.1.102')
        self.clock[0] += 61
        self.found = {'gwId':self.module['device_id'], 'ip':self.device.live_ip}
        self.assertTrue(self.manager.state(self.id, fresh=True)['online'])
        self.assertEqual(len(self.scans), 2)

    def test_foreign_id_public_ip_and_invalid_status_never_replace_address(self):
        self.device.live_ip = '192.168.1.150'
        cases = [({'gwId':'another-device', 'ip':self.device.live_ip}, False),
                 ({'gwId':self.module['device_id'], 'ip':'37.48.27.210'}, False),
                 ({'gwId':self.module['device_id'], 'ip':'not-an-ip'}, False),
                 ({'gwId':self.module['device_id'], 'ip':self.device.live_ip}, True)]
        for found, invalid_status in cases:
            with self.subTest(found=found):
                self.clock[0] += 61
                self.found = found
                self.device.errors = {self.device.live_ip:{'dps':{}}} if invalid_status else {}
                self.device.connected_ips.clear()
                self.assertFalse(self.manager.state(self.id, fresh=True)['online'])
                self.assertEqual(self.manager.get_module(self.id)['ip'], '192.168.1.102')
                if not invalid_status:
                    self.assertEqual(self.device.connected_ips, ['192.168.1.102'])
        self.assertEqual(self.device.writes, [])

    def test_key_error_does_not_trigger_discovery(self):
        self.device.errors['192.168.1.102'] = {'Error':'secret ' + TEST_KEY, 'Err':'914'}
        state = self.manager.state(self.id, fresh=True)
        self.assertEqual(state['error_code'], 'authentication_failed')
        self.assertEqual(self.scans, [])
        self.assertNotIn(TEST_KEY, json.dumps(state))

    def test_failed_command_is_never_replayed_by_reconnection(self):
        self.device.live_ip = '192.168.1.150'
        with self.assertRaises(IoTError):
            self.manager.command(self.id, {'control':'target_temp_c', 'value':28})
        self.assertEqual(self.device.writes, [(3, 28)])
        self.assertEqual(self.scans, [])
        self.assertFalse(self.manager.events(self.id)[0]['ok'])
        self.assertTrue(self.manager.state(self.id, fresh=True)['online'])
        self.assertEqual(self.device.writes, [(3, 28)])
        self.assertEqual(self.device.dps['3'], 26)
        self.manager.command(self.id, {'control':'target_temp_c', 'value':28})
        self.assertEqual(self.device.writes, [(3, 28), (3, 28)])
        self.assertEqual(self.device.connected_ips[-1], self.device.live_ip)
        self.assertTrue(self.manager.events(self.id)[0]['ok'])

    def test_healthy_and_paused_devices_do_not_scan(self):
        self.assertTrue(self.manager.state(self.id, fresh=True)['online'])
        self.manager.update_module(self.id, {'enabled':False})
        self.device.live_ip = '192.168.1.150'
        self.assertTrue(self.manager.state(self.id, fresh=True)['paused'])
        self.assertEqual(self.scans, [])

    def test_discovery_exception_is_redacted_and_does_not_change_address(self):
        self.device.live_ip = '192.168.1.150'
        with patch.object(self.adapter, 'discoverer', side_effect=RuntimeError(TEST_KEY)):
            state = self.manager.state(self.id, fresh=True)
        self.assertFalse(state['online'])
        self.assertNotIn(TEST_KEY, json.dumps(state))
        self.assertEqual(self.manager.get_module(self.id)['ip'], '192.168.1.102')

    def test_same_ip_can_recover_without_changing_registration(self):
        self.device.live_ip = '192.168.1.102'
        self.found = {'gwId':self.module['device_id'], 'ip':self.device.live_ip}
        original_status = self.device.status
        failures = [True]
        def fail_once():
            if failures:
                failures.pop()
                return {'Error':'unreachable', 'Err':'905'}
            return original_status()
        with patch.object(self.device, 'status', side_effect=fail_once):
            self.assertTrue(self.manager.state(self.id, fresh=True)['online'])
        self.assertEqual(self.manager.get_module(self.id)['updated_at'], self.module['updated_at'])
        self.assertEqual(self.device.writes, [])


class DiscoveryContractTests(unittest.TestCase):
    def test_installed_tinytuya_scanner_is_read_only_bounded_and_matches_id(self):
        import inspect
        from tinytuya import scanner
        result = {'gwId':'fixture', 'ip':'192.168.1.150', 'version':'3.4'}
        with patch.object(scanner, 'devices', return_value={'fixture':result}) as scan:
            self.assertEqual(LocalTuya._scan('fixture'), result)
        options = scan.call_args.kwargs
        inspect.signature(scanner.devices).bind(**options)
        self.assertFalse(options['poll'])
        self.assertFalse(options['forcescan'])
        self.assertEqual(options['scantime'], 5)
        self.assertEqual(options['wantids'], ['fixture'])
        self.assertEqual(options['tuyadevices'][0]['key'], '')

    def test_parallel_discoveries_do_not_contend_for_udp_ports(self):
        started, release = threading.Event(), threading.Event()
        def scan(device_id):
            started.set()
            release.wait(timeout=2)
            return {'gwId':device_id, 'ip':'192.168.1.150'}
        adapter = LocalTuya(discoverer=scan)
        result = []
        thread = threading.Thread(target=lambda: result.append(adapter.discover({'device_id':'first'})))
        thread.start()
        try:
            self.assertTrue(started.wait(timeout=1))
            self.assertIsNone(adapter.discover({'device_id':'second'}))
        finally:
            release.set()
            thread.join(timeout=2)
        self.assertEqual(result, ['192.168.1.150'])


class HTTPTests(ManagerFixture):
    def setUp(self):
        super().setUp()
        self.server = make_server('127.0.0.1', 0, self.manager)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def request(self, path, method='GET', data=None, headers=None):
        headers = headers or {}
        if data is not None:
            data = json.dumps(data).encode()
            headers = {'Content-Type':'application/json', **headers}
        request = Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            response = urlopen(request, timeout=4)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.read()

    def test_live_api_read_command_and_lcd_endpoint(self):
        code, payload = self.request('/api/iot/devices')
        self.assertEqual(code, 200)
        self.assertNotIn(TEST_KEY.encode(), payload)
        code, payload = self.request(f'/api/iot/devices/{self.id}/command', 'POST',
                                    {'control':'target_temp_c', 'value':28})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(payload)['state']['target_temp_c'], 28)
        code, payload = self.request('/api/iot/panel')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(payload)['state']['target_temp_c'], 28)

    def test_private_files_not_served(self):
        for path in ['/devices.json', '/modules.sqlite3', '/iot/server.py', '/.env', '/../../devices.json']:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_foreign_origins_other_ports_and_public_forwarded_clients_denied(self):
        for headers in [{'Origin':'https://attacker.example'}, {'Origin':'http://127.0.0.1:9999'},
                        {'Origin':'http://['},
                        {'CF-Connecting-IP':'203.0.113.12'}, {'X-Forwarded-For':'203.0.113.12'}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request('/api/iot/devices', headers=headers)[0], 403)
        self.assertEqual(self.request('/api/iot/devices', headers={'Origin':self.base_url})[0], 200)

    def test_http_validation_and_disabled_command(self):
        code, _ = self.request(f'/api/iot/devices/{self.id}/command', 'POST',
                               {'control':'current_temp_c', 'value':27})
        self.assertEqual(code, 400)
        self.assertEqual(self.request(f'/api/iot/devices/{self.id}', 'PUT', {'enabled':False})[0], 200)
        self.assertEqual(self.request(f'/api/iot/devices/{self.id}/command', 'POST',
                                      {'control':'power', 'value':True})[0], 409)
        self.assertEqual(self.device.writes, [])

    def test_web_assets_and_health(self):
        self.assertIn('IoT moduly'.encode(), self.request('/')[1])
        self.assertEqual(self.request('/iot.js')[0], 200)
        self.assertTrue(json.loads(self.request('/api/iot/health')[1])['ok'])


if __name__ == '__main__':
    unittest.main()
