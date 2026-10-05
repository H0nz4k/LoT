#!/usr/bin/env python3
"""Idempotentní napojení samostatné IoT služby na stávající HanzHub dashboard."""
import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ASSETS = Path(__file__).resolve().parent
NAV_MARKER = "<!-- HANZHUB_IOT_NAV -->"
STYLE_MARKER = "/* HANZHUB_IOT_NAV */"
NAV = '''<!-- HANZHUB_IOT_NAV -->
      <nav class="hanzhub-iot-nav" aria-label="HanzHub">
        <a href="index.html" aria-current="page">Služby</a><a href="iot.html">IoT</a>
      </nav>
      <!-- /HANZHUB_IOT_NAV -->'''
STYLE = '''
    /* HANZHUB_IOT_NAV */
    .hanzhub-iot-nav { display:flex; gap:4px; padding:4px; border:1px solid rgba(255,255,255,.12); border-radius:12px; background:rgba(255,255,255,.03); }
    .hanzhub-iot-nav a { padding:7px 12px; border-radius:8px; color:#aab4c5; text-decoration:none; font-size:13px; }
    .hanzhub-iot-nav a[aria-current], .hanzhub-iot-nav a:hover { color:#e9edf5; background:rgba(255,255,255,.08); }
    .hanzhub-iot-nav a:focus-visible { outline:2px solid #32dc82; outline-offset:2px; }
    @media(max-width:620px) { .top-bar { flex-wrap:wrap; } .hanzhub-iot-nav { margin-left:auto; } }
    /* /HANZHUB_IOT_NAV */
'''


def patch_html(text):
    if NAV_MARKER in text:
        return text
    anchor = '<span class="top-bar-spacer"></span>'
    if anchor not in text or '</style>' not in text:
        raise ValueError("Neznámá struktura dashboardu. Očekávána aktuální stránka HanzHub s top-bar-spacer.")
    text = text.replace(anchor, anchor + '\n      ' + NAV, 1)
    return text.replace('</style>', STYLE + '  </style>', 1)


def detect_directory():
    result = subprocess.run(['docker', 'inspect', 'hanzhub_dashboard', '--format',
        '{{range .Mounts}}{{if eq .Destination "/usr/share/nginx/html"}}{{.Source}}{{end}}{{end}}'],
        capture_output=True, text=True, timeout=15)
    if result.returncode or not result.stdout.strip():
        raise ValueError("Nelze najít dashboard. Použij --directory /cesta/k/dashboardu obsahující index.html.")
    return Path(result.stdout.strip())


def install_navigation(directory):
    directory = Path(directory).resolve()
    index = directory / 'index.html'
    original = index.read_text(encoding='utf-8')
    changed = patch_html(original)
    backup = None
    if changed != original:
        # Zálohy jsou mimo statický web, aby se nepublikovaly s obsahem dashboardu.
        backup_dir = directory.parent / (directory.name + '-iot-backups')
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = backup_dir / ('index-' + stamp + '.html')
        shutil.copy2(index, backup)
        index.write_text(changed, encoding='utf-8')
    (directory / 'icons').mkdir(exist_ok=True)
    shutil.copyfile(ASSETS / 'iot.svg', directory / 'icons' / 'iot.svg')
    shutil.copyfile(ASSETS / 'iot-entry.html', directory / 'iot.html')
    return backup


def register_card(api, iot_url, port):
    icon = "data:image/svg+xml;base64," + base64.b64encode((ASSETS / 'iot.svg').read_bytes()).decode('ascii')
    def refresh_icon(services):
        existing = next((item for item in services if item.get('key') == 'iot'), None)
        if existing is None:
            return False
        if existing.get('icon') != icon:
            request = Request(api.rstrip('/') + '/api/services/iot', method='PUT',
                data=json.dumps({'icon':icon}).encode('utf-8'), headers={'Content-Type':'application/json'})
            with urlopen(request, timeout=5) as response:
                if not json.load(response).get('ok'):
                    raise ValueError('Dashboard nepotvrdil obnovu ikony.')
        return True
    with urlopen(api.rstrip('/') + '/api/services', timeout=5) as response:
        services = json.load(response).get('services', [])
    if refresh_icon(services):
        return False
    card = {'key':'iot', 'name':'IoT moduly', 'desc':'Ovládání a správa chytrých zařízení',
            'url':iot_url, 'ping':f'http://127.0.0.1:{port}/api/iot/health',
            'icon':icon, 'color':'linear-gradient(180deg,#fb923c,#f59e0b)', 'public':False}
    request = Request(api.rstrip('/') + '/api/services', method='POST',
        data=json.dumps(card).encode('utf-8'), headers={'Content-Type':'application/json'})
    try:
        with urlopen(request, timeout=5) as response:
            if not json.load(response).get('ok'):
                raise ValueError('Dashboard nepotvrdil vytvoření karty.')
    except HTTPError as error:
        # Kartu mohl mezitím zaregistrovat právě spuštěný IoT kontejner.
        if error.code == 400:
            with urlopen(api.rstrip('/') + '/api/services', timeout=5) as response:
                if refresh_icon(json.load(response).get('services', [])):
                    return False
        raise
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--directory', type=Path)
    ap.add_argument('--api', default='http://127.0.0.1:4010')
    ap.add_argument('--iot-url', default='http://192.168.1.3:4011')
    ap.add_argument('--port', type=int, default=4011)
    args = ap.parse_args()
    try:
        directory = args.directory or detect_directory()
        backup = install_navigation(directory)
        print(f'Navigace IoT a ikona připraveny: {directory}')
        if backup:
            print(f'Záloha původní stránky: {backup}')
        added = register_card(args.api, args.iot_url, args.port)
        print('Karta IoT přidána.' if added else 'Karta IoT již existuje; ikona aktualizována, ostatní nastavení zachováno.')
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'Integrace dashboardu: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
