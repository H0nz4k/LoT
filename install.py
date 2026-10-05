#!/usr/bin/env python3
"""Instalace nebo aktualizace HanzHub IoT; samostatný Compose projekt."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent


def settings():
    env_file = ROOT / '.env'
    if not env_file.exists():
        shutil.copyfile(ROOT / '.env.example', env_file)
    env_file.chmod(0o600)
    values = {}
    for line in env_file.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep:
            raise ValueError('V .env je neplatný řádek. Použij formát JMENO=hodnota.')
        values[key.strip()] = value.strip().strip('"\'')
    return {**values, **os.environ}


def run(*command):
    subprocess.run(command, cwd=ROOT, check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--with-lcd', action='store_true', help='Nahradí LoRa sekci LCD se zálohou a restartem')
    ap.add_argument('--lcd-script', type=Path, help='Skutečná cesta ke stávajícímu lcd_info.py')
    ap.add_argument('--lcd-service', default='lcd-info.service')
    ap.add_argument('--dashboard-dir', type=Path, help='Statický adresář dashboardu, pokud autodetekce nestačí')
    ap.add_argument('--skip-dashboard', action='store_true')
    args = ap.parse_args()
    try:
        config = settings()
        port = int(config.get('HANZHUB_IOT_PORT', '4011'))
        if not 1 <= port <= 65535:
            raise ValueError('HANZHUB_IOT_PORT musí být v rozsahu 1–65535.')
        source = Path(config.get('TINYTUYA_DEVICES_FILE', '/opt/infrapanel/devices.json'))
        if not source.is_absolute() or not source.is_file():
            raise ValueError('TINYTUYA_DEVICES_FILE musí ukazovat na existující devices.json z TinyTuya wizardu. Uprav .env.')
        rows = json.loads(source.read_text(encoding='utf-8'))
        if not isinstance(rows, list):
            raise ValueError('TinyTuya devices.json musí obsahovat seznam zařízení.')
        run('docker', 'compose', 'version')
        run('docker', 'compose', 'config', '--quiet')
        print('Připravuji samostatnou službu hanzhub_iot…', flush=True)
        run('docker', 'compose', 'build', 'hanzhub_iot')
        run('docker', 'compose', 'up', '-d', 'hanzhub_iot')
        ready = False
        for _ in range(30):
            try:
                with urlopen(f'http://127.0.0.1:{port}/api/iot/health', timeout=2) as response:
                    ready = json.load(response).get('ok') is True
                if ready:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(1)
        if not ready:
            raise ValueError('IoT API není připravené. Zkontroluj: docker compose logs --tail=80 hanzhub_iot')
        failures = []
        if not args.skip_dashboard:
            command = [sys.executable, str(ROOT / 'integrations' / 'dashboard.py'),
                '--api', config.get('HANZHUB_DASHBOARD_API', 'http://127.0.0.1:4010'),
                '--iot-url', config.get('HANZHUB_IOT_URL', f'http://192.168.1.3:{port}'), '--port', str(port)]
            if args.dashboard_dir:
                command += ['--directory', str(args.dashboard_dir)]
            if subprocess.run(command, cwd=ROOT).returncode:
                failures.append('Integrace dashboardu nebyla dokončena. Použij --dashboard-dir /cesta/k/webu.')
        if args.with_lcd:
            command = [sys.executable, str(ROOT / 'lcd' / 'install_lcd.py'), '--service', args.lcd_service]
            if args.lcd_script:
                command += ['--script', str(args.lcd_script)]
            if subprocess.run(command, cwd=ROOT).returncode:
                failures.append('Aktualizace LCD nebyla dokončena. Použij --lcd-script /skutecna/cesta/lcd_info.py.')
        print('IoT služba běží: ' + config.get('HANZHUB_IOT_URL', f'http://192.168.1.3:{port}'))
        if failures:
            for error in failures:
                print(error, file=sys.stderr)
            return 1
        print('Instalace dokončena. Otevři kartu IoT moduly v HanzHubu.')
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'Instalace: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
