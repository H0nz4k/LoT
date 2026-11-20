from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'iot'))
from energy import EnergyMonitor, TZ, DEVICE_ID, sonoff_energy
from iot import IoTManager
from iot_driver import IoTError
from server import make_server
from access import remote_route_allowed


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp()


class EnergyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = IoTManager(Path(self.tmp.name) / 'data', Path(self.tmp.name) / 'missing')
        self.monitor = self.manager.energy
        self.t = stamp('2026-11-20T12:00:00')

    def tearDown(self):
        self.monitor.stop.set()
        if self.monitor.thread:
            self.monitor.thread.join(timeout=2)
        self.tmp.cleanup()

    def sample(self, at, month=10, day=1, power=600, status='active'):
        self.monitor.record({'month_kwh':month, 'day_kwh':day, 'power_w':power,
                             'model':'S60TPF','counter_timezone':'Europe/Prague'},status,at=at)

    def rows(self, table):
        with self.manager._db() as db:
            return [dict(x) for x in db.execute('SELECT * FROM '+table)]

    def test_supported_units_and_invalid_instantaneous_readings(self):
        item={'productModel':'S60TPF','extra':{'uiid':190},'params':{'power':60000,'voltage':23000,'dayKwh':281,'monthKwh':6025}}
        data=sonoff_energy(item)
        self.assertEqual(data['power_w'],600)
        self.assertEqual(data['day_kwh'],2.81)
        self.assertEqual(data['month_kwh'],60.25)
        item['params']['voltage']=0
        self.assertIsNone(sonoff_energy(item)['power_w'])
        item['extra']['uiid']=1
        self.assertIsNone(sonoff_energy(item)['month_kwh'])
        item['extra']['uiid']=190
        for value in (-1,True,'NaN','inf',{},None):
            item['params']['monthKwh']=value
            self.assertIsNone(sonoff_energy(item)['month_kwh'])

    def test_baseline_duplicates_restart_and_counter_priority(self):
        self.sample(self.t)
        self.assertFalse(self.rows('energy_intervals'))
        self.sample(self.t+60,month=10.01)
        self.sample(self.t+61,month=10.02)
        EnergyMonitor(self.manager).record({'month_kwh':10.02,'counter_timezone':'Europe/Prague'},at=self.t+120)
        self.assertEqual(len(self.rows('energy_samples')),3)
        self.assertAlmostEqual(sum(r['kwh'] for r in self.rows('energy_intervals')),.02)
        self.assertTrue(all(r['method']=='measured' for r in self.rows('energy_intervals')))

    def test_outage_recovery_and_reset_do_not_fabricate_energy(self):
        self.sample(self.t)
        self.sample(self.t+60,status='gap')
        self.sample(self.t+600,month=10.1)
        self.assertAlmostEqual(sum(r['kwh'] for r in self.rows('energy_intervals')),.1)
        self.assertEqual(self.rows('energy_intervals')[0]['allocation_estimated'],1)
        self.sample(self.t+660,month=0)
        self.assertEqual(self.rows('energy_events')[0]['phase'],'counter_reset')
        self.sample(self.t+720,month=.01)
        self.assertAlmostEqual(sum(r['kwh'] for r in self.rows('energy_intervals')),.11)

    def test_power_only_is_estimated_and_does_not_bridge_outages(self):
        self.sample(self.t,month=None,day=None)
        self.sample(self.t+60,month=None,day=None)
        row=self.rows('energy_intervals')[0]
        self.assertEqual(row['method'],'estimated')
        self.assertAlmostEqual(row['kwh'],.01)
        self.assertAlmostEqual(row['cost'],.02512)
        self.sample(self.t+600,month=None,day=None)
        self.assertEqual(len(self.rows('energy_intervals')),1)

    def test_identical_cloud_snapshots_without_timestamps_are_not_zero_energy(self):
        self.sample(self.t,power=None)
        self.sample(self.t+60,power=None)
        self.sample(self.t+120,power=None)
        self.assertFalse(self.rows('energy_intervals'))
        self.sample(self.t+600,month=10.01,power=None)
        row=self.rows('energy_intervals')[0]
        self.assertEqual(row['start'],self.t)
        self.assertAlmostEqual(row['kwh'],.01)
        self.assertEqual(row['observed_seconds'],0)
        self.assertIsNone(row['run_h'])

    def test_collector_start_is_idempotent_and_restart_keeps_ledger(self):
        self.sample(self.t)
        def once():self.monitor.stop.set()
        self.monitor.collect=Mock(side_effect=once)
        self.monitor.start()
        self.monitor.thread.join(timeout=2)
        self.assertFalse(self.monitor.thread.is_alive())
        self.assertEqual(self.monitor.collect.call_count,1)
        self.monitor.start()
        self.monitor.thread.join(timeout=2)
        self.assertEqual(self.monitor.collect.call_count,2)
        self.assertEqual(len(EnergyMonitor(self.manager).report('2026-11-20','2026-11-20')['days']),1)

    def test_monitor_event_failure_cannot_fail_a_confirmed_power_command(self):
        with self.manager._db() as db:
            db.execute('''INSERT INTO modules(id,name,room,driver,device_id,mac,ip,version,local_key,power_dp,enabled,created_at,updated_at)
                VALUES('012345abcdef','Zás. infra','','sonoff_ewelink',?,NULL,'','cloud','',1,1,'','')''',(DEVICE_ID,))
        self.manager.ewelink_adapter.command=Mock(return_value={'online':True,'power':True})
        self.monitor.event=Mock(side_effect=sqlite3.OperationalError('test storage unavailable'))
        self.assertTrue(self.manager.command('012345abcdef',{'control':'power','value':True})['power'])
        self.manager.ewelink_adapter.command.assert_called_once()

    def test_tariff_is_split_and_old_costs_are_preserved(self):
        self.sample(self.t)
        self.monitor.tariff('5,000',at=self.t+30)
        self.sample(self.t+60,month=10.02)
        rows=self.rows('energy_intervals')
        self.assertEqual(len(rows),2)
        self.assertAlmostEqual(sum(r['cost'] for r in rows),.07512)
        self.monitor.tariff('8',at=self.t+90)
        self.assertAlmostEqual(sum(r['cost'] for r in self.rows('energy_intervals')),.07512)
        for invalid in ('-1','NaN','inf','1.0000001',True,{},None):
            with self.assertRaises(IoTError):
                self.monitor.tariff(invalid)

    def test_midnight_and_dst_have_correct_prague_days(self):
        a=stamp('2027-03-27T23:30:00')
        b=stamp('2027-03-29T00:30:00')
        self.sample(a,month=10)
        self.sample(b,month=12.4)
        report=self.monitor.report('2027-03-27','2027-03-29')
        self.assertEqual(len(report['days']),3)
        self.assertEqual(report['days'][1]['covered_seconds'],0)
        self.assertAlmostEqual(report['days'][1]['runtime_missing_seconds'],23*3600)
        self.assertAlmostEqual(sum(d['kwh'] for d in report['days']),2.4)
        self.assertIsNone(report['days'][1]['run_h'])

    def test_month_rollover_missing_boundary_is_unknown(self):
        self.sample(stamp('2026-11-30T23:59:00'),month=20,power=None)
        self.sample(stamp('2026-12-01T00:01:00'),month=.01,power=None)
        self.assertFalse(self.rows('energy_intervals'))

    def test_missing_days_are_null_and_bad_ranges_rejected(self):
        report=self.monitor.report('2026-11-20','2026-11-21')
        self.assertIsNone(report['days'][0]['kwh'])
        self.assertIsNone(report['totals']['kwh'])
        self.assertEqual(report['status'],'unavailable')
        for first,last in (('bad','2026-11-20'),('2026-11-19','2026-11-20'),('2026-11-21','2026-11-20')):
            with self.assertRaises(IoTError):self.monitor.report(first,last)

    def test_collection_has_no_device_writes_and_recovers_after_api_failure(self):
        with self.manager._db() as db:
            db.execute('''INSERT INTO modules(id,name,room,driver,device_id,mac,ip,version,local_key,power_dp,enabled,created_at,updated_at)
                VALUES('012345abcdef','Zás. infra','','sonoff_ewelink',?,NULL,'','cloud','',1,1,'','')''',(DEVICE_ID,))
        adapter=Mock()
        adapter.status.side_effect=[IoTError('offline',502,'ewelink_unavailable'),{'online':True,'energy':{'month_kwh':1,'counter_timezone':'Europe/Prague'}}]
        self.manager.ewelink_adapter=adapter
        self.monitor.collect()
        self.assertEqual(self.rows('energy_samples')[0]['status'],'gap')
        # Delete only isolated test sample to allow a second collection in the same minute.
        with self.manager._db() as db:db.execute('DELETE FROM energy_samples')
        self.monitor.collect()
        self.assertEqual(self.rows('energy_samples')[0]['status'],'active')
        adapter.command.assert_not_called()
        lock=threading.Lock();lock.acquire()
        self.manager._locks['012345abcdef']=lock
        self.monitor.collect()
        self.assertEqual(adapter.status.call_count,2)
        lock.release()

    def test_http_csv_and_tariff_and_remote_permissions(self):
        self.sample(self.t)
        self.sample(self.t+60,month=10.01)
        server=make_server('127.0.0.1',0,self.manager)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base+'/api/iot/heating?from=2026-11-20&to=2026-11-20') as response:
                self.assertAlmostEqual(json.load(response)['days'][0]['kwh'],.01)
            with urlopen(base+'/api/iot/heating.csv?from=2026-11-20&to=2026-11-20') as response:
                self.assertIn('0,0100;0,03',response.read().decode('utf-8-sig'))
            request=Request(base+'/api/iot/heating/tariff',data=b'{"czk_per_kwh":"3.5"}',headers={'Content-Type':'application/json'},method='POST')
            with urlopen(request) as response:self.assertTrue(json.load(response)['ok'])
            self.assertTrue(remote_route_allowed('GET','/api/iot/heating'))
            self.assertFalse(remote_route_allowed('POST','/api/iot/heating/tariff'))
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)


if __name__=='__main__':unittest.main()
