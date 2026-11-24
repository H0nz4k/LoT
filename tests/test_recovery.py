"""All switching here is an in-memory Sonoff simulation, never a real device."""
from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
import uuid
import json
from urllib.error import HTTPError
from urllib.request import Request,urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'iot'))
from iot import IoTManager
from iot_driver import IoTError
from ewelink_driver import Ewelink
from energy import DEVICE_ID
from recovery import PowerRecovery, process_lock
from access import remote_route_allowed
from server import make_server
from unittest.mock import patch


class FakeSonoff:
    def __init__(self):
        self.item={'deviceid':DEVICE_ID,'brandName':'SONOFF','productModel':'S60TPF','online':True,
                   'extra':{'uiid':190},'params':{'switches':[{'outlet':0,'switch':'on'}],
                                                'pulses':[{'outlet':0,'pulse':'off'}]}}
        self.client=self
        self.writes=[]
        self.fail_off=False
        self.fail_on=False
        self.power=Ewelink.power
    def things(self,force=False):
        return [deepcopy(self.item)]
    def status(self,module=None,force=False):
        if module is None:return {'configured':False,'connected':False}
        return {'online':self.item['online'],'power':self.power(self.item['params'])}
    def command(self,module,command):
        assert module['device_id']==DEVICE_ID
        value=command['value']
        self.writes.append(value)
        if value and self.fail_on:
            raise IoTError('private transport detail',502,'ewelink_unavailable')
        self.item['params']['switches'][0]['switch']='on' if value else 'off'
        if not value and self.fail_off:
            raise IoTError('private transport detail',502,'not_confirmed')
        return {'online':True,'power':value}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.manager=IoTManager(Path(self.tmp.name)/'data',Path(self.tmp.name)/'missing')
        self.adapter=FakeSonoff()
        self.manager.ewelink_adapter=self.adapter
        self.recovery=self.manager.recovery
        self.waits=[]
        self.recovery.sleep=self.waits.append
        self.id=str(uuid.uuid4())
        with self.manager._db() as db:
            db.execute('''INSERT INTO modules(id,name,room,driver,device_id,mac,ip,version,local_key,power_dp,enabled,created_at,updated_at)
                VALUES('012345abcdef','Zás. infra','','sonoff_ewelink',?,NULL,'','cloud','',1,1,'','')''',(DEVICE_ID,))

    def tearDown(self):
        if self.recovery.thread:self.recovery.thread.join(timeout=5)
        self.tmp.cleanup()

    def start(self):
        self.recovery.start(self.id,DEVICE_ID,'power-cycle-30s')
        self.recovery.thread.join(timeout=5)
        self.assertFalse(self.recovery.thread.is_alive())
        return self.recovery.status()['operation']

    def pending(self,phase='off',intent=1):
        with self.manager._db() as db:
            db.execute('INSERT INTO infra_recovery(id,module_id,device_id,phase,message,off_intent,requested_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                       (self.id,'012345abcdef',DEVICE_ID,phase,'test interrupted',intent,time.time()-20,time.time()))

    def test_full_cycle_verifies_and_waits_30_seconds_and_never_replays_off(self):
        row=self.start()
        self.assertEqual(row['phase'],'completed')
        self.assertEqual(self.adapter.writes,[False,True])
        self.assertEqual(self.waits,[30])
        self.recovery.start(self.id,DEVICE_ID,'power-cycle-30s')
        self.assertEqual(self.adapter.writes,[False,True])
        with self.manager._db() as db:
            phases=[r['phase'] for r in db.execute('SELECT phase FROM energy_events')]
        for phase in ('recovery_started','recovery_off_intent','recovery_off','recovery_restoring','recovery_completed'):
            self.assertIn(phase,phases)

    def test_wrong_device_confirmation_uuid_and_model_do_not_switch(self):
        for args in ((self.id,'other','power-cycle-30s'),(self.id,DEVICE_ID,'yes'),('bad',DEVICE_ID,'power-cycle-30s')):
            with self.assertRaises(IoTError):self.recovery.start(*args)
        self.adapter.item['productModel']='other'
        self.assertEqual(self.start()['phase'],'failed')
        self.assertEqual(self.adapter.writes,[])

    def test_concurrent_requests_hold_device_lock_and_allow_only_one_off(self):
        waiting=threading.Event();release=threading.Event()
        def wait(seconds):
            if seconds==30:
                waiting.set()
                if not release.wait(5):raise RuntimeError('test timeout')
        self.recovery.sleep=wait
        try:
            self.recovery.start(self.id,DEVICE_ID,'power-cycle-30s')
            self.assertTrue(waiting.wait(3))
            self.assertTrue(self.manager._locks['012345abcdef'].locked())
            self.recovery.start(self.id,DEVICE_ID,'power-cycle-30s')
            with self.assertRaises(IoTError):self.recovery.start(str(uuid.uuid4()),DEVICE_ID,'power-cycle-30s')
            self.assertEqual(self.adapter.writes,[False])
            self.manager.energy.collect()
        finally:
            release.set()
            if self.recovery.thread:self.recovery.thread.join(timeout=5)
        self.assertEqual(self.adapter.writes,[False,True])

    def test_other_process_lock_refuses_admission_without_leaving_started_operation(self):
        with process_lock(self.manager.data_dir/'infra-recovery.lock'):
            with self.assertRaises(IoTError):self.recovery.start(self.id,DEVICE_ID,'power-cycle-30s')
        self.assertIsNone(self.recovery._row(self.id))
        self.assertEqual(self.adapter.writes,[])

    def test_offline_initially_off_and_unsafe_pulse_are_refused(self):
        for change in ('offline','off','pulse'):
            with self.subTest(change=change):
                self.id=str(uuid.uuid4())
                self.adapter.item['online']=change!='offline'
                self.adapter.item['params']['switches'][0]['switch']='off' if change=='off' else 'on'
                self.adapter.item['params']['pulses'][0]['pulse']='on' if change=='pulse' else 'off'
                self.assertEqual(self.start()['phase'],'failed')
        self.assertEqual(self.adapter.writes,[])

    def test_uncertain_off_immediately_restores_on_without_another_off(self):
        self.adapter.fail_off=True
        row=self.start()
        self.assertEqual(row['phase'],'restored')
        self.assertEqual(self.adapter.writes,[False,True])
        self.assertNotIn(30,self.waits)
        self.assertNotIn('private transport detail',str(row))

    def test_failed_on_has_bounded_retries_blocks_new_off_and_allows_on_only(self):
        self.adapter.fail_on=True
        self.assertEqual(self.start()['phase'],'needs_review')
        self.assertEqual(self.adapter.writes,[False,True,True,True])
        with self.assertRaises(IoTError):self.recovery.start(str(uuid.uuid4()),DEVICE_ID,'power-cycle-30s')
        self.adapter.fail_on=False
        self.recovery.retry_restore(self.id,'restore-on')
        self.recovery.thread.join(timeout=5)
        self.assertEqual(self.recovery.status()['operation']['phase'],'restored')
        self.assertEqual(self.adapter.writes.count(False),1)

    def test_restart_only_restores_durable_intent_and_does_not_replay_off(self):
        self.pending()
        self.adapter.item['params']['switches'][0]['switch']='off'
        self.recovery=PowerRecovery(self.manager)
        self.recovery.sleep=self.waits.append
        self.recovery.resume();self.recovery.thread.join(timeout=5)
        self.assertEqual(self.recovery.status()['operation']['phase'],'restored')
        self.assertEqual(self.adapter.writes,[True])

    def test_restart_before_off_intent_and_changed_identity_never_switch(self):
        self.pending('started',0)
        self.recovery.resume();self.recovery.thread.join(timeout=5)
        self.assertEqual(self.recovery.status()['operation']['phase'],'failed')
        with self.manager._db() as db:db.execute('UPDATE infra_recovery SET phase=?,off_intent=1 WHERE id=?',('off',self.id))
        self.adapter.item['deviceid']='other'
        self.recovery.resume();self.recovery.thread.join(timeout=5)
        self.assertEqual(self.recovery.status()['operation']['phase'],'needs_review')
        self.assertEqual(self.adapter.writes,[])

    def test_journal_failure_before_off_prevents_switching(self):
        original=self.recovery._set
        def fail_intent(op,phase,message,**fields):
            if phase=='off_intent':raise sqlite3.OperationalError('synthetic')
            return original(op,phase,message,**fields)
        self.recovery._set=fail_intent
        self.assertEqual(self.start()['phase'],'failed')
        self.assertEqual(self.adapter.writes,[])

    def test_journal_failure_after_off_cannot_prevent_on_restore(self):
        original=self.recovery._set
        def fail_after(op,phase,message,**fields):
            if phase in ('off','restoring'):raise sqlite3.OperationalError('synthetic')
            return original(op,phase,message,**fields)
        self.recovery._set=fail_after
        self.assertEqual(self.start()['phase'],'restored')
        self.assertEqual(self.adapter.writes,[False,True])

    def test_monitor_skips_recovery_and_never_integrates_power_over_blackout(self):
        self.pending()
        self.adapter.status=Mock(side_effect=AssertionError('must not poll'))
        self.manager.energy.collect()
        self.adapter.status.assert_not_called()
        with self.manager._db() as db:
            row=db.execute('SELECT * FROM energy_samples').fetchone()
            self.assertEqual(row['status'],'gap')
            self.assertIsNone(row['power_w'])
        with self.manager._db() as db:db.execute('DELETE FROM energy_samples')
        t=time.time()-120
        self.manager.energy.record({'power_w':600},at=t)
        # Use a separate future slot to avoid overwriting the gap above.
        self.manager.energy.record({'power_w':600},at=t+180)
        with self.manager._db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM energy_intervals').fetchone()[0],0)

    def test_cross_process_lock_and_remote_route_preserve_guards(self):
        with process_lock(self.manager.data_dir/'test.lock'):
            with self.assertRaises(IoTError):
                with process_lock(self.manager.data_dir/'test.lock'):pass
        self.assertTrue(remote_route_allowed('GET','/api/iot/heating/recovery'))
        self.assertFalse(remote_route_allowed('POST','/api/iot/heating/recovery'))
        self.assertFalse(remote_route_allowed('POST','/api/iot/heating/recovery/restore'))

    def test_monitor_migration_failure_does_not_block_pending_on_recovery(self):
        self.pending()
        self.adapter.item['params']['switches'][0]['switch']='off'
        with patch('iot.EnergyMonitor',side_effect=sqlite3.OperationalError('synthetic')):
            manager=IoTManager(self.manager.data_dir,Path(self.tmp.name)/'missing')
        manager.ewelink_adapter=self.adapter
        self.assertIsNone(manager.energy)
        manager.recovery.sleep=self.waits.append
        manager.recovery.resume();manager.recovery.thread.join(timeout=5)
        self.assertEqual(manager.recovery.status()['operation']['phase'],'restored')
        self.assertEqual(self.adapter.writes,[True])

    def test_http_requires_explicit_confirmation_and_duplicate_request_is_idempotent(self):
        server=make_server('127.0.0.1',0,self.manager)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}/api/iot/heating/recovery'
        def send(body,headers=None):
            return urlopen(Request(base,data=json.dumps(body).encode(),headers={'Content-Type':'application/json',**(headers or {})},method='POST'))
        try:
            with self.assertRaises(HTTPError) as error:send({})
            self.assertEqual(error.exception.code,400)
            error.exception.close()
            body={'request_id':self.id,'device_id':DEVICE_ID,'confirm':'power-cycle-30s'}
            with self.assertRaises(HTTPError) as error:send(body,{'Origin':'https://untrusted.example'})
            self.assertEqual(error.exception.code,403)
            error.exception.close()
            self.assertEqual(self.adapter.writes,[])
            with send(body) as response:self.assertEqual(response.status,202)
            self.recovery.thread.join(timeout=5)
            with send(body) as response:self.assertEqual(json.load(response)['operation']['phase'],'completed')
            self.assertEqual(self.adapter.writes,[False,True])
            with urlopen(base) as response:self.assertEqual(json.load(response)['operation']['id'],self.id)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':unittest.main()
