#!/usr/bin/env python3
"""Read available Tuya DPS directly in LAN. This tool never writes a DP."""
import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "iot"))
from local_keys import KeyFileError, local_key


def public_dps(response):
    if not isinstance(response, dict) or "Err" in response or "Error" in response:
        raise ValueError("Device did not return valid DPS")
    values = response.get("dps", response.get("data", {}).get("dps"))
    if not isinstance(values, dict):
        raise ValueError("Missing DPS")
    # Unknown string-valued DPs may contain private values. Report only type.
    return {str(dp): {"type": type(value).__name__,
                      "value": value if type(value) in (bool, int, float) else "[redacted]"}
            for dp, value in values.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--ip", required=True)
    parser.add_argument("--port", type=int, default=6668)
    parser.add_argument("--version", type=float, default=3.4)
    args = parser.parse_args()
    logging.getLogger("tinytuya").disabled = True
    try:
        import tinytuya
        key = local_key({"device_id": args.device_id, "local_key": ""})
        if not key:
            raise KeyFileError("Configure HANZHUB_TUYA_KEY_DIR")
        panel = tinytuya.Device(args.device_id, args.ip, key, version=args.version, port=args.port)
        panel.set_socketTimeout(5)
        panel.set_socketRetryLimit(1)
        before = public_dps(panel.status())
        available = panel.detect_available_dps()
        if not isinstance(available, dict) or not available:
            raise ValueError("DPS discovery returned no valid data")
        after = public_dps(panel.status())
        print(json.dumps({"observed_at": datetime.now(timezone.utc).isoformat(),
                          "client_version": tinytuya.__version__, "protocol": args.version,
                          "method": "status + read-only detect_available_dps + status",
                          "available_dps": sorted(available, key=int),
                          "before": before, "after": after}, indent=2))
        return 0
    except Exception:
        # Neither exception text nor library payloads should disclose a key.
        print("Read-only probe failed; check connection and local key without enabling secret debug logs.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
