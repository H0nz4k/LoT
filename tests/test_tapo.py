import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'iot'))
from iot import IoTManager
from iot_driver import IoTError
from tapo_driver import LocalTapo
import test_iot as fixtures
from server import make_server

# Výhradně testovací údaje.
TEST_USER = 'fixture@example.invalid'
TEST_PASSWORD = '  test-only-Tapo-password  '


class FakeAuthenticationError(Exception):
    pass


class FakePlug:
    def __init__(self):
        self.model = 'P110M'
        self.mac = 'aa:bb:cc:dd:ee:ff'
        self.device_id = 'fixture-tapo-001'
        self.is_on = False
        self.modules = {'Energy': SimpleNamespace(current_consumption=18.5,
            consumption_today=.123, consumption_this_month=2.456, voltage=230.2, current=.081)}
        self.features = {}
        self.reject = False
        self.fail = None
        self.turns = []
        self.updates = 0
        self.disconnects = 0
        self.connections = []
        self.loops = []

    async def factory(self, module):
        self.connections.append((module['ip'], module['tapo_username'], module['tapo_password']))
        self.loops.append(asyncio.get_running_loop())
        return self

    async def update(self):
        self.updates += 1
        if self.fail is not None:
            raise self.fail

    async def turn_on(self):
        self.turns.append(True)
        if not self.reject:
            self.is_on = True

    async def turn_off(self):
        self.turns.append(False)
        if not self.reject:
            self.is_on = False

    async def disconnect(self):
        self.disconnects += 1


def tapo_config(**changes):
    return {'name':'Zásuvka dílna', 'room':'Dílna', 'driver':'tapo_p110m',
            'ip':'192.168.1.120', 'tapo_username':TEST_USER,
            'tapo_password':TEST_PASSWORD, **changes}


class TapoFixture:
    def setUp(self):
        super().setUp()
        self.plug = FakePlug()
        self.tapo = LocalTapo(self.plug.factory)
        self.manager.tapo_adapter = self.tapo
        self.module = self.manager.add_module(tapo_config())
        self.tapo_id = self.module['id']

    def tearDown(self):
        self.tapo.close()
        super().tearDown()


class TapoTests(TapoFixture, fixtures.ManagerFixture):
    def test_registration_reads_identity_without_switching_and_reports_correct_units(self):
        self.assertEqual(self.plug.turns, [])
        self.assertEqual(self.module['mac'], self.plug.mac)
        self.assertEqual(self.module['device_id'], self.plug.device_id)
        state = self.manager.state(self.tapo_id, fresh=True)
        self.assertFalse(state['power'])
        self.assertEqual(state['power_w'], 18.5)
        self.assertEqual(state['energy_today_kwh'], .123)
        self.assertEqual(state['energy_month_kwh'], 2.456)
        self.assertEqual(state['voltage_v'], 230.2)
        self.assertEqual(len(set(self.plug.loops)), 1)
        self.assertEqual(self.plug.disconnects, len(self.plug.connections))

    def test_confirmed_power_commands_are_routed_to_tapo_not_tuya(self):
        state = self.manager.command(self.tapo_id, {'control':'power', 'value':True})
        self.assertTrue(state['power'])
        state = self.manager.command(self.tapo_id, {'control':'power', 'value':False})
        self.assertFalse(state['power'])
        self.assertEqual(self.plug.turns, [True, False])
        self.assertEqual(self.device.writes, [])
        self.assertTrue(all(e['ok'] for e in self.manager.events(self.tapo_id)))

    def test_unconfirmed_write_is_failure_and_connection_is_closed(self):
        self.plug.reject = True
        with self.assertRaises(IoTError) as error:
            self.manager.command(self.tapo_id, {'control':'power', 'value':True})
        self.assertEqual(error.exception.code, 'write_unconfirmed')
        self.assertFalse(self.manager.events(self.tapo_id)[0]['ok'])
        self.assertEqual(self.plug.disconnects, len(self.plug.connections))

    def test_ip_reused_by_other_socket_cannot_be_controlled(self):
        self.plug.mac = 'aa:bb:cc:dd:ee:01'
        state = self.manager.state(self.tapo_id, fresh=True)
        self.assertEqual(state['error_code'], 'identity_mismatch')
        with self.assertRaises(IoTError):
            self.manager.command(self.tapo_id, {'control':'power', 'value':True})
        self.assertEqual(self.plug.turns, [])

    def test_wrong_model_is_not_registered(self):
        self.plug.model = 'P110'
        with self.assertRaises(IoTError) as error:
            self.manager.add_module(tapo_config(ip='192.168.1.121'))
        self.assertEqual(error.exception.code, 'wrong_device')
        self.assertEqual(len(self.manager.list_modules()), 2)
        self.assertEqual(self.plug.turns, [])

    def test_missing_or_invalid_meter_readings_stay_unknown(self):
        self.plug.modules['Energy'] = SimpleNamespace(current_consumption=float('nan'),
            consumption_today=-1, consumption_this_month=None, voltage=True, current=float('inf'))
        state = self.manager.state(self.tapo_id, fresh=True)
        self.assertTrue(state['online'])
        for field in ('power_w','energy_today_kwh','energy_month_kwh','voltage_v','current_a'):
            self.assertIsNone(state[field])
        self.plug.modules.clear()
        self.assertTrue(self.manager.command(self.tapo_id, {'control':'power','value':True})['power'])

    def test_credentials_preserved_on_blank_edit_and_updated_as_pair(self):
        self.manager.update_module(self.tapo_id, {'name':'Pracovní stůl', 'tapo_username':'', 'tapo_password':''})
        self.manager.state(self.tapo_id, fresh=True)
        self.assertEqual(self.plug.connections[-1][1:], (TEST_USER, TEST_PASSWORD))
        with self.assertRaises(IoTError):
            self.manager.update_module(self.tapo_id, {'tapo_password':'different'})
        self.manager.update_module(self.tapo_id, {'tapo_username':'new@example.invalid', 'tapo_password':' next test '})
        self.manager.state(self.tapo_id, fresh=True)
        self.assertEqual(self.plug.connections[-1][1:], ('new@example.invalid', ' next test '))

    def test_credentials_and_library_errors_never_appear_in_public_views(self):
        self.plug.fail = FakeAuthenticationError(TEST_USER + TEST_PASSWORD)
        with patch.dict(sys.modules, {'kasa': SimpleNamespace(AuthenticationError=FakeAuthenticationError)}):
            state = self.manager.state(self.tapo_id, fresh=True)
            self.assertEqual(state['error_code'], 'authentication_failed')
            with self.assertRaises(IoTError):
                self.manager.command(self.tapo_id, {'control':'power','value':True})
        views = json.dumps([self.manager.list_modules(), self.manager.get_module(self.tapo_id),
                            self.manager.events(), state])
        for secret in (TEST_USER, TEST_PASSWORD, 'tapo_password', 'tapo_username'):
            self.assertNotIn(secret, views)
        self.assertTrue(self.manager.get_module(self.tapo_id)['has_credentials'])

    def test_paused_deleted_restarted_and_imported_modules_preserve_behavior(self):
        self.manager.update_module(self.tapo_id, {'enabled':False})
        before = len(self.plug.connections)
        self.assertTrue(self.manager.state(self.tapo_id)['paused'])
        with self.assertRaises(IoTError):
            self.manager.command(self.tapo_id, {'control':'power','value':True})
        self.assertEqual(len(self.plug.connections), before)
        self.assertEqual(self.manager.import_source()['updated'], 1)
        restarted = IoTManager(self.base / 'data', self.source, self.adapter, tapo_adapter=self.tapo)
        self.assertTrue(restarted.get_module(self.tapo_id)['has_credentials'])
        self.manager.delete_module(self.tapo_id)
        self.assertFalse(any(m['driver'] == 'tapo_p110m' for m in restarted.list_modules()))

    def test_duplicate_address_identity_and_public_ip_fail_before_any_write(self):
        with self.assertRaises(IoTError) as error:
            self.manager.add_module(tapo_config())
        self.assertEqual(error.exception.code, 'duplicate')
        for changes in ({'ip':'37.48.27.210'}, {'tapo_password':''}, {'tapo_username':True}):
            with self.subTest(changes=changes), self.assertRaises(IoTError):
                self.manager.add_module(tapo_config(**changes))
        for value in (1, 'on'):
            with self.assertRaises(IoTError):
                self.manager.command(self.tapo_id, {'control':'power', 'value':value})
        self.assertEqual(self.plug.turns, [])


class MigrationTests(fixtures.ManagerFixture):
    def test_existing_heater_and_events_survive_idempotent_schema_migration(self):
        self.manager.command(self.id, {'control':'target_temp_c','value':28})
        with sqlite3.connect(self.manager.db_path) as db:
            db.execute('ALTER TABLE modules DROP COLUMN tapo_username')
            db.execute('ALTER TABLE modules DROP COLUMN tapo_password')
        for _ in range(2):
            restarted = IoTManager(self.base / 'data', self.source, self.adapter)
            self.assertEqual(restarted.get_module(self.id)['device_id'], 'fixture-heater-001')
            self.assertEqual(restarted.state(self.id)['target_temp_c'], 28)
            self.assertEqual(len(restarted.events(self.id)), 1)


class TapoHTTPTests(TapoFixture, fixtures.ManagerFixture):
    request = fixtures.HTTPTests.request

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

    def test_tapo_api_masks_credentials_and_confirms_state(self):
        for path in ('/api/iot/devices', '/api/iot/devices/' + self.tapo_id):
            code, payload = self.request(path)
            self.assertEqual(code, 200)
            self.assertNotIn(TEST_PASSWORD.encode(), payload)
            self.assertNotIn(TEST_USER.encode(), payload)
        code, payload = self.request('/api/iot/devices/' + self.tapo_id + '/command', 'POST',
                                      {'control':'power','value':True})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(payload)['state']['power'])
        self.assertNotIn(TEST_PASSWORD.encode(), payload)


@unittest.skipUnless(importlib.util.find_spec('kasa'), 'python-kasa není nainstalované')
class KasaCompatibilityTests(unittest.TestCase):
    def test_real_energy_module_converts_milliwatts_and_wh_to_public_units(self):
        from kasa import Module
        from kasa.smart import SmartDevice
        from kasa.smart.modules.energy import Energy
        async def readings():
            device = SmartDevice('192.168.1.120')
            try:
                device._components = {'energy_monitoring':1}
                device._last_update = {'get_energy_usage':
                    {'current_power':18500, 'today_energy':123, 'month_energy':2456}}
                energy = Energy(device, 'energy_monitoring')
                await energy._post_update_hook()
                self.assertEqual(Module.Energy, 'Energy')
                self.assertEqual(energy.current_consumption, 18.5)
                self.assertEqual(energy.consumption_today, .123)
                self.assertEqual(energy.consumption_this_month, 2.456)
            finally:
                await device.disconnect()
        asyncio.run(readings())


if __name__ == '__main__':
    unittest.main()
