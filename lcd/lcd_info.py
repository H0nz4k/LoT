#!/usr/bin/env python3
import argparse
import ctypes
import fcntl
import json
import mmap
import os
import re
import time
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont
import psutil
from infrapanel_widget import PanelPoller, draw_panel_section

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602

class fb_bitfield(ctypes.Structure):
    _fields_ = [("offset", ctypes.c_uint32), ("length", ctypes.c_uint32), ("msb_right", ctypes.c_uint32)]

class fb_var_screeninfo(ctypes.Structure):
    _fields_ = [
        ("xres", ctypes.c_uint32), ("yres", ctypes.c_uint32),
        ("xres_virtual", ctypes.c_uint32), ("yres_virtual", ctypes.c_uint32),
        ("xoffset", ctypes.c_uint32), ("yoffset", ctypes.c_uint32),
        ("bits_per_pixel", ctypes.c_uint32), ("grayscale", ctypes.c_uint32),
        ("red", fb_bitfield), ("green", fb_bitfield), ("blue", fb_bitfield), ("transp", fb_bitfield),
        ("nonstd", ctypes.c_uint32), ("activate", ctypes.c_uint32),
        ("height", ctypes.c_uint32), ("width", ctypes.c_uint32),
        ("accel_flags", ctypes.c_uint32),
        ("pixclock", ctypes.c_uint32),
        ("left_margin", ctypes.c_uint32), ("right_margin", ctypes.c_uint32),
        ("upper_margin", ctypes.c_uint32), ("lower_margin", ctypes.c_uint32),
        ("hsync_len", ctypes.c_uint32), ("vsync_len", ctypes.c_uint32),
        ("sync", ctypes.c_uint32), ("vmode", ctypes.c_uint32),
        ("rotate", ctypes.c_uint32),
        ("colorspace", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 4),
    ]

class fb_fix_screeninfo(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_char * 16),
        ("smem_start", ctypes.c_ulong),
        ("smem_len", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("type_aux", ctypes.c_uint32),
        ("visual", ctypes.c_uint32),
        ("xpanstep", ctypes.c_uint16),
        ("ypanstep", ctypes.c_uint16),
        ("ywrapstep", ctypes.c_uint16),
        ("line_length", ctypes.c_uint32),
        ("mmio_start", ctypes.c_ulong),
        ("mmio_len", ctypes.c_uint32),
        ("accel", ctypes.c_uint32),
        ("accel_flags", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint16),
        ("reserved", ctypes.c_uint16 * 2),
    ]

def fb_ioctl_struct(fd: int, req: int, struct_obj):
    buf = bytearray(ctypes.sizeof(struct_obj))
    fcntl.ioctl(fd, req, buf, True)
    ctypes.memmove(ctypes.addressof(struct_obj), bytes(buf), ctypes.sizeof(struct_obj))
    return struct_obj

def get_fb_info(fb_path: str):
    fd = os.open(fb_path, os.O_RDWR)
    try:
        v = fb_ioctl_struct(fd, FBIOGET_VSCREENINFO, fb_var_screeninfo())
        f = fb_ioctl_struct(fd, FBIOGET_FSCREENINFO, fb_fix_screeninfo())
        return int(v.xres), int(v.yres), int(v.bits_per_pixel), int(f.line_length), int(f.smem_len)
    finally:
        os.close(fd)

def rgb_to_rgb565_bytes(img: Image.Image) -> bytes:
    if img.mode != "RGB":
        img = img.convert("RGB")
    px = img.tobytes()
    out = bytearray((len(px) // 3) * 2)
    j = 0
    for i in range(0, len(px), 3):
        r, g, b = px[i], px[i + 1], px[i + 2]
        rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out[j] = rgb565 & 0xFF
        out[j + 1] = (rgb565 >> 8) & 0xFF
        j += 2
    return bytes(out)

def rotate_image(img: Image.Image, deg: int) -> Image.Image:
    deg = deg % 360
    if deg == 0:
        return img
    return img.rotate(deg, expand=True)  # CCW

def format_bytes(n: float) -> str:
    for unit in ["B","KB","MB","GB","TB"]:
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"

def get_cpu_temp_c():
    for p in ("/sys/class/thermal/thermal_zone0/temp", "/sys/devices/virtual/thermal/thermal_zone0/temp"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return float(f.read().strip()) / 1000.0
        except Exception:
            pass
    return None

def get_uptime_str():
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            secs = float(f.read().split()[0])
        m, _ = divmod(int(secs), 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        return f"{d}d {h:02d}:{m:02d}" if d > 0 else f"{h:02d}:{m:02d}"
    except Exception:
        return "?"

def get_iface_ip(iface: str) -> str:
    try:
        addrs = psutil.net_if_addrs().get(iface, [])
        for a in addrs:
            if getattr(a, "family", None) == 2 and a.address:  # AF_INET
                return a.address
    except Exception:
        pass
    return "(no ip)"

# --- METEO robust parsing ---

_INF_NAN_RE = re.compile(r'(?<!")\b(?:inf|-inf|nan)\b(?!")', re.IGNORECASE)

def _sanitize_jsonish(s: str) -> str:
    # replace bare inf/nan tokens with null so json.loads() won't fail
    return _INF_NAN_RE.sub("null", s)

def _try_parse_line(line: str):
    """
    Accepts:
    ISO_TS,{json...}
    and tolerates inf/nan and partially broken tail lines by skipping them.
    Returns dict or None.
    """
    if not line or "," not in line:
        return None
    try:
        ts_s, json_s = line.split(",", 1)
        json_s = json_s.strip()

        # must at least start with { and contain a closing }
        if not json_s.startswith("{") or "}" not in json_s:
            return None
        # cut to last closing brace to avoid trailing garbage
        json_s = json_s[:json_s.rfind("}") + 1]
        json_s = _sanitize_jsonish(json_s)

        d = json.loads(json_s)

        ts = None
        try:
            ts = datetime.fromisoformat(ts_s)
        except Exception:
            ts = None

        # require at least one of the keys to exist to consider it "meteo"
        if ("temp" not in d) and ("vbat" not in d) and ("soc" not in d):
            return None

        return {
            "temp": d.get("temp"),
            "vbat": d.get("vbat"),
            "soc": d.get("soc"),
            "ts": ts,
        }
    except Exception:
        return None

def read_last_valid_meteo(path: str, max_bytes: int = 262144):
    """
    Reads last chunk of file and returns the last *valid* meteo record.
    This avoids the common issue of last line being mid-write / broken.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            if end <= 0:
                return None
            read_size = min(end, max_bytes)
            f.seek(-read_size, os.SEEK_END)
            data = f.read(read_size)

        lines = data.splitlines()
        for b in reversed(lines):
            line = b.decode("utf-8", errors="ignore").strip()
            rec = _try_parse_line(line)
            if rec:
                return rec
        return None
    except Exception:
        return None

def fmt_last(ts: datetime | None) -> str:
    return "??:?? ??:??:??" if not ts else ts.strftime("%H:%M %d.%m.%y")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fb", default="/dev/fb0")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--rotate", type=int, default=0, help="0/90/180/270 (CCW)")
    ap.add_argument("--font", default="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    ap.add_argument("--meteo_csv", default="/opt/meteo3/meteo_log.csv")
    ap.add_argument("--meteo_refresh", type=float, default=2.0)
    # Staré argumenty z ExecStart zůstávají kompatibilní; LoRa sekci nahrazuje panel.
    ap.add_argument("--lora_www", default="/opt/lora2/www", help=argparse.SUPPRESS)
    ap.add_argument("--lora_refresh", type=float, default=2.0, help=argparse.SUPPRESS)
    ap.add_argument("--infrapanel_api", default="http://127.0.0.1:4011/api/iot/panel")
    ap.add_argument("--infrapanel_refresh", type=float, default=5.0)
    ap.add_argument("--infrapanel_id", default=None, help="Volitelný identifikátor modulu z IoT správy")
    ap.add_argument("--iface", default="eth0")
    args = ap.parse_args()

    if not os.path.exists(args.fb):
        raise SystemExit(f"Framebuffer {args.fb} neexistuje. Zkontroluj /dev/fb*")

    xres, yres, bpp, line_length, smem_len = get_fb_info(args.fb)

    fd = os.open(args.fb, os.O_RDWR)
    fbmem = mmap.mmap(fd, smem_len, mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ, 0)

    panel_poller = None
    try:
        try:
            font_title = ImageFont.truetype(args.font, size=max(20, min(xres, yres) // 10))
            font = ImageFont.truetype(args.font, size=max(14, min(xres, yres) // 18))
        except Exception:
            font_title = ImageFont.load_default()
            font = ImageFont.load_default()

        hostname = "HanzHUB"
        bytes_per_pixel = 2 if bpp == 16 else 4
        row_bytes = xres * bytes_per_pixel
        rows = min(yres, (smem_len // line_length) if line_length else yres)

        meteo_cache = {"temp": None, "vbat": None, "soc": None, "ts": None}
        meteo_next = 0.0
        last_stamp = None  # track last ts to avoid flicker
        panel_poller = PanelPoller(args.infrapanel_api, args.infrapanel_refresh, args.infrapanel_id)

        while True:
            now = datetime.now()

            # METEO: last valid record (not last line)
            tnow = time.time()
            if tnow >= meteo_next:
                meteo_next = tnow + max(0.5, args.meteo_refresh)
                rec = read_last_valid_meteo(args.meteo_csv)
                if rec:
                    stamp = rec.get("ts")
                    if stamp != last_stamp:
                        meteo_cache = rec
                        last_stamp = stamp

            cpu_pct = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            du = psutil.disk_usage("/")
            cpu_temp = get_cpu_temp_c()
            uptime = get_uptime_str()
            ip_eth0 = get_iface_ip(args.iface)

            img = Image.new("RGB", (xres, yres), (0, 0, 0))
            draw = ImageDraw.Draw(img)

            pad = 10
            y = pad

            draw.text((pad, y), f"{hostname}  {now.strftime('%H:%M:%S')}", font=font_title, fill=(255,255,255))
            y += font_title.size + 6
            draw.line((pad, y, xres - pad, y), fill=(120,120,120), width=2)
            y += 10

            x_label = pad
            x_value = pad + 120

            def line(label, value):
                nonlocal y
                draw.text((x_label, y), label, font=font, fill=(255,255,255))
                draw.text((x_value, y), str(value), font=font, fill=(255,255,255))
                y += font.size + 2

            line("CPU:", f"{cpu_pct:.0f}%")
            line("RAM:", f"{vm.percent:.0f}%  ({format_bytes(vm.used)}/{format_bytes(vm.total)})")
            line("DISK:", f"{du.percent:.0f}%  ({format_bytes(du.used)}/{format_bytes(du.total)})")
            line("TEMP:", "?°C" if cpu_temp is None else f"{cpu_temp:.1f}°C")
            line("UPTIME:", uptime)
            line("eth0:", ip_eth0)

            y += 6
            draw.line((pad, y, xres - pad, y), fill=(120,120,120), width=2)
            y += 10

            draw.text((pad, y), "Meteo", font=font_title, fill=(255,255,255))
            y += font_title.size + 2
            draw.line((pad, y, xres - pad, y), fill=(80,80,80), width=2)
            y += 8

            mtemp, mvbat, msoc, mts = meteo_cache.get("temp"), meteo_cache.get("vbat"), meteo_cache.get("soc"), meteo_cache.get("ts")

            line("Temp:", "?" if mtemp is None else f"{float(mtemp):.2f} °C")
            line("Vbat:", "?" if mvbat is None else f"{float(mvbat):.3f} V")
            line("SoC:",  "?" if msoc  is None else f"{float(msoc):.0f} %")
            line("Last:", fmt_last(mts))

            y += 6
            draw.line((pad, y, xres - pad, y), fill=(120,120,120), width=2)
            y += 10

            y = draw_panel_section(draw, pad, y, xres, font_title, font, panel_poller.snapshot())

            img_r = rotate_image(img, args.rotate)

            if img_r.size != (xres, yres):
                tmp = Image.new("RGB", (xres, yres), (0, 0, 0))
                if img_r.width >= xres and img_r.height >= yres:
                    left = (img_r.width - xres) // 2
                    top = (img_r.height - yres) // 2
                    img_r = img_r.crop((left, top, left + xres, top + yres))
                    tmp.paste(img_r, (0, 0))
                else:
                    ox = (xres - img_r.width) // 2
                    oy = (yres - img_r.height) // 2
                    tmp.paste(img_r, (ox, oy))
                img_r = tmp

            raw = rgb_to_rgb565_bytes(img_r) if bpp == 16 else img_r.tobytes("raw", "BGRX")

            fbmem.seek(0)
            for row in range(rows):
                start = row * row_bytes
                fbmem.write(raw[start:start + row_bytes])
                if line_length > row_bytes:
                    fbmem.write(b"\x00" * (line_length - row_bytes))

            fbmem.flush()
            time.sleep(args.interval)

    finally:
        if panel_poller:
            panel_poller.close()
        try:
            fbmem.close()
        except Exception:
            pass
        os.close(fd)

if __name__ == "__main__":
    main()
