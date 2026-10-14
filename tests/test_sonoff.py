import copy
from io import BytesIO
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'iot'))
from iot import IoTManager
from iot_driver import IoTError
from sonoff_driver import CloudSonoff, NoRedirect
from configure_sonoff import save_credentials


class SonoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.credentials = self.base / 'ewelink.json'
        self.credentials.write_text(json.dumps({'region':'eu','appid':'test-app','access_token':'test-secret-token'}))
        self.thing = {'deviceid':'test-sonoff', 'productModel':'S60TPF', 'online':True,
                      'extra':{'uiid':999}, 'params':{'switches':[{'outlet':0,'switch':'off'}],
                                                     'power':12345,'oneKwhData':88}}
        self.calls, self.error, self.unconfirmed = [], False, False
        self.now = 0
        def sleep(delay):
            self.now += delay
        self.cloud = CloudSonoff(self.credentials, self.transport, lambda:self.now, sleep)
        source = self.base / 'devices.json'
        source.write_text('[]')
        self.manager = IoTManager(self.base/'data', source, sonoff_adapter=self.cloud)

    def tearDown(self):
        self.tmp.cleanup()

    def transport(self, request, timeout):
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.full_url, body, dict(request.header_items()), self.now))
        if self.error:
            raise OSError('test-secret-token private upstream error')
        if request.full_url.endswith('/status'):
            if not self.unconfirmed:
                self.thing['params'].update(body['params'])
            data = {}
        else:
            data = {'thingList':[{'itemType':1,'itemData':copy.deepcopy(self.thing)}]}
        return BytesIO(json.dumps({'error':0,'data':data}).encode())

    def add(self):
        return self.manager.add_module({'driver':'sonoff_s60tpf','device_id':'test-sonoff','name':'SONOFF'})

    def test_registration_is_read_only_and_keeps_credentials_private(self):
        module = self.add()
        self.assertEqual(len(self.calls),1)
        self.assertIsNone(self.calls[0][1])
        self.assertTrue(self.calls[0][0].startswith('https://eu-apia.coolkit.cc/'))
        self.assertEqual(self.calls[0][2]['Authorization'],'Bearer test-secret-token')
        self.assertFalse(module['state']['power'])
        public = json.dumps(self.manager.list_modules())
        self.assertNotIn('test-secret-token',public)
        self.assertNotIn('power_w',public)
        self.assertNotIn('energy_today_kwh',public)
        self.assertEqual(module['ip'],'')

    def test_commands_use_only_observed_switch_shape_and_read_back(self):
        module = self.add()
        state = self.manager.command(module['id'],{'control':'power','value':True})
        self.assertTrue(state['power'])
        writes = [c for c in self.calls if c[0].endswith('/status')]
        self.assertEqual(writes[0][1],{'type':1,'id':'test-sonoff','params':{'switches':[{'outlet':0,'switch':'on'}]}})
        self.assertTrue(all(b[3]-a[3]>=1 for a,b in zip(self.calls,self.calls[1:])))
        self.assertIn('cloudem',self.manager.events(module['id'])[0]['message'])

    def test_scalar_switch_is_supported(self):
        self.thing['params'] = {'switch':'off'}
        module = self.add()
        self.manager.command(module['id'],{'control':'power','value':True})
        self.assertEqual(self.calls[-2][1]['params'],{'switch':'on'})

    def test_unconfirmed_command_is_failure_with_no_write_retry(self):
        module = self.add(); self.unconfirmed = True
        with self.assertRaises(IoTError) as caught:
            self.manager.command(module['id'],{'control':'power','value':True})
        self.assertEqual(caught.exception.code,'unconfirmed')
        self.assertEqual(sum(c[0].endswith('/status') for c in self.calls),1)
        self.assertFalse(self.manager.events(module['id'])[0]['ok'])

    def test_write_timeout_discards_pre_command_cache_without_retry(self):
        module = self.add()
        original = self.cloud.transport
        def fail_write(request,timeout):
            if request.full_url.endswith('/status'):
                raise TimeoutError('test-secret-token')
            return original(request,timeout)
        self.cloud.transport = fail_write
        with self.assertRaises(IoTError):
            self.manager.command(module['id'],{'control':'power','value':True})
        self.assertIsNone(self.cloud.inventory)
        self.assertEqual(self.cloud.next_inventory,0)

    def test_model_offline_and_ambiguous_protocol_fail_closed(self):
        for changes in ({'productModel':'P110M'}, {'productModel':'S60TPF-M'},
                        {'params':{'switches':[{'outlet':1,'switch':'on'}]}},
                        {'params':{'switch':'on','switches':[{'outlet':0,'switch':'on'}]}}):
            with self.subTest(changes=changes):
                thing = {**self.thing, **changes}
                if thing['productModel']=='S60TPF':
                    with self.assertRaises(IoTError): self.cloud._state(thing)
                else:
                    old = self.thing; self.thing = thing; self.cloud.next_inventory = 0
                    with self.assertRaises(IoTError): self.add()
                    self.thing = old
        self.thing['online'] = False; self.cloud.next_inventory = 0
        module = self.add()
        self.assertNotIn('power',module['state'])
        with self.assertRaises(IoTError): self.manager.command(module['id'],{'control':'power','value':True})
        self.assertFalse(any(c[0].endswith('/status') for c in self.calls))

    def test_cache_backoff_rotation_and_redacted_errors(self):
        module = self.add()
        for _ in range(5): self.manager.state(module['id'],fresh=True)
        self.assertEqual(len(self.calls),1)
        self.now = 121; self.error = True
        state = self.manager.state(module['id'],fresh=True)
        self.assertFalse(state['online'])
        self.assertNotIn('test-secret-token',json.dumps(state))
        self.manager.state(module['id'],fresh=True)
        self.assertEqual(len(self.calls),2)
        self.error = False
        self.credentials.write_text(json.dumps({'region':'us','appid':'test-app','access_token':'rotated-token'}))
        self.assertTrue(self.manager.state(module['id'],fresh=True)['online'])
        self.assertTrue(self.calls[-1][0].startswith('https://us-apia.coolkit.cc/'))

    def test_rejects_foreign_secrets_driver_change_and_import(self):
        module = self.add()
        for changes in ({'local_key':'0123456789abcdef'},{'tapo_password':'secret'},
                        {'ip':'192.168.1.10'},{'driver':'tuya_switch'},{'device_id':'another'}):
            with self.assertRaises(IoTError): self.manager.update_module(module['id'],changes)
        self.assertEqual(self.manager.import_source(),{'added':0,'updated':0,'skipped':0})
        self.manager.update_module(module['id'],{'enabled':False})
        count = len(self.calls)
        self.assertTrue(self.manager.state(module['id'])['paused'])
        with self.assertRaises(IoTError): self.manager.command(module['id'],{'control':'power','value':True})
        self.assertEqual(count,len(self.calls))

    def test_only_published_meter_units_are_used_and_nan_is_omitted(self):
        self.thing['extra']['uiid'] = 5
        self.thing['params'].update(power='18.5',voltage='230.1',current='NaN')
        state = self.cloud._state(self.thing)
        self.assertEqual(state['power_w'],18.5)
        self.assertEqual(state['voltage_v'],230.1)
        self.assertNotIn('current_a',state)
        self.assertNotIn('energy_today_kwh',state)

    def test_credentials_missing_invalid_region_and_redirect_are_safe(self):
        self.credentials.write_text('{"region":"https://evil.example","appid":"a","access_token":"s"}')
        with self.assertRaises(IoTError): self.add()
        self.assertEqual(self.calls,[])
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://evil.example'))

    def test_interactive_provisioning_is_atomic_private_and_rejects_bad_headers(self):
        config = {'region':'eu','appid':'new-app','access_token':'new-token'}
        save_credentials(self.credentials,config)
        self.assertEqual(json.loads(self.credentials.read_text()),config)
        for token in ('bad\nheader','', 'bad token'):
            with self.assertRaises(ValueError):
                save_credentials(self.credentials,{**config,'access_token':token})
            self.assertEqual(json.loads(self.credentials.read_text()),config)
        self.assertEqual(list(self.base.glob('.ewelink-*')),[])
        if sys.platform != 'win32':
            self.assertEqual(self.credentials.stat().st_mode & 0o777,0o600)

    @unittest.skipUnless((ROOT.parent/'Dashboard/health/presence.py').exists(), 'Cross-repository test requires sibling Dashboard checkout')
    def test_arrival_and_departure_run_through_real_manager(self):
        spec = importlib.util.spec_from_file_location('scene_presence', ROOT.parent/'Dashboard/health/presence.py')
        presence = importlib.util.module_from_spec(spec); spec.loader.exec_module(presence)
        module = self.add()
        def request(url,body=None,timeout=None):
            if url.endswith('/devices'): return {'devices':self.manager.list_modules()}
            if url.endswith('/command'): return {'ok':True,'state':self.manager.command(url.split('/')[-2],body)}
            raise OSError('Radio not configured')
        scene = presence.Presence(self.base/'presence.json',transport=request)
        target = 'iot:'+module['id']
        scene.configure({'arrival':[target],'departure':[target]})
        for action,value in (('arrival',True),('departure',False)):
            scene.run(action,'test_request_'+action); scene.worker.join(2)
            self.assertTrue(scene.state()['success'])
            self.assertIs(self.manager.state(module['id'])['power'],value)
        self.assertTrue(scene.state()['away'])


if __name__ == '__main__':
    unittest.main()
