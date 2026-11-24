"""Explicit Sonoff power recovery. OFF is never replayed; interrupted work restores ON only."""
from contextlib import contextmanager
import logging
import os
import sqlite3
import threading
import time
import uuid

from energy import DEVICE_ID
from iot_driver import IoTError

LOG = logging.getLogger(__name__)
TERMINAL = ('completed', 'restored', 'failed')


@contextmanager
def process_lock(path):
    """Kernel-released lock survives neither process death nor a machine restart."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'0')
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise IoTError('Obnova již běží v jiném procesu.', 409, 'recovery_busy') from None
        yield
    finally:
        os.close(fd)


class PowerRecovery:
    def __init__(self, manager):
        self.manager = manager
        self._gate = threading.RLock()
        self._busy = False
        self._generation = 0
        self.thread = None
        self.sleep = time.sleep
        with manager._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS infra_recovery (
                    id TEXT PRIMARY KEY, module_id TEXT NOT NULL, device_id TEXT NOT NULL,
                    phase TEXT NOT NULL, message TEXT NOT NULL,
                    off_intent INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    requested_at REAL NOT NULL, updated_at REAL NOT NULL, restore_after REAL);
                CREATE UNIQUE INDEX IF NOT EXISTS infra_recovery_single_active ON infra_recovery((1))
                    WHERE phase NOT IN ('completed','restored','failed');
            ''')

    def snapshot(self):
        with self._gate:
            return self._generation, self._busy

    def _row(self, operation_id=None):
        with self.manager._db() as db:
            row = (db.execute('SELECT * FROM infra_recovery WHERE id=?', (operation_id,)).fetchone()
                   if operation_id else db.execute('SELECT * FROM infra_recovery ORDER BY requested_at DESC LIMIT 1').fetchone())
        return dict(row) if row else None

    def status(self):
        row = self._row()
        return {'device_id': DEVICE_ID, 'operation': row,
                'busy': self.snapshot()[1] or bool(row and row['phase'] not in TERMINAL),
                'confirmation': 'Cloudové potvrzení není fyzické ověření relé.'}

    def monitor_snapshot(self):
        row = self._row()
        generation, busy = self.snapshot()
        return (generation, row['id'] if row else None, row['updated_at'] if row else None), busy or bool(row and row['phase'] not in TERMINAL)

    def _set(self, operation_id, phase, message, **fields):
        # Internal fields only; every intent and phase event share a transaction.
        assert set(fields) <= {'off_intent', 'attempts', 'restore_after'}
        at = time.time()
        with self.manager._db() as db:
            db.execute('UPDATE infra_recovery SET phase=?,message=?,updated_at=?' +
                       ''.join(',' + name + '=?' for name in fields) + ' WHERE id=?',
                       (phase, message, at, *fields.values(), operation_id))
            db.execute('INSERT INTO energy_events(at,phase,message) VALUES(?,?,?)',
                       (at, 'recovery_' + phase, operation_id + ': ' + message))

    def _best_effort_set(self, operation_id, phase, message, **fields):
        try:
            self._set(operation_id, phase, message, **fields)
        except (sqlite3.Error, OSError):
            LOG.error('Cannot journal infra recovery phase %s; ON restoration remains required', phase)

    def start(self, request_id, device_id, confirmation):
        try:
            if str(uuid.UUID(request_id)) != request_id or uuid.UUID(request_id).version != 4:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise IoTError('Požadován nový identifikátor UUID v4.') from None
        if device_id != DEVICE_ID or confirmation != 'power-cycle-30s':
            raise IoTError('Potvrďte 30sekundové vypnutí správné zásuvky.', 409, 'recovery_confirmation')
        with self._gate:
            previous = self._row(request_id)
            if previous:
                return previous  # Duplicate HTTP delivery cannot produce another OFF.
            if self._busy:
                raise IoTError('Předchozí obnova ještě dokončuje bezpečné ukončení.',409,'recovery_busy')
            with process_lock(self.manager.data_dir / 'infra-recovery.lock'):
                with self.manager._db() as db:
                    modules = db.execute("SELECT * FROM modules WHERE device_id=? AND driver='sonoff_ewelink' AND enabled=1", (DEVICE_ID,)).fetchall()
                    if len(modules) != 1:
                        raise IoTError('Identita zásuvky není jednoznačná nebo je pozastavená.', 409, 'recovery_identity')
                    at = time.time()
                    try:
                        db.execute('INSERT INTO infra_recovery(id,module_id,device_id,phase,message,requested_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                                   (request_id, modules[0]['id'], DEVICE_ID, 'started', 'Čeká na ověření identity a stavu', at, at))
                        db.execute('INSERT INTO energy_events(at,phase,message) VALUES(?,?,?)',
                                   (at, 'recovery_started', request_id + ': Zahájení obnovy připojení'))
                    except sqlite3.IntegrityError:
                        raise IoTError('Nejprve dokončete předchozí obnovu nebo obnovte zapnutý stav.', 409, 'recovery_busy') from None
            self._launch(request_id)
            return self._row(request_id)

    def retry_restore(self, operation_id, confirmation):
        if not isinstance(operation_id, str) or len(operation_id) != 36:
            raise IoTError('Neplatný identifikátor obnovy.')
        if confirmation != 'restore-on':
            raise IoTError('Potvrďte pouze obnovení zapnutého stavu.', 409, 'recovery_confirmation')
        with self._gate:
            row = self._row(operation_id)
            if not row or not row['off_intent']:
                raise IoTError('Není evidována obnova se záměrem vypnutí.', 409, 'recovery_not_pending')
            if row['phase'] in TERMINAL:
                return row
            if row['phase'] != 'needs_review' and not self._busy:
                raise IoTError('Operace ještě běží; vyčkejte na její výsledek.', 409, 'recovery_busy')
            self._launch(operation_id, restore_only=True)
            return self._row(operation_id)

    def resume(self):
        """Startup can only restore a durable earlier OFF intent, never start a new cycle."""
        row = self._row()
        if row and row['phase'] not in TERMINAL:
            self._launch(row['id'], restore_only=True)

    def _launch(self, operation_id, restore_only=False):
        with self._gate:
            if self._busy:
                return
            self._busy = True
            self._generation += 1
            self.thread = threading.Thread(target=self._execute, args=(operation_id, restore_only),
                                           name='infra-recovery', daemon=True)
            try:
                self.thread.start()
            except RuntimeError:
                self._busy = False
                self._generation += 1
                self._best_effort_set(operation_id, 'needs_review' if restore_only else 'failed', 'Nelze spustit pracovní vlákno; žádný nový příkaz nebyl odeslán')
                raise IoTError('Nelze spustit obnovu; zkontrolujte stav služby.',503,'recovery_worker') from None

    def _verify(self, row, expected=None):
        module = self.manager._module(row['module_id'])
        if row['device_id'] != DEVICE_ID or module['device_id'] != DEVICE_ID or module['driver'] != 'sonoff_ewelink' or not module['enabled']:
            raise IoTError('Identita nebo povolení zásuvky se změnily.', 409, 'recovery_identity')
        items = [item for item in self.manager.ewelink_adapter.client.things(force=True)
                 if item.get('deviceid') == DEVICE_ID]
        if len(items) != 1:
            raise IoTError('Nejednoznačná cloudová identita.', 409, 'recovery_identity')
        item = items[0]
        params = item.get('params') or {}
        switches = params.get('switches')
        pulses = params.get('pulses')
        if (item.get('productModel') != 'S60TPF' or (item.get('extra') or {}).get('uiid') != 190
                or str(item.get('brandName', '')).casefold() not in ('sonoff', 'coolkit')
                or not isinstance(switches, list) or len(switches) != 1
                or type(switches[0].get('outlet')) is not int or switches[0].get('outlet') != 0 or 'switch' in params
                or not isinstance(pulses,list) or len(pulses)!=1
                or pulses[0].get('outlet')!=0 or pulses[0].get('pulse')!='off'):
            raise IoTError('Model nebo protokol zásuvky nesouhlasí, případně je aktivní pulzní režim.', 409, 'recovery_identity')
        if item.get('online') is not True:
            raise IoTError('Zásuvka je offline.', 409, 'recovery_offline')
        power = self.manager.ewelink_adapter.client.power(params)
        if power is None or (expected is not None and power is not expected):
            raise IoTError('Požadovaný stav zásuvky nebyl potvrzen.', 502, 'not_confirmed')
        return module, power

    def _restore(self, row):
        for attempt in range(1, 4):
            self._best_effort_set(row['id'], 'restoring', 'Ověřuji a obnovuji zapnutý stav', attempts=row['attempts'] + attempt)
            try:
                module, power = self._verify(row)
                if power is not True:
                    self.manager.ewelink_adapter.command(module, {'control': 'power', 'value': True})
                self._verify(row, expected=True)
                with self.manager._guard:
                    self.manager._cache.pop(row['module_id'], None)
                return True
            except Exception:
                # Never expose transport payloads or credentials through the journal.
                self._best_effort_set(row['id'], 'restore_pending', 'Zapnutí nebylo potvrzeno; další bezpečný pokus pouze ON')
                if attempt < 3:
                    self.sleep(2)
        return False

    def _execute(self, operation_id, restore_only):
        try:
            with process_lock(self.manager.data_dir / 'infra-recovery.lock'):
                row = self._row(operation_id)
                if not row or row['phase'] in TERMINAL:
                    return
                with self.manager._device_lock(row['module_id']):
                    restore = bool(row['off_intent'])
                    interrupted = restore_only
                    try:
                        if restore_only:
                            if not restore:
                                self._set(operation_id, 'failed', 'Přerušeno před záměrem vypnutí; žádný příkaz se neopakuje')
                                return
                        else:
                            module, _ = self._verify(row, expected=True)
                            self._set(operation_id, 'off_intent', 'Identita ověřena; jediný záměr vypnutí', off_intent=1)
                            restore = True
                            with self.manager._guard:
                                self.manager._cache.pop(row['module_id'], None)
                            self.manager.ewelink_adapter.command(module, {'control': 'power', 'value': False})
                            self._verify(row, expected=False)
                            self._set(operation_id, 'off', 'Vypnutí potvrzeno cloudem; čekám 30 sekund', restore_after=time.time()+30)
                            self.sleep(30)
                    except Exception:
                        interrupted = True
                        if not restore:
                            self._best_effort_set(operation_id, 'failed', 'Kontrola nebo zápis záměru selhaly před vypnutím; zásuvka se neovládá')
                        else:
                            self._best_effort_set(operation_id, 'restore_pending', 'Průběh přerušen nebo vypnutí nepotvrzeno; obnovuji pouze ON')
                    finally:
                        if restore:
                            restored = self._restore(row)
                            phase = ('restored' if interrupted else 'completed') if restored else 'needs_review'
                            message = ('Zapnutí potvrzeno cloudem po přerušené obnově' if interrupted else '30sekundová obnova dokončena; zapnutí potvrzeno cloudem') if restored else 'Zapnutí není potvrzené. Nutná kontrola; další cyklus OFF je blokován.'
                            self._best_effort_set(operation_id, phase, message)
        except IoTError as error:
            if error.code != 'recovery_busy':
                row = self._row(operation_id)
                phase = 'needs_review' if row and row['off_intent'] else 'failed'
                self._best_effort_set(operation_id, phase, 'Obnova nemohla získat zámek zařízení; žádný nový příkaz nebyl odeslán')
        except Exception:
            LOG.error('Infra recovery worker failed; durable journal requires review')
        finally:
            with self._gate:
                self._busy = False
                self._generation += 1
