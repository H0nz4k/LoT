"""Tapo C320WS read-only LAN RTSP snapshots. Credentials remain server-side."""
import socket
import subprocess
from urllib.parse import quote
from iot_driver import IoTError, utc_now

class LocalCamera:
    def status(self, module):
        try:
            with socket.create_connection((module["ip"], 554), timeout=2):
                pass
        except OSError:
            raise IoTError("Kamera neodpovídá na RTSP portu 554.", 502, "device_unavailable") from None
        return {"online": True, "model": "C320WS", "ip": module["ip"],
                "observed_at": utc_now(), "fault_code": 0, "faults": []}

    def snapshot(self, module):
        self.status(module)
        username = quote(module["tapo_username"], safe="")
        password = quote(module["tapo_password"], safe="")
        url = f"rtsp://{username}:{password}@{module['ip']}:554/stream2"
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "quiet",
                 "-rtsp_transport", "tcp", "-i", url,
                 "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=12, check=False)
        except FileNotFoundError:
            raise IoTError("Na serveru chybí ffmpeg.", 503, "dependency_missing") from None
        except subprocess.TimeoutExpired:
            raise IoTError("Vypršel čas načtení snímku kamery.", 504, "camera_timeout") from None
        if result.returncode != 0 or not result.stdout.startswith(b"\xff\xd8") or len(result.stdout) > 5_000_000:
            raise IoTError("RTSP snímek se nepodařilo načíst. Ověř účet kamery a RTSP.", 502, "camera_unavailable")
        return result.stdout
