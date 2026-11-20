"""Infrapanel energy ledger in the existing SQLite database. No device writes."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import logging
import math
import threading
from zoneinfo import ZoneInfo

from iot_driver import IoTError

DEVICE_ID = '1002a4a7e6'
TZ = ZoneInfo('Europe/Prague')
START = datetime(2026, 11, 20, tzinfo=TZ).timestamp()
LOG = logging.getLogger(__name__)


def number(value, maximum=1e9):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and 0 <= value <= maximum else None
    except (ValueError, TypeError):
        return None


def sonoff_energy(item):
    """UIID 190 uses hundredths for power, voltage, dayKwh and monthKwh.

    Read-only cloud snapshots do not prove the age of instantaneous readings.
    Zero mains voltage is not accepted as a fresh zero-watt measurement.
    """
    params = item.get('params') or {}
    supported = item.get('productModel') == 'S60TPF' and (item.get('extra') or {}).get('uiid') == 190
    def scaled(key, maximum=1e9):
        value = number(params.get(key), maximum * 100)
        return None if value is None else value / 100
    voltage = scaled('voltage', 300) if supported else None
    return {'model': item.get('productModel'), 'supported': supported,
            'power_w': scaled('power', 4000) if voltage is not None and voltage >= 100 else None,
            'day_kwh': scaled('dayKwh') if supported else None,
            'month_kwh': scaled('monthKwh') if supported else None,
            'counter_timezone': params.get('TZ'),
            'history_available': False,
            'source': 'ewelink_cloud_snapshot', 'device_timestamp_available': False}


class EnergyMonitor:
    def __init__(self, manager):
        self.manager = manager
        self.stop = threading.Event()
        self.thread = None
        with manager._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS energy_samples (
                    slot INTEGER PRIMARY KEY, at REAL NOT NULL, status TEXT NOT NULL,
                    power_w REAL, day_kwh REAL, month_kwh REAL, counter_timezone TEXT,
                    model TEXT, reason TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS energy_samples_at ON energy_samples(at);
                CREATE TABLE IF NOT EXISTS energy_intervals (
                    sample_slot INTEGER NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
                    day TEXT NOT NULL, kwh REAL NOT NULL, cost REAL NOT NULL,
                    run_h REAL, method TEXT NOT NULL, allocation_estimated INTEGER NOT NULL,
                    observed_seconds REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(sample_slot,start));
                CREATE INDEX IF NOT EXISTS energy_intervals_day ON energy_intervals(day);
                CREATE TABLE IF NOT EXISTS energy_tariffs (
                    effective REAL PRIMARY KEY, czk_per_kwh TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS energy_events (
                    id INTEGER PRIMARY KEY, at REAL NOT NULL, phase TEXT NOT NULL,
                    message TEXT NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO energy_tariffs VALUES (?,?)', (START, '2.512'))
            columns = {row['name'] for row in db.execute('PRAGMA table_info(energy_intervals)')}
            if 'observed_seconds' not in columns:
                db.execute('ALTER TABLE energy_intervals ADD COLUMN observed_seconds REAL NOT NULL DEFAULT 0')

    def event(self, phase, message, at=None):
        with self.manager._db() as db:
            db.execute('INSERT INTO energy_events(at,phase,message) VALUES(?,?,?)',
                       (at if at is not None else datetime.now(timezone.utc).timestamp(), phase, message))

    def tariff(self, value, at=None):
        try:
            if isinstance(value, bool):
                raise ValueError()
            rate = Decimal(str(value).replace(',', '.'))
            if not rate.is_finite() or not 0 <= rate <= 1000 or rate.as_tuple().exponent < -6:
                raise ValueError()
        except (InvalidOperation, ValueError, TypeError):
            raise IoTError('Cena musí být 0–1000 Kč/kWh, nejvýše šest desetinných míst.') from None
        effective = at if at is not None else datetime.now(timezone.utc).timestamp()
        with self.manager._db() as db:
            db.execute('INSERT INTO energy_tariffs VALUES (?,?)', (effective, str(rate)))

    def collect(self):
        with self.manager._db() as db:
            modules = db.execute('SELECT * FROM modules WHERE device_id=? AND driver=?',
                                 (DEVICE_ID, 'sonoff_ewelink')).fetchall()
        state, reason = {}, ''
        status = 'unavailable'
        if len(modules) == 1 and modules[0]['enabled']:
            module = dict(modules[0])
            # Yield to control/recovery, rather than waiting on its device lock.
            with self.manager._guard:
                lock = self.manager._locks.setdefault(module['id'], threading.Lock())
            if not lock.acquire(blocking=False):
                self.record({}, 'gap', 'Probíhá ovládání zařízení')
                return
            lock.release()
            try:
                state = self.manager.ewelink_adapter.status(module, force=True)
                energy = state.get('energy') or {}
                status = ('gap' if state.get('online') is not True else
                          'active' if energy.get('month_kwh') is not None or energy.get('power_w') is not None
                          else 'unavailable')
                reason = '' if status == 'active' else 'Zařízení offline nebo měření není dostupné'
            except IoTError as error:
                status, reason = 'gap', error.code
        else:
            reason = 'Správná zásuvka není jednoznačně registrovaná nebo je pozastavená'
        self.record(state.get('energy') or {}, status, reason)

    def record(self, energy, status='active', reason='', at=None):
        at = at if at is not None else datetime.now(timezone.utc).timestamp()
        if at < START:
            return
        slot = int(at // 60)
        power = number(energy.get('power_w'), 4000) if status == 'active' else None
        day = number(energy.get('day_kwh')) if status == 'active' else None
        month = number(energy.get('month_kwh')) if status == 'active' else None
        with self.manager._db() as db:
            prev = db.execute('SELECT * FROM energy_samples ORDER BY at DESC LIMIT 1').fetchone()
            if prev and at <= prev['at']:
                return
            inserted = db.execute('INSERT OR IGNORE INTO energy_samples VALUES(?,?,?,?,?,?,?,?,?)',
                                  (slot, at, status, power, day, month, energy.get('counter_timezone'),
                                   energy.get('model'), reason)).rowcount
            if not inserted:
                return
            if not prev or status != 'active':
                return
            # Recover cumulative deltas even across missing polls; keep gaps in the sample ledger.
            baseline = db.execute("SELECT * FROM energy_samples WHERE at < ? AND status='active' ORDER BY at DESC LIMIT 1", (at,)).fetchone()
            if not baseline:
                return
            # A repeated cloud counter without a device timestamp is not evidence
            # of zero use. Accumulate from the first identical snapshot instead
            # of assigning the next counter increment to just the last minute.
            if month is not None and baseline['month_kwh'] is not None:
                changed = db.execute('''SELECT at FROM energy_samples WHERE at<? AND status='active'
                    AND (month_kwh IS NULL OR ABS(month_kwh-?)>0.000000001 OR counter_timezone IS NOT ?)
                    ORDER BY at DESC LIMIT 1''', (at,baseline['month_kwh'],baseline['counter_timezone'])).fetchone()
                baseline = db.execute('''SELECT * FROM energy_samples WHERE at<? AND at>? AND status='active'
                    AND month_kwh=? ORDER BY at LIMIT 1''',
                    (at,changed['at'] if changed else START-1,baseline['month_kwh'])).fetchone() or baseline
            start = baseline['at']
            before, now = datetime.fromtimestamp(start, TZ), datetime.fromtimestamp(at, TZ)
            kwh = None
            method = 'measured'
            if energy.get('counter_timezone') == 'Europe/Prague' and baseline['counter_timezone'] == 'Europe/Prague':
                if month is not None and baseline['month_kwh'] is not None and (before.year, before.month) == (now.year, now.month):
                    kwh = month - baseline['month_kwh']
                elif day is not None and baseline['day_kwh'] is not None and before.date() == now.date():
                    kwh = day - baseline['day_kwh']
            if kwh is not None and (kwh < -1e-9 or kwh > 4 * (at - start) / 3600 + .02):
                db.execute('INSERT INTO energy_events(at,phase,message) VALUES(?,?,?)',
                           (at, 'counter_reset', 'Reset nebo neplatný rozdíl elektroměru; nový základ bez doplnění energie'))
                return
            if kwh is not None and abs(kwh) < 1e-9:
                return
            contiguous = at - start <= 180 and prev['status'] == 'active'
            if kwh is None:
                if not contiguous or power is None or prev['power_w'] is None:
                    return
                start = prev['at']
                kwh = (power + prev['power_w']) / 2 * (at - start) / 3600000
                method = 'estimated'
            kwh = max(0, kwh)
            tariffs = db.execute('SELECT * FROM energy_tariffs ORDER BY effective').fetchall()
            cuts = {start, at}
            midnight = datetime.combine(before.date() + timedelta(days=1), datetime.min.time(), TZ)
            while midnight.timestamp() < at:
                cuts.add(midnight.timestamp())
                midnight += timedelta(days=1)
            cuts.update(t['effective'] for t in tariffs if start < t['effective'] < at)
            cuts = sorted(cuts)
            run_h = ((at-start)/3600 if power > 5 and prev['power_w'] > 5 else 0) if contiguous and power is not None and prev['power_w'] is not None else None
            for left, right in zip(cuts, cuts[1:]):
                fraction = (right-left)/(at-start)
                rate = next((Decimal(t['czk_per_kwh']) for t in reversed(tariffs) if t['effective'] <= left), Decimal('2.512'))
                part = kwh * fraction
                db.execute('INSERT INTO energy_intervals VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (slot, left, right, datetime.fromtimestamp(left, TZ).date().isoformat(), part,
                            float(Decimal(str(part)) * rate), None if run_h is None else run_h*fraction,
                            method, int(len(cuts)>2 or not contiguous),right-left if contiguous else 0))

    def report(self, first, last):
        try:
            first = datetime.strptime(first, '%Y-%m-%d').date()
            last = datetime.strptime(last, '%Y-%m-%d').date()
            if first < datetime.fromtimestamp(START,TZ).date() or last < first or (last-first).days > 3660:
                raise ValueError()
        except (ValueError, TypeError):
            raise IoTError('Zvolte období od 20. 11. 2026, nejvýše 10 let.') from None
        left = datetime.combine(first, datetime.min.time(), TZ).timestamp()
        right = datetime.combine(last+timedelta(days=1), datetime.min.time(), TZ).timestamp()
        now = datetime.now(timezone.utc).timestamp()
        with self.manager._db() as db:
            rows = {r['day']:dict(r) for r in db.execute('''SELECT day, SUM(kwh) kwh,SUM(cost) cost,
                SUM(run_h) run_h,SUM(observed_seconds) covered_seconds, SUM(CASE WHEN run_h IS NULL THEN end-start ELSE 0 END) runtime_missing_seconds,
                MAX(method='estimated') estimated,MAX(allocation_estimated) allocation_estimated
                FROM energy_intervals WHERE day>=? AND day<=? GROUP BY day''', (first.isoformat(),last.isoformat()))}
            latest = db.execute('SELECT * FROM energy_samples ORDER BY at DESC LIMIT 1').fetchone()
            success = db.execute("SELECT at FROM energy_samples WHERE status='active' ORDER BY at DESC LIMIT 1").fetchone()
            bucket = 3600 if (last-first).days <= 31 else 86400
            power_rows = {int(r['at']):dict(r) for r in db.execute('''SELECT CAST(at/? AS INTEGER)*? at,AVG(power_w) power_w,
                COUNT(power_w) valid,COUNT(*) count FROM energy_samples WHERE at>=? AND at<? GROUP BY CAST(at/? AS INTEGER)''', (bucket,bucket,left,right,bucket))}
            events = [dict(r) for r in db.execute('SELECT * FROM energy_events WHERE at>=? AND at<? ORDER BY at DESC LIMIT 200', (left,right))]
            tariffs = [dict(r) for r in db.execute('SELECT * FROM energy_tariffs ORDER BY effective')]
            totals = dict(db.execute("SELECT SUM(kwh) kwh,SUM(cost) cost,SUM(run_h) run_h,MIN(start) since,MAX(method='estimated') estimated FROM energy_intervals").fetchone())
            today = datetime.now(TZ).date()
            overview = {}
            for key, start_day, end_day in (
                    ('today',today,today), ('yesterday',today-timedelta(days=1),today-timedelta(days=1)),
                    ('month',today.replace(day=1),today)):
                overview[key] = dict(db.execute('SELECT SUM(kwh) kwh,SUM(cost) cost,SUM(run_h) run_h,MAX(method=\'estimated\') estimated FROM energy_intervals WHERE day>=? AND day<=?',
                                               (start_day.isoformat(),end_day.isoformat())).fetchone())
        days=[]
        power=[]
        for at in range(int(left//bucket)*bucket,int(min(right,now)),bucket):
            row=power_rows.get(at,{'at':at,'power_w':None,'valid':0,'count':0})
            row['partial']=row['valid']<max(1,(min(at+bucket,right,now)-max(at,left))/60)
            power.append(row)
        cursor=first
        while cursor<=last:
            key=cursor.isoformat()
            row=rows.get(key, {'day':key,'kwh':None,'cost':None,'run_h':None,'covered_seconds':0,'runtime_missing_seconds':0,'estimated':False,'allocation_estimated':False})
            day_start=datetime.combine(cursor,datetime.min.time(),TZ).timestamp()
            day_end=datetime.combine(cursor+timedelta(days=1),datetime.min.time(),TZ).timestamp()
            row['missing_seconds']=max(0,min(now,day_end)-day_start-row['covered_seconds']) if now>day_start else 0
            days.append(row)
            cursor+=timedelta(days=1)
        status=latest['status'] if latest else 'unavailable'
        if latest and now-latest['at']>180:
            status='gap'
        return {'device_id':DEVICE_ID,'timezone':'Europe/Prague','status':status,
                'last_success':success['at'] if success else None,'latest':dict(latest) if latest else None,
                'days':days,'power':power,'events':events,'tariffs':tariffs,'totals':totals,'overview':overview,
                'current_power_w':latest['power_w'] if latest and status=='active' and now-latest['at']<=180 else None,
                'history_import':'unavailable: no timestamped history in read-only API snapshot',
                'source':'ewelink_cloud_snapshot','instantaneous_freshness':'device timestamp unavailable'}

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        def loop():
            while not self.stop.is_set():
                try:
                    self.collect()
                except Exception:
                    LOG.error('Energy collection failed; retry in 60 seconds')
                    try:
                        self.record({}, 'error', 'Chyba sběru; viz diagnostika služby')
                    except Exception:
                        LOG.error('Energy ledger write failed')
                self.stop.wait(60)
        self.thread=threading.Thread(target=loop,name='infra-energy',daemon=True)
        self.thread.start()
