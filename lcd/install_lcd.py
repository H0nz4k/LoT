#!/usr/bin/env python3
"""Výměna LoRa sekce na existujícím LCD se zálohou původních souborů."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parent


def detect_script(service):
    result = subprocess.run(['systemctl', 'show', service, '--property=ExecStart', '--value'],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise ValueError(f'Nelze načíst {service}. Zadej --script /skutecna/cesta/lcd_info.py.')
    paths = re.findall(r'(/[^\s;"\']*/lcd_info\.py)(?:\s|;|$)', result.stdout)
    if not paths:
        raise ValueError(f'V ExecStart služby {service} není absolutní cesta k lcd_info.py. Použij --script.')
    return Path(paths[0]).resolve()


def install_files(script):
    script = Path(script).resolve()
    if not script.is_file() or script.name != 'lcd_info.py':
        raise ValueError('Cíl musí být existující lcd_info.py používaný tvým LCD.')
    if script == SOURCE / 'lcd_info.py':
        raise ValueError('Zadej skutečný LCD skript, nikoli zdrojovou kopii v tomto repozitáři.')
    text = script.read_text(encoding='utf-8')
    if 'FBIOGET_VSCREENINFO' not in text or 'read_last_valid_meteo' not in text:
        raise ValueError('LCD skript neodpovídá poskytnuté framebuffer/Meteo verzi. Instalace zastavena.')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = script.parent / ('lcd-backup-' + stamp)
    backup.mkdir(mode=0o700)
    for name in ('infrapanel_widget.py', 'lcd_info.py'):
        target = script.parent / name
        if target.exists():
            shutil.copy2(target, backup / name)
        # Nahrazení souboru v témže adresáři je atomické; zachová se původní režim skriptu.
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
        temporary = target.with_name(target.name + '.iot-new')
        shutil.copyfile(SOURCE / name, temporary)
        temporary.chmod(mode)
        temporary.replace(target)
    return backup


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--service', default='lcd-info.service')
    ap.add_argument('--script', type=Path)
    ap.add_argument('--no-restart', action='store_true')
    args = ap.parse_args()
    try:
        script = args.script or detect_script(args.service)
        backup = install_files(script)
        print(f'LCD aktualizováno: {script}')
        print(f'Záloha: {backup}')
        if not args.no_restart:
            subprocess.run(['systemctl', 'restart', args.service], check=True, timeout=30)
            subprocess.run(['systemctl', 'is-active', '--quiet', args.service], check=True, timeout=10)
            print(f'{args.service} běží. LoRa sekci nahrazuje Infrapanel.')
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'Instalace LCD: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
