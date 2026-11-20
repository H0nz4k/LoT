"""Isolated browser-test fixture; never starts scheduler, collector or devices."""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'iot'))
from iot import IoTManager
from energy import TZ
from server import make_server

if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory:
        manager=IoTManager(Path(directory)/'data',Path(directory)/'missing.json')
        now=datetime.now(TZ)
        at=(now-timedelta(minutes=8)).timestamp()
        for index in range(8):
            manager.energy.record({'power_w':600,'month_kwh':10+index*.01,
                                   'day_kwh':1+index*.01,'model':'S60TPF',
                                   'counter_timezone':'Europe/Prague'},at=at+index*60)
        server=make_server('127.0.0.1',0,manager)
        print(server.server_port,flush=True)
        server.serve_forever()
