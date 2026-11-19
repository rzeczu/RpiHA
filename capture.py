#!/usr/bin/env python3
"""
capture.py - profilowanie kontrolera USB HID (gitara "RT-GH INPUT") na Windows 10/11.

Gitara zostaje na systemowym sterowniku HID (bez libusb/WinUSB/Zadiga).
Deskryptory i timing pochodzą z nagrań USBPcap (USBPcapCMD), raporty z hidapi.

Wymaga: Python 3, `pip install hidapi`, Wireshark + USBPcap, uruchomienie jako Administrator.

Przykłady:
    py capture.py                          # pełne profilowanie -> profile.json, enum.pcap, timing.pcap
    py capture.py --verify                 # porównanie podłączonego Pico z profile.json
    py capture.py --parse-only --from-pcap enum.pcap --vid 0x1234 --pid 0x5678
"""
from __future__ import annotations

import argparse
import ctypes
import datetime
import json
import os
import re
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

DEFAULT_PRODUCT = "RT-GH INPUT"
DEFAULT_INPUTS = ["GREEN", "RED", "YELLOW", "BLUE", "ORANGE", "STRUM UP", "STRUM DOWN"]
LINKTYPE_USBPCAP = 249

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass


# --------------------------------------------------------------------------- utils

def die(msg: str, code: int = 2):
    print(f"\n[BŁĄD] {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg: str):
    print(f"  [!] {msg}")


def step(n, title: str):
    print(f"\n=== {n}. {title} " + "=" * max(3, 60 - len(title)))


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def hx(b: bytes | None) -> str | None:
    return None if b is None else bytes(b).hex()


def unhx(s: str | None) -> bytes | None:
    return None if s is None else bytes.fromhex(s)


def hexdump(b: bytes, indent: str = "    ", width: int = 16) -> str:
    return "\n".join(indent + b[i:i + width].hex(" ") for i in range(0, len(b), width)) or indent + "(puste)"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def rid_key(rid) -> str:
    return "none" if rid is None else str(rid)


def key_rid(k: str):
    return None if k == "none" else int(k)


# --------------------------------------------------------------------------- narzędzia

def _program_dirs():
    dirs = []
    for var, dflt in (("ProgramW6432", r"C:\Program Files"), ("ProgramFiles", r"C:\Program Files"),
                      ("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        d = os.environ.get(var, dflt)
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def find_tool(explicit: str | None, exe: str, rel_candidates: list[str]) -> str | None:
    if explicit:
        if Path(explicit).is_file():
            return explicit
        die(f"Nie znaleziono {exe} pod podaną ścieżką: {explicit}")
    for d in _program_dirs():
        for rel in rel_candidates:
            p = Path(d) / rel
            if p.is_file():
                return str(p)
    return shutil.which(exe)


def find_usbpcapcmd(explicit):
    return find_tool(explicit, "USBPcapCMD.exe", [
        r"USBPcap\USBPcapCMD.exe",
        r"Wireshark\extcap\USBPcapCMD.exe",
        r"Wireshark\extcap\wireshark\USBPcapCMD.exe",
    ])


def find_tshark(explicit):
    return find_tool(explicit, "tshark.exe", [r"Wireshark\tshark.exe"])


def _decode_tool_output(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("mbcs", errors="replace")


# --------------------------------------------------------------------------- pcap / pcapng

class Rec:
    __slots__ = ("ts", "data", "linktype")

    def __init__(self, ts, data, linktype):
        self.ts, self.data, self.linktype = ts, data, linktype


def read_capture(path) -> list[Rec]:
    raw = Path(path).read_bytes()
    if len(raw) < 24:
        return []
    m = raw[:4]
    if m in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"):
        return _read_pcap(raw)
    if m == b"\x0a\x0d\x0d\x0a":
        return _read_pcapng(raw)
    raise ValueError(f"{path}: nieznany format pliku (ani pcap, ani pcapng)")


def _read_pcap(raw: bytes) -> list[Rec]:
    e = "<" if raw[:4] in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
    div = 1e9 if raw[:4] in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d") else 1e6
    linktype = struct.unpack_from(e + "I", raw, 20)[0] & 0x0FFFFFFF
    out, off = [], 24
    while off + 16 <= len(raw):
        sec, frac, incl, _orig = struct.unpack_from(e + "IIII", raw, off)
        data = raw[off + 16: off + 16 + incl]
        if len(data) < incl:  # ostatni rekord ucięty przy zatrzymaniu USBPcapCMD
            break
        out.append(Rec(sec + frac / div, data, linktype))
        off += 16 + incl
    return out


def _read_pcapng(raw: bytes) -> list[Rec]:
    out, off, e, ifaces = [], 0, "<", []
    while off + 12 <= len(raw):
        btype = struct.unpack_from("<I", raw, off)[0]
        if btype == 0x0A0D0D0A:
            e = "<" if raw[off + 8: off + 12] == b"\x4d\x3c\x2b\x1a" else ">"
            ifaces = []
        blen = struct.unpack_from(e + "I", raw, off + 4)[0]
        if blen < 12 or off + blen > len(raw):
            break
        body = raw[off + 8: off + blen - 4]
        if btype == 1 and len(body) >= 8:  # IDB
            lt = struct.unpack_from(e + "H", body, 0)[0]
            res, o = 1e-6, 8
            while o + 4 <= len(body):
                code, ln = struct.unpack_from(e + "HH", body, o)
                if code == 0:
                    break
                if code == 9 and ln >= 1:
                    v = body[o + 4]
                    res = 2.0 ** -(v & 0x7F) if v & 0x80 else 10.0 ** -v
                o += 4 + ((ln + 3) & ~3)
            ifaces.append((lt, res))
        elif btype == 6 and len(body) >= 20:  # EPB
            iid, th, tl, cap, _orig = struct.unpack_from(e + "IIIII", body, 0)
            lt, res = ifaces[iid] if iid < len(ifaces) else (0, 1e-6)
            out.append(Rec(((th << 32) | tl) * res, body[20:20 + cap], lt))
        off += blen
    return out


# --------------------------------------------------------------------------- USBPcap

class UPkt:
    __slots__ = ("ts", "irp", "status", "function", "info", "bus", "dev", "ep", "xfer", "stage", "data")


def parse_usbpcap(recs: list[Rec]) -> list[UPkt]:
    out = []
    for r in recs:
        if r.linktype != LINKTYPE_USBPCAP or len(r.data) < 27:
            continue
        d = r.data
        hlen, irp, status, func, info, bus, dev, ep, xfer, dlen = struct.unpack_from("<HQIHBHHBBI", d, 0)
        if hlen < 27 or hlen > len(d):
            continue
        p = UPkt()
        p.ts, p.irp, p.status, p.function, p.info = r.ts, irp, status, func, info
        p.bus, p.dev, p.ep, p.xfer = bus, dev, ep, xfer
        p.stage = d[27] if xfer == 2 and hlen >= 28 else None
        p.data = d[hlen:hlen + dlen]
        out.append(p)
    return out


USBD_STATUS = {0: "OK", 0xC0000004: "STALL", 0xC0000005: "DEV_NOT_RESPONDING", 0xC0010000: "CANCELED",
               0xC0000030: "BABBLE"}


def status_name(s):
    if s is None:
        return "brak odpowiedzi"
    return USBD_STATUS.get(s, f"0x{s:08X}")


class Ctrl:
    __slots__ = ("ts", "bus", "dev", "setup", "out_data", "response", "status", "complete")

    def __init__(self, ts, bus, dev, setup):
        self.ts, self.bus, self.dev, self.setup = ts, bus, dev, setup
        self.out_data, self.response, self.status, self.complete = b"", b"", None, False

    @property
    def fields(self):
        return struct.unpack("<BBHHH", self.setup)  # bmRequestType, bRequest, wValue, wIndex, wLength

    def is_get_desc(self, dtype=None, bm=None):
        b, r, v, _i, _l = self.fields
        return r == 6 and (b & 0x80) and (dtype is None or (v >> 8) == dtype) and (bm is None or b == bm)

    def to_json(self, t0=0.0):
        return {"t": round(self.ts - t0, 6), "setup": self.setup.hex(), "out": self.out_data.hex(),
                "response": self.response.hex(), "status": status_name(self.status)}


def pair_control(pkts: list[UPkt]) -> list[Ctrl]:
    pending, done = {}, []
    for p in pkts:
        if p.xfer != 2:
            continue
        key = (p.bus, p.irp)
        if not (p.info & 1):  # host -> urządzenie
            if (p.stage == 0 or p.stage is None) and len(p.data) >= 8:
                c = Ctrl(p.ts, p.bus, p.dev, p.data[:8])
                c.out_data = p.data[8:]
                pending[key] = c
                done.append(c)
            elif p.stage == 1 and key in pending:
                pending[key].out_data += p.data
        else:  # zakończenie
            c = pending.get(key)
            if c is None:
                continue
            if p.data and (c.setup[0] & 0x80) and not (p.stage == 3 and c.response):
                c.response += p.data
            c.status = p.status
            if p.stage in (None, 2, 3):
                c.complete = True
                del pending[key]
    return done


def group_devices(ctrls: list[Ctrl]):
    groups: dict[tuple, list[Ctrl]] = {}
    for c in ctrls:
        groups.setdefault((c.bus, c.dev), []).append(c)
    devices = {}  # (bus,dev) -> (vid,pid)
    for k, lst in groups.items():
        for c in lst:
            if c.is_get_desc(1, 0x80) and len(c.response) >= 12:
                devices[k] = struct.unpack_from("<HH", c.response, 8)
    return groups, devices


def pick_group(groups, devices, vid, pid):
    """Grupa (bus,adres) gitary: najwięcej różnych deskryptorów, przy remisie najpóźniejsza."""
    cands = [k for k, vp in devices.items() if (vid is None or vp[0] == vid) and (pid is None or vp[1] == pid)]
    if len(cands) > 1:
        cands = [k for k in cands if k[1] != 0] or cands

    def score(k):
        keys = {c.setup[:6] for c in groups[k] if c.is_get_desc() and c.response}
        return (len(keys), max(c.ts for c in groups[k]))

    return max(cands, key=score) if cands else None


# --------------------------------------------------------------------------- deskryptory USB

DESC_NAMES = {1: "DEVICE", 2: "CONFIGURATION", 3: "STRING", 4: "INTERFACE", 5: "ENDPOINT",
              6: "DEVICE_QUALIFIER", 7: "OTHER_SPEED_CONFIG", 8: "INTERFACE_POWER", 0x0B: "IAD", 0x0F: "BOS",
              0x10: "DEVICE_CAPABILITY", 0x21: "HID", 0x22: "REPORT", 0x24: "CS_INTERFACE", 0x25: "CS_ENDPOINT"}

FIELD_LAYOUTS = {
    1: [(0, 1, "bLength"), (1, 1, "bDescriptorType"), (2, 2, "bcdUSB"), (4, 1, "bDeviceClass"),
        (5, 1, "bDeviceSubClass"), (6, 1, "bDeviceProtocol"), (7, 1, "bMaxPacketSize0"), (8, 2, "idVendor"),
        (10, 2, "idProduct"), (12, 2, "bcdDevice"), (14, 1, "iManufacturer"), (15, 1, "iProduct"),
        (16, 1, "iSerialNumber"), (17, 1, "bNumConfigurations")],
    2: [(0, 1, "bLength"), (1, 1, "bDescriptorType"), (2, 2, "wTotalLength"), (4, 1, "bNumInterfaces"),
        (5, 1, "bConfigurationValue"), (6, 1, "iConfiguration"), (7, 1, "bmAttributes"), (8, 1, "bMaxPower")],
    4: [(0, 1, "bLength"), (1, 1, "bDescriptorType"), (2, 1, "bInterfaceNumber"), (3, 1, "bAlternateSetting"),
        (4, 1, "bNumEndpoints"), (5, 1, "bInterfaceClass"), (6, 1, "bInterfaceSubClass"),
        (7, 1, "bInterfaceProtocol"), (8, 1, "iInterface")],
    5: [(0, 1, "bLength"), (1, 1, "bDescriptorType"), (2, 1, "bEndpointAddress"), (3, 1, "bmAttributes"),
        (4, 2, "wMaxPacketSize"), (6, 1, "bInterval")],
    6: [(0, 1, "bLength"), (1, 1, "bDescriptorType"), (2, 2, "bcdUSB"), (4, 1, "bDeviceClass"),
        (5, 1, "bDeviceSubClass"), (6, 1, "bDeviceProtocol"), (7, 1, "bMaxPacketSize0"),
        (8, 1, "bNumConfigurations"), (9, 1, "bReserved")],
    0x0B: [(0, 1, "bLength"), (1, 1, "bDescriptorType"), (2, 1, "bFirstInterface"), (3, 1, "bInterfaceCount"),
           (4, 1, "bFunctionClass"), (5, 1, "bFunctionSubClass"), (6, 1, "bFunctionProtocol"), (7, 1, "iFunction")],
    0x0F: [(0, 1, "bLength"), (1, 1, "bDescriptorType"), (2, 2, "wTotalLength"), (4, 1, "bNumDeviceCaps")],
    0x21: [(0, 1, "bLength"), (1, 1, "bDescriptorType"), (2, 2, "bcdHID"), (4, 1, "bCountryCode"),
           (5, 1, "bNumDescriptors"), (6, 1, "bClassDescriptorType"), (7, 2, "wDescriptorLength")],
}

EP_TYPES = ["Control", "Isochronous", "Bulk", "Interrupt"]


def split_descriptors(data: bytes):
    out, i = [], 0
    while i + 2 <= len(data):
        ln = data[i]
        if ln < 2:
            out.append((i, len(data) - i, None))
            break
        out.append((i, min(ln, len(data) - i), data[i + 1]))
        i += ln
    return out


def decode_fields(typ, raw: bytes) -> dict:
    return {n: int.from_bytes(raw[o:o + s], "little") for o, s, n in FIELD_LAYOUTS.get(typ, []) if o + s <= len(raw)}


def desc_field_at(data: bytes, off: int) -> str:
    for start, ln, typ in split_descriptors(data):
        if start <= off < start + ln:
            rel = off - start
            tname = DESC_NAMES.get(typ, f"0x{typ:02X}" if typ is not None else "?")
            for o, s, n in FIELD_LAYOUTS.get(typ, []):
                if o <= rel < o + s:
                    return f"{tname}@{start}.{n}"
            return f"{tname}@{start}+{rel}"
    return "poza deskryptorem"


def fmt_fields(typ, raw) -> str:
    f = decode_fields(typ, raw)
    sizes = {n: s for _o, s, n in FIELD_LAYOUTS.get(typ, [])}
    parts = []
    for n, v in f.items():
        if n in ("bLength", "bDescriptorType"):
            continue
        parts.append(f"{n}=0x{v:0{2 * sizes[n]}X}" if sizes[n] > 1 or n.startswith(("bm", "bcd", "id", "bEndpoint", "bInterfaceClass", "bDeviceClass")) else f"{n}={v}")
    if typ == 5 and "bEndpointAddress" in f:
        a = f["bEndpointAddress"]
        parts.append(f"({'IN' if a & 0x80 else 'OUT'} {a & 0x0F}, {EP_TYPES[f.get('bmAttributes', 0) & 3]})")
    return " ".join(parts)


def config_info(data: bytes) -> list[dict]:
    ifaces, cur = [], None
    for start, ln, typ in split_descriptors(data):
        f = decode_fields(typ, data[start:start + ln])
        if typ == 4:
            cur = {"number": f.get("bInterfaceNumber"), "alt": f.get("bAlternateSetting"),
                   "class": f.get("bInterfaceClass"), "subclass": f.get("bInterfaceSubClass"),
                   "protocol": f.get("bInterfaceProtocol"), "iInterface": f.get("iInterface", 0),
                   "endpoints": [], "hid": None}
            ifaces.append(cur)
        elif typ == 5 and cur is not None:
            cur["endpoints"].append({"address": f.get("bEndpointAddress"), "attributes": f.get("bmAttributes"),
                                     "wMaxPacketSize": f.get("wMaxPacketSize"), "bInterval": f.get("bInterval")})
        elif typ == 0x21 and cur is not None:
            cur["hid"] = f
    return ifaces


def string_indices(device: bytes | None, configs: dict) -> dict[int, str]:
    idx = {}
    if device and len(device) >= 18:
        f = decode_fields(1, device)
        for n in ("iManufacturer", "iProduct", "iSerialNumber"):
            if f.get(n):
                idx[f[n]] = n
    for ci, cfg in configs.items():
        for start, ln, typ in split_descriptors(cfg):
            f = decode_fields(typ, cfg[start:start + ln])
            for n in ("iConfiguration", "iInterface", "iFunction"):
                if f.get(n):
                    idx.setdefault(f[n], f"{n} w {DESC_NAMES.get(typ)} @{start}")
    return idx


def decode_string(raw: bytes) -> str:
    if len(raw) < 2:
        return ""
    return raw[2:raw[0]].decode("utf-16-le", errors="replace")


def make_string_desc(text: str) -> bytes:
    b = text.encode("utf-16-le")
    return bytes([2 + len(b), 3]) + b


# --------------------------------------------------------------------------- HID report descriptor

MAIN_TAGS = {8: "Input", 9: "Output", 10: "Collection", 11: "Feature", 12: "End Collection"}
GLOBAL_TAGS = {0: "Usage Page", 1: "Logical Minimum", 2: "Logical Maximum", 3: "Physical Minimum",
               4: "Physical Maximum", 5: "Unit Exponent", 6: "Unit", 7: "Report Size", 8: "Report ID",
               9: "Report Count", 10: "Push", 11: "Pop"}
LOCAL_TAGS = {0: "Usage", 1: "Usage Minimum", 2: "Usage Maximum", 3: "Designator Index", 4: "Designator Minimum",
              5: "Designator Maximum", 7: "String Index", 8: "String Minimum", 9: "String Maximum", 10: "Delimiter"}
COLLECTION_TYPES = {0: "Physical", 1: "Application", 2: "Logical", 3: "Report", 4: "Named Array",
                    5: "Usage Switch", 6: "Usage Modifier"}
PAGES = {0x01: "Generic Desktop", 0x02: "Simulation", 0x05: "Game Controls", 0x06: "Generic Device",
         0x07: "Keyboard", 0x08: "LED", 0x09: "Button", 0x0C: "Consumer", 0x0F: "PID", 0x20: "Sensors"}
GD_USAGES = {0x01: "Pointer", 0x02: "Mouse", 0x04: "Joystick", 0x05: "Gamepad", 0x06: "Keyboard",
             0x08: "Multi-axis", 0x30: "X", 0x31: "Y", 0x32: "Z", 0x33: "Rx", 0x34: "Ry", 0x35: "Rz",
             0x36: "Slider", 0x37: "Dial", 0x38: "Wheel", 0x39: "Hat switch", 0x3D: "Start", 0x3E: "Select",
             0x90: "D-pad Up", 0x91: "D-pad Down", 0x92: "D-pad Right", 0x93: "D-pad Left"}


def usage_name(page, u) -> str:
    if page is None or u is None:
        return "?"
    if page == 0x09:
        return f"Button {u}" if u else "No button"
    if page == 0x01 and u in GD_USAGES:
        return GD_USAGES[u]
    if page >= 0xFF00:
        return f"Vendor 0x{page:04X}:0x{u:02X}"
    return f"{PAGES.get(page, f'Page 0x{page:04X}')}:0x{u:02X}"


def flags_str(f: int, main: str) -> str:
    s = ["Const" if f & 1 else "Data", "Var" if f & 2 else "Array", "Rel" if f & 4 else "Abs"]
    if f & 8: s.append("Wrap")
    if f & 0x10: s.append("NonLinear")
    if f & 0x20: s.append("NoPreferred")
    if f & 0x40: s.append("Null")
    if main != "Input" and f & 0x80: s.append("Volatile")
    if f & 0x100: s.append("Buffered")
    return ",".join(s)


def parse_report_descriptor(desc: bytes) -> dict:
    items, fields, warnings = [], [], []
    g = dict(page=0, lmin=0, lmax=0, pmin=0, pmax=0, uexp=0, unit=0, size=0, rid=None, count=0)
    stack, coll = [], []
    loc = {"usages": [], "umin": None, "umax": None}
    offsets: dict[tuple, int] = {}
    uses_ids = False
    i = 0
    while i < len(desc):
        b = desc[i]
        if b == 0xFE:  # long item
            ln = desc[i + 1] if i + 1 < len(desc) else 0
            items.append({"offset": i, "hex": desc[i:i + 3 + ln].hex(" "), "name": "Long item", "value": None,
                          "depth": len(coll)})
            i += 3 + ln
            continue
        size = (0, 1, 2, 4)[b & 3]
        typ, tag = (b >> 2) & 3, b >> 4
        raw = desc[i + 1:i + 1 + size]
        if len(raw) < size:
            warnings.append(f"ucięty item na offsecie {i}")
            break
        u = int.from_bytes(raw, "little")
        s = int.from_bytes(raw, "little", signed=True) if size else 0
        depth = len(coll)
        name, val = "?", u
        if typ == 0:
            name = MAIN_TAGS.get(tag, f"Main tag {tag}")
            if tag in (8, 9, 11):
                key = (name, g["rid"])
                start = offsets.get(key, 0)
                usages = [(uu >> 16, uu & 0xFFFF) if us == 4 else (g["page"], uu) for uu, us in loc["usages"]]
                rng = None
                if loc["umin"] is not None and loc["umax"] is not None:
                    pg = loc["umin"][0] >> 16 if loc["umin"][1] == 4 else g["page"]
                    rng = (pg, loc["umin"][0] & 0xFFFF if loc["umin"][1] == 4 else loc["umin"][0],
                           loc["umax"][0] & 0xFFFF if loc["umax"][1] == 4 else loc["umax"][0])
                lmin, lmax = g["lmin"], g["lmax"]
                if lmin >= 0 and lmax < lmin and g.get("_lmax_size"):  # częsty błąd: 0xFF zakodowane jednym bajtem
                    lmax &= (1 << (8 * g["_lmax_size"])) - 1
                fields.append({"type": name, "report_id": g["rid"], "bit": start, "size": g["size"],
                               "count": g["count"], "flags": u, "usages": usages, "range": rng,
                               "lmin": lmin, "lmax": lmax, "collection": "/".join(coll)})
                offsets[key] = start + g["size"] * g["count"]
                val = flags_str(u, name)
            elif tag == 10:
                cu = usage_name(*(((loc["usages"][0][0] >> 16, loc["usages"][0][0] & 0xFFFF)
                                   if loc["usages"][0][1] == 4 else (g["page"], loc["usages"][0][0]))
                                  if loc["usages"] else (None, None)))
                coll.append(f"{COLLECTION_TYPES.get(u, u)}({cu})")
                val = COLLECTION_TYPES.get(u, u)
            elif tag == 12:
                if coll:
                    coll.pop()
                else:
                    warnings.append(f"End Collection bez Collection (offset {i})")
                depth = len(coll)
                val = None
            loc = {"usages": [], "umin": None, "umax": None}
        elif typ == 1:
            name = GLOBAL_TAGS.get(tag, f"Global tag {tag}")
            if tag == 0: g["page"] = u; val = f"0x{u:04X} ({PAGES.get(u, 'Vendor' if u >= 0xFF00 else '?')})"
            elif tag == 1: g["lmin"] = s; val = s
            elif tag == 2: g["lmax"] = s; g["_lmax_size"] = size; val = s
            elif tag == 3: g["pmin"] = s; val = s
            elif tag == 4: g["pmax"] = s; val = s
            elif tag == 5: g["uexp"] = s; val = s
            elif tag == 6: g["unit"] = u; val = f"0x{u:X}"
            elif tag == 7: g["size"] = u
            elif tag == 8: g["rid"] = u; uses_ids = True
            elif tag == 9: g["count"] = u
            elif tag == 10: stack.append(dict(g)); val = None
            elif tag == 11:
                if stack: g = stack.pop()
                else: warnings.append(f"Pop bez Push (offset {i})")
                val = None
        elif typ == 2:
            name = LOCAL_TAGS.get(tag, f"Local tag {tag}")
            if tag == 0:
                loc["usages"].append((u, size))
                pg = (u >> 16) if size == 4 else g["page"]
                val = f"0x{u & 0xFFFF:02X} ({usage_name(pg, u & 0xFFFF)})"
            elif tag == 1: loc["umin"] = (u, size); val = f"0x{u:02X}"
            elif tag == 2: loc["umax"] = (u, size); val = f"0x{u:02X}"
        else:
            name = "Reserved"
        items.append({"offset": i, "hex": desc[i:i + 1 + size].hex(" "), "name": name, "value": val, "depth": depth})
        i += 1 + size
    if coll:
        warnings.append("niezamknięte kolekcje: " + ", ".join(coll))
    lengths = {k: v for k, v in offsets.items()}
    rmap = {"uses_report_ids": uses_ids, "items": items, "fields": fields, "lengths": lengths,
            "warnings": warnings}
    rmap["elements"] = build_elements(rmap)
    return rmap


def build_elements(rmap) -> list[dict]:
    els, off = [], 1 if rmap["uses_report_ids"] else 0
    for f in rmap["fields"]:
        base = dict(type=f["type"], report_id=f["report_id"], lmin=f["lmin"], lmax=f["lmax"],
                    flags=flags_str(f["flags"], f["type"]), collection=f["collection"])
        if f["flags"] & 1:
            if f["size"] * f["count"]:
                els.append(dict(base, bit=f["bit"], size=f["size"] * f["count"], kind="pad", usage="(padding)"))
            continue
        if f["flags"] & 2:
            for j in range(f["count"]):
                if f["usages"]:
                    pg, uu = f["usages"][min(j, len(f["usages"]) - 1)]
                elif f["range"]:
                    pg, lo, hi = f["range"]
                    uu = min(lo + j, hi)
                else:
                    pg, uu = None, None
                els.append(dict(base, bit=f["bit"] + j * f["size"], size=f["size"], kind="var",
                                usage=usage_name(pg, uu), usage_page=pg, usage_id=uu))
        else:
            if f["range"]:
                desc = f"array[{usage_name(f['range'][0], f['range'][1])} .. {usage_name(f['range'][0], f['range'][2])}]"
            else:
                desc = "array[" + ", ".join(usage_name(p, x) for p, x in f["usages"]) + "]"
            for j in range(f["count"]):
                els.append(dict(base, bit=f["bit"] + j * f["size"], size=f["size"], kind="array", usage=desc))
    for e in els:
        e["byte"] = e["bit"] // 8 + off
        e["bit_in_byte"] = e["bit"] % 8
    return els


def element_at(rmap, rtype, rid, wire_byte, bit):
    pbit = (wire_byte - (1 if rmap["uses_report_ids"] else 0)) * 8 + bit
    for e in rmap["elements"]:
        if e["type"] == rtype and e["report_id"] == rid and e["bit"] <= pbit < e["bit"] + e["size"]:
            return e
    return None


def element_value(report: bytes, e, uses_ids) -> int:
    payload = report[1:] if uses_ids else report
    v = int.from_bytes(payload, "little")
    x = (v >> e["bit"]) & ((1 << e["size"]) - 1)
    if e.get("lmin", 0) < 0 and e["size"] > 1 and x >> (e["size"] - 1):  # pole ze znakiem
        x -= 1 << e["size"]
    return x


def report_len(rmap, rtype, rid) -> int:
    """Długość raportu na kablu (z bajtem report ID, jeśli używany)."""
    bits = rmap["lengths"].get((rtype, rid), 0)
    return (bits + 7) // 8 + (1 if rmap["uses_report_ids"] else 0)


def print_report_items(rmap):
    for it in rmap["items"]:
        v = "" if it["value"] is None else f" {it['value']}"
        print(f"    {it['offset']:4d}  {it['hex']:<14} {'  ' * it['depth']}{it['name']}{v}")


def print_report_map(rmap):
    uses = rmap["uses_report_ids"]
    print(f"  Report ID używane: {'TAK (pierwszy bajt raportu = ID)' if uses else 'NIE'}")
    for (t, rid), bits in sorted(rmap["lengths"].items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        print(f"  {t:<7} ID={rid_key(rid):<4}: {bits} bitów danych -> raport na kablu {report_len(rmap, t, rid)} B")
    print("  Pola (bajt/bit = pozycja w surowym raporcie, z bajtem ID jeśli jest):")
    print(f"    {'typ':<7} {'ID':<4} {'bajt.bit':<8} {'bity':>4}  {'usage':<34} {'zakres':<14} flagi")
    for e in rmap["elements"]:
        rng = f"[{e['lmin']}..{e['lmax']}]" if e["kind"] != "pad" else ""
        print(f"    {e['type']:<7} {rid_key(e['report_id']):<4} {e['byte']:>4}.{e['bit_in_byte']:<3} {e['size']:>4}  "
              f"{e['usage'][:34]:<34} {rng:<14} {e['flags']}")
    for w in rmap["warnings"]:
        warn(w)


def item_at(rmap, off):
    for it in rmap["items"]:
        n = len(it["hex"].split())
        if it["offset"] <= off < it["offset"] + n:
            return f"item@{it['offset']} {it['name']}"
    return "?"


def rmap_signature(rmap):
    return [(e["type"], e["report_id"], e["bit"], e["size"], e["kind"], e["usage"],
             None if e["kind"] == "pad" else (e["lmin"], e["lmax"]), e["flags"]) for e in rmap["elements"]]


def rmap_to_json(rmap):
    return {"uses_report_ids": rmap["uses_report_ids"],
            "lengths": [{"type": t, "report_id": rid, "bits": b, "wire_bytes": report_len(rmap, t, rid)}
                        for (t, rid), b in rmap["lengths"].items()],
            "elements": rmap["elements"], "warnings": rmap["warnings"]}


# --------------------------------------------------------------------------- analiza nagrania enumeracji

def analyze_enum(path, vid, pid) -> dict | None:
    pkts = parse_usbpcap(read_capture(path))
    ctrls = pair_control(pkts)
    groups, devices = group_devices(ctrls)
    key = pick_group(groups, devices, vid, pid)
    en = {"path": str(path), "packets": len(pkts), "devices": devices, "key": key}
    if key is None:
        return en
    lst = groups[key]
    t0 = lst[0].ts
    best, failed, cv = {}, [], []
    for c in lst:
        bm, br, wv, wi, _wl = c.fields
        if c.is_get_desc():
            k = (bm, wv, wi)
            if c.status == 0 and c.response:
                if k not in best or len(c.response) >= len(best[k].response):
                    best[k] = c
            elif c.complete:
                failed.append(c)
        elif bm & 0x80 and (bm & 0x60) in (0x20, 0x40) and c.complete:
            cv.append(c)
    en.update(device=None, configs={}, strings={}, hid_report={}, hid_desc={}, bos=None, qualifier=None,
              other_desc={})
    for (bm, wv, wi), c in best.items():
        dt, di = wv >> 8, wv & 0xFF
        r = c.response
        if bm == 0x80 and dt == 1:
            en["device"] = r
        elif bm == 0x80 and dt == 2:
            en["configs"][di] = r
        elif bm == 0x80 and dt == 3:
            en["strings"][(di, wi)] = {"raw": r, "source": "pcap"}
        elif bm == 0x81 and dt == 0x22:
            en["hid_report"][wi] = r
        elif bm == 0x81 and dt == 0x21:
            en["hid_desc"][wi] = r
        elif bm == 0x80 and dt == 0x0F:
            en["bos"] = r
        elif bm == 0x80 and dt == 6:
            en["qualifier"] = {"status": "OK", "hex": r.hex()}
        else:
            en["other_desc"][f"{bm:02X}:{wv:04X}:{wi:04X}"] = r
    for c in failed:
        if c.is_get_desc(6) and en["qualifier"] is None:
            en["qualifier"] = {"status": status_name(c.status), "hex": None}
    en["failed_desc"] = [c.to_json(t0) for c in failed]
    en["class_vendor_in"] = [c.to_json(t0) for c in cv]
    en["control_log"] = [c.to_json(t0) for c in lst]
    en["ifaces"] = config_info(en["configs"].get(0, b""))
    en["langids"] = langids_of(en["strings"].get((0, 0), {}).get("raw", b""))
    en["string_probe"] = None
    en["interrupt_in"] = sum(1 for p in pkts if p.xfer == 1 and (p.ep & 0x80) and (p.info & 1)
                             and (p.bus, p.dev) == key and p.data)
    check_enum(en)
    return en


def check_enum(en):
    crit, other = [], []
    d = en["device"]
    if not d or len(d) < 18:
        crit.append("device descriptor (pełne 18 B)")
    cfg = en["configs"].get(0)
    if not cfg:
        crit.append("configuration descriptor")
    else:
        tot = decode_fields(2, cfg).get("wTotalLength", 0)
        if len(cfg) < tot:
            crit.append(f"pełny configuration descriptor (jest {len(cfg)} B z {tot} B)")
    for it in en["ifaces"]:
        if it["class"] == 3:
            rd = en["hid_report"].get(it["number"])
            exp = (it["hid"] or {}).get("wDescriptorLength")
            if rd is None:
                crit.append(f"HID report descriptor interfejsu {it['number']}")
            elif exp and len(rd) != exp:
                crit.append(f"HID report descriptor interfejsu {it['number']}: {len(rd)} B, wDescriptorLength={exp}")
    if not en["langids"]:
        other.append("string descriptor 0 (lista LANGID)")
    lang = en["langids"][0] if en["langids"] else 0x0409
    for idx, who in sorted(string_indices(d, en["configs"]).items()):
        if (idx, lang) not in en["strings"]:
            other.append(f"string {idx} ({who}), LANGID 0x{lang:04X}")
    if d and len(d) >= 4 and decode_fields(1, d)["bcdUSB"] >= 0x0201 and en["bos"] is None:
        other.append("BOS descriptor (bcdUSB >= 2.01)")
    en["missing_critical"], en["missing_other"] = crit, other


def print_enum(en):
    print(f"  Plik: {en['path']}  pakietów USBPcap: {en['packets']}")
    print("  Urządzenia z device descriptorem w nagraniu:")
    for (bus, dev), (v, p) in sorted(en["devices"].items()):
        mark = "  <== gitara" if (bus, dev) == en["key"] else ""
        print(f"    bus {bus} adres {dev}: VID 0x{v:04X} PID 0x{p:04X}{mark}")
    if en["key"] is None:
        return
    d = en["device"]
    if d:
        f = decode_fields(1, d)
        print(f"\n  DEVICE ({len(d)} B): {fmt_fields(1, d)}")
        print(hexdump(d))
        print(f"    -> bcdUSB {f.get('bcdUSB', 0):04X}, bcdDevice {f.get('bcdDevice', 0):04X}, "
              f"bMaxPacketSize0 {f.get('bMaxPacketSize0')}, VID:PID {f.get('idVendor', 0):04X}:{f.get('idProduct', 0):04X}")
    for ci, cfg in sorted(en["configs"].items()):
        print(f"\n  CONFIGURATION #{ci} ({len(cfg)} B):")
        for start, ln, typ in split_descriptors(cfg):
            ind = "    " if typ in (2, 4, 0x0B) else "      "
            if typ == 2:
                ind = "  "
            print(f"  {ind}{DESC_NAMES.get(typ, f'0x{typ:02X}' if typ is not None else '?')}: "
                  f"{fmt_fields(typ, cfg[start:start + ln])}  [{cfg[start:start + ln].hex(' ')}]")
    if en["bos"]:
        print(f"\n  BOS ({len(en['bos'])} B):\n{hexdump(en['bos'])}")
    if en["qualifier"]:
        print(f"\n  DEVICE_QUALIFIER: {en['qualifier']['status']} {en['qualifier']['hex'] or ''}")
    print(f"\n  LANGID: {', '.join(f'0x{x:04X}' for x in en['langids']) or '(brak)'}")
    for (idx, lang), s in sorted(en["strings"].items()):
        if idx == 0:
            continue
        txt = decode_string(s["raw"])
        extra = ""
        if idx == 0xEE and txt.startswith("MSFT100") and len(s["raw"]) >= 17:
            extra = f"  (MS OS 1.0, bMS_VendorCode=0x{s['raw'][16]:02X})"
        print(f"  STRING {idx:3d} lang 0x{lang:04X}: {txt!r}{extra}  [{s['raw'].hex(' ')}]  ({s['source']})")
    for wi, rd in sorted(en["hid_report"].items()):
        print(f"\n  HID REPORT DESCRIPTOR interfejs {wi} ({len(rd)} B):\n{hexdump(rd)}")
    if en["class_vendor_in"]:
        print("\n  Inne żądania IN (class/vendor) z odpowiedzią:")
        for c in en["class_vendor_in"]:
            print(f"    setup {c['setup']} -> {c['status']} {c['response']}")
    if en["failed_desc"]:
        print("\n  GET_DESCRIPTOR zakończone błędem:")
        for c in en["failed_desc"]:
            print(f"    setup {c['setup']} -> {c['status']}")
    print(f"\n  Transferów kontrolnych do gitary: {len(en['control_log'])}, interrupt IN z danymi: {en['interrupt_in']}")


def tshark_crosscheck(tshark, path, key, expected_count):
    if not tshark or key is None:
        return
    try:
        r = subprocess.run([tshark, "-r", str(path), "-Y", f"usb.bus_id == {key[0]} && usb.device_address == {key[1]}",
                            "-T", "fields", "-e", "frame.number"], capture_output=True, timeout=60)
    except Exception as e:
        warn(f"tshark: {e}")
        return
    n = len([l for l in _decode_tool_output(r.stdout).splitlines() if l.strip()])
    if n == expected_count:
        print(f"  tshark: {n} pakietów dla bus {key[0]} adres {key[1]} - zgodne z parserem.")
    else:
        warn(f"tshark widzi {n} pakietów dla bus {key[0]} adres {key[1]}, parser {expected_count}. "
             "Sprawdź nagranie w Wiresharku.")


def count_dev_packets(path, key):
    return sum(1 for p in parse_usbpcap(read_capture(path)) if (p.bus, p.dev) == key)


# --------------------------------------------------------------------------- USBPcapCMD

class UsbPcap:
    def __init__(self, exe: str):
        self.exe = exe
        self.new_devices_flag = True

    def _run(self, args) -> str:
        r = subprocess.run([self.exe] + args, capture_output=True, timeout=30)
        return _decode_tool_output(r.stdout)

    def interfaces(self) -> list[tuple[str, str]]:
        out = []
        for line in self._run(["--extcap-interfaces"]).splitlines():
            if line.startswith("interface "):
                kv = dict(re.findall(r"\{(\w+)=([^}]*)\}", line))
                out.append((kv.get("value", ""), kv.get("display", "")))
        return out

    def devices(self, iface: str) -> list[dict]:
        out = []
        for line in self._run(["--extcap-interface", iface, "--extcap-config"]).splitlines():
            if line.startswith("value ") and "{arg=99}" in line:
                kv = dict(re.findall(r"\{(\w+)=([^}]*)\}", line))
                out.append(kv)
        return out

    def start(self, iface: str, outfile: Path, inject=True):
        outfile = Path(outfile)
        if outfile.exists():  # USBPcapCMD nie nadpisuje pliku: "Thread started with invalid write handle!"
            try:
                outfile.unlink()
            except OSError as e:
                die(f"Nie mogę usunąć starego nagrania {outfile} (otwarte w Wiresharku?): {e}")
        while True:
            args = [self.exe, "-d", iface, "-o", str(outfile), "-A"]
            if self.new_devices_flag:
                args.append("--capture-from-new-devices")
            if inject:
                args.append("--inject-descriptors")
            log = tempfile.TemporaryFile()
            p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            time.sleep(1.0)
            if p.poll() is None:
                p._log = log
                return p
            log.seek(0)
            msg = _decode_tool_output(log.read()).strip()
            if self.new_devices_flag:
                self.new_devices_flag = False  # starsza wersja USBPcapCMD bez tej opcji
                continue
            hint = ("Nie da się zapisać pliku wyjściowego (istnieje / zablokowany / zła ścieżka)."
                    if "write handle" in msg else "Sprawdź uprawnienia Administratora i instalację USBPcap.")
            die(f"USBPcapCMD zakończył się od razu (kod {p.returncode}): {msg or '(brak komunikatu)'}\n{hint}\n"
                f"Polecenie: {' '.join(args)}")

    def stop(self, p):
        time.sleep(0.7)  # niech ostatnie pakiety trafią do pliku
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
        try:
            p._log.close()
        except Exception:
            pass


def choose_iface(up: UsbPcap, explicit, vid, pid, product) -> str:
    ifaces = up.interfaces()
    if explicit:
        e = explicit if explicit.startswith("\\\\") else rf"\\.\USBPcap{explicit.lstrip('USBPcap')}"
        return e
    if not ifaces:
        die("USBPcapCMD nie zwrócił żadnych interfejsów. Czy USBPcap jest zainstalowany (i był restart po instalacji)?")
    word = (product or "").split()[0] if product else ""
    for n, (val, disp) in enumerate(ifaces, 1):
        print(f"  [{n}] {disp} ({val})")
        for d in up.devices(val):
            depth = 0 if "parent" not in d else (2 if "_" in d.get("parent", "") else 1)
            name = d.get("display", "")
            mark = "   <== ?" if word and word.lower() in name.lower() else ""
            print(f"        {'   ' * depth}{name}{mark}")
    print("  Szukam gitary po VID/PID (krótkie nagrania z --inject-descriptors)...")
    hits = []
    with tempfile.TemporaryDirectory() as td:
        for val, _disp in ifaces:
            f = Path(td) / "probe.pcap"
            p = up.start(val, f, inject=True)
            time.sleep(0.5)
            up.stop(p)
            try:
                _g, devs = group_devices(pair_control(parse_usbpcap(read_capture(f))))
            except Exception:
                devs = {}
            if any(vp == (vid, pid) for vp in devs.values()):
                hits.append(val)
    if len(hits) == 1:
        print(f"  Gitara (VID 0x{vid:04X} PID 0x{pid:04X}) jest na {hits[0]}")
        return hits[0]
    if not hits:
        warn("Nie znalazłem urządzenia po VID/PID w żadnym interfejsie (stara wersja USBPcap bez --inject-descriptors?).")
    else:
        warn(f"Urządzenie widoczne na kilku interfejsach: {hits}")
    while True:
        a = ask(f"  Wybierz interfejs [1-{len(ifaces)}]: ")
        if a.isdigit() and 1 <= int(a) <= len(ifaces):
            return ifaces[int(a) - 1][0]


def record_enumeration(up: UsbPcap, iface: str, path: Path, what: str):
    p = up.start(iface, path, inject=True)
    print(f"  Nagrywam {iface} -> {path.name}")
    print(f"  >>> ODEPNIJ {what}, odczekaj ~2 s i WEPNIJ z powrotem DO TEGO SAMEGO PORTU.")
    ask("  >>> Gdy Windows skończy instalację (~5 s po wpięciu), naciśnij Enter: ")
    time.sleep(1.0)
    up.stop(p)


def enum_loop(a, up, tshark, iface, vid, pid, path: Path, what: str) -> dict:
    while True:
        if not a.from_pcap:
            record_enumeration(up, iface, path, what)
        en = analyze_enum(path, vid, pid)
        print_enum(en)
        if en["key"] is None:
            warn(f"W nagraniu nie ma device descriptora VID 0x{vid:04X} PID 0x{pid:04X}.")
            if a.from_pcap:
                die("Nie da się kontynuować bez deskryptorów.")
            ask("  Enter = nagraj ponownie (odepnij/wepnij w trakcie nagrania): ")
            continue
        tshark_crosscheck(tshark, path, en["key"], count_dev_packets(path, en["key"]))
        if en["missing_critical"] or en["missing_other"]:
            print("\n  Brakuje w nagraniu:")
            for m in en["missing_critical"]:
                print(f"    [krytyczne] {m}")
            for m in en["missing_other"]:
                print(f"    [opcjonalne] {m}")
            if en["missing_other"] and not en["missing_critical"]:
                print("    (Windows przy enumeracji nie pyta o wszystkie stringi, a zapytań huba USBPcap nie widzi -\n"
                      "     powtórka nic nie da. Brakujące stringi zostaną pobrane w następnym kroku przez\n"
                      "     HidD_GetIndexedString.)")
                return en
            if not a.from_pcap:
                c = ask("  [p]owtórz nagranie / [k]ontynuuj z tym, co jest: ").lower()
                if c.startswith("p"):
                    continue
            if en["missing_critical"] and a.from_pcap:
                warn("Brakuje krytycznych deskryptorów w podanym pliku.")
        return en


# --------------------------------------------------------------------------- hidapi

def import_hid():
    try:
        import hid  # noqa
    except ImportError:
        die("Brak modułu 'hid'. Zainstaluj: py -m pip install hidapi")
    if not hasattr(hid, "device"):
        die("Zaimportowany moduł 'hid' to inny pakiet (pip 'hid'). Zrób: py -m pip uninstall hid && py -m pip install hidapi")
    return hid


def hid_candidates(hid, product, vid, pid) -> list[dict]:
    devs = hid.enumerate()
    m = []
    if product:
        m = [d for d in devs if (d.get("product_string") or "").strip() == product.strip()]
    if vid is not None:
        m = [d for d in (m or devs) if d["vendor_id"] == vid and (pid is None or d["product_id"] == pid)]
    m.sort(key=lambda d: (d.get("usage_page") != 1 or d.get("usage") not in (4, 5), d.get("interface_number", 0)))
    return m


def pick_hid(hid, product, vid, pid, index=None, wait_s=0.0) -> dict:
    t_end = time.time() + wait_s
    while True:
        m = hid_candidates(hid, product, vid, pid)
        if m or time.time() >= t_end:
            break
        time.sleep(0.5)
    if not m:
        print("  Widoczne urządzenia HID:")
        for d in hid.enumerate():
            print(f"    {d['vendor_id']:04X}:{d['product_id']:04X} {d.get('product_string')!r} "
                  f"usage {d.get('usage_page', 0):04X}:{d.get('usage', 0):04X}")
        die(f"Nie znaleziono urządzenia HID o product string {product!r}"
            + (f" / VID 0x{vid:04X}" if vid is not None else "") + ". Użyj --product albo --vid/--pid.\n"
            "Jeśli urządzenie jest na liście z pustą nazwą, przestało odpowiadać na stringi - odepnij i wepnij je.")
    if len(m) > 1:
        print("  Pasujące kolekcje HID:")
        for n, d in enumerate(m):
            print(f"    [{n}] usage {d.get('usage_page', 0):04X}:{d.get('usage', 0):04X} "
                  f"interfejs {d.get('interface_number')} {d['path']!r}")
        if index is None:
            print("  Wybieram [0] (Generic Desktop Joystick/Gamepad, jeśli jest). Inaczej: --hid-index N")
            index = 0
    return m[index or 0]


def print_hid_info(d):
    print(f"  VID:PID       {d['vendor_id']:04X}:{d['product_id']:04X}")
    print(f"  manufacturer  {d.get('manufacturer_string')!r}")
    print(f"  product       {d.get('product_string')!r}")
    print(f"  serial        {d.get('serial_number')!r}")
    print(f"  release       0x{d.get('release_number', 0):04X}")
    print(f"  usage         page 0x{d.get('usage_page', 0):04X} usage 0x{d.get('usage', 0):04X}")
    print(f"  interfejs     {d.get('interface_number')}")
    print(f"  ścieżka       {d['path'].decode(errors='replace') if isinstance(d['path'], bytes) else d['path']}")


def hid_info_json(d):
    return {"vendor_id": d["vendor_id"], "product_id": d["product_id"],
            "manufacturer": d.get("manufacturer_string"), "product": d.get("product_string"),
            "serial": d.get("serial_number"), "release_number": d.get("release_number"),
            "usage_page": d.get("usage_page"), "usage": d.get("usage"),
            "interface_number": d.get("interface_number"),
            "path": d["path"].decode(errors="replace") if isinstance(d["path"], bytes) else d["path"]}


def open_hid(hid, info):
    dev = hid.device()
    try:
        dev.open_path(info["path"])
    except Exception as e:
        die(f"Nie mogę otworzyć urządzenia HID: {e}")
    return dev


def hid_report_descriptor(dev) -> tuple[bytes | None, str | None]:
    fn = getattr(dev, "get_report_descriptor", None)
    if fn is None:
        return None, "ta wersja hidapi nie ma get_report_descriptor (potrzebne hidapi >= 0.14)"
    try:
        try:
            return bytes(fn()), None  # hidapi 0.15: bez argumentów
        except TypeError:
            return bytes(fn(4096)), None  # starsze: max_length
    except Exception as e:
        return None, str(e)


def probe_reports(dev, rmap) -> dict:
    out = {"feature": {}, "input": {}, "note": "odpowiedź hidapi: pierwszy bajt = report ID (0 gdy brak ID)"}
    for (t, rid), _bits in rmap["lengths"].items():
        if t not in ("Feature", "Input"):
            continue
        n = report_len(rmap, t, rid) + (0 if rmap["uses_report_ids"] else 1)
        fn = getattr(dev, "get_feature_report" if t == "Feature" else "get_input_report", None)
        k = "feature" if t == "Feature" else "input"
        if fn is None:
            out[k][rid_key(rid)] = {"error": "brak funkcji w tej wersji hidapi"}
            continue
        try:
            r = bytes(fn(rid or 0, n))
            out[k][rid_key(rid)] = {"hex": r.hex(), "len": len(r)}
            print(f"  get_{k}_report(ID {rid_key(rid)}): {len(r)} B  {r.hex(' ')}")
        except Exception as e:
            out[k][rid_key(rid)] = {"error": str(e)}
            print(f"  get_{k}_report(ID {rid_key(rid)}): błąd: {e}")
    return out


class HidReader(threading.Thread):
    def __init__(self, dev, size, uses_ids):
        super().__init__(daemon=True)
        self.dev, self.size, self.uses_ids = dev, size, uses_ids
        self.log: list[tuple[float, bytes]] = []
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.error = None

    def run(self):
        while not self.stop_ev.is_set():
            try:
                r = self.dev.read(self.size, 100)
            except Exception as e:
                self.error = str(e)
                break
            if r:
                with self.lock:
                    self.log.append((time.perf_counter(), bytes(r)))

    def between(self, t0, t1):
        with self.lock:
            return [x for x in self.log if t0 <= x[0] <= t1]

    def last_before(self, t):
        last = {}
        with self.lock:
            for ts, r in self.log:
                if ts > t:
                    break
                last[rid_of(r, self.uses_ids)] = (ts, r)
        return list(last.values())

    def stop(self):
        self.stop_ev.set()
        self.join(1.0)


def rid_of(r: bytes, uses_ids: bool):
    return r[0] if uses_ids and r else None


# --------------------------------------------------------------------------- baseline / timing

def analyze_baseline(reports: list[bytes], rmap) -> dict:
    uses = rmap["uses_report_ids"]
    by: dict = {}
    for r in reports:
        by.setdefault(rid_of(r, uses), []).append(r)
    res = {}
    for rid, lst in by.items():
        ln = statistics.mode(len(x) for x in lst)
        lst = [x for x in lst if len(x) == ln]
        neutral = bytes(statistics.median_low([x[i] for x in lst]) for i in range(ln))
        noisy, ranges = set(), {}
        for i in range(ln):
            o, a = 0, 0xFF
            for x in lst:
                o |= x[i]
                a &= x[i]
            for b in range(8):
                if (o ^ a) >> b & 1:
                    noisy.add((i, b))
            if o ^ a:
                ranges[i] = (min(x[i] for x in lst), max(x[i] for x in lst))
        expanded, noisy_els = set(noisy), []
        for (i, b) in noisy:
            e = element_at(rmap, "Input", rid, i, b)
            if e and e not in noisy_els:
                noisy_els.append(e)
                for k in range(e["size"]):
                    pb = e["bit"] + k + (8 if uses else 0)
                    expanded.add((pb // 8, pb % 8))
        el_ranges = []
        for e in noisy_els:
            vals = [element_value(x, e, uses) for x in lst]
            el_ranges.append({"usage": e["usage"], "byte": e["byte"], "bit": e["bit_in_byte"], "size": e["size"],
                              "min": min(vals), "max": max(vals), "median": statistics.median_low(vals)})
        res[rid] = {"neutral": neutral, "noisy": noisy, "mask": expanded, "count": len(lst),
                    "byte_ranges": ranges, "noisy_elements": el_ranges}
    return res


def print_baseline(base, rmap):
    for rid, b in base.items():
        print(f"  Raport ID {rid_key(rid)}: {b['count']} próbek, neutralny ({len(b['neutral'])} B): {b['neutral'].hex(' ')}")
        if b["noisy"]:
            print(f"    Bity szumiące: " + ", ".join(f"{i}.{k}" for i, k in sorted(b["noisy"])))
            for i, (lo, hi) in sorted(b["byte_ranges"].items()):
                print(f"      bajt {i}: zakres 0x{lo:02X}..0x{hi:02X}")
            for e in b["noisy_elements"]:
                print(f"      pole {e['usage']} (bajt {e['byte']}.{e['bit']}, {e['size']} b): "
                      f"{e['min']}..{e['max']}, mediana {e['median']}")
        else:
            print("    Brak szumu (wszystkie próbki identyczne).")


def _masked(r: bytes, base, uses_ids) -> bytes:
    """Raport z wyzerowanymi bitami szumiącymi (wg baseline), do porównań 'czy się zmienił'."""
    b = (base or {}).get(rid_of(r, uses_ids))
    if not b:
        return r
    out = bytearray(r)
    for (i, k) in b["mask"]:
        if i < len(out):
            out[i] &= ~(1 << k) & 0xFF
    return bytes(out)


def analyze_timing(path, vid, pid, ep_addr=None, fallback_key=None, base=None, uses_ids=False) -> dict:
    pkts = parse_usbpcap(read_capture(path))
    groups, devices = group_devices(pair_control(pkts))
    key = pick_group(groups, devices, vid, pid) or fallback_key
    res = {"pcap": Path(path).name, "device_key": list(key) if key else None}
    if key is None:
        res["error"] = "nie znaleziono urządzenia w nagraniu timingu"
        return res
    ints = [p for p in pkts if p.xfer == 1 and (p.ep & 0x80) and (p.info & 1) and (p.bus, p.dev) == key
            and p.data and p.status == 0]
    if ep_addr is None and ints:
        ep_addr = statistics.mode(p.ep for p in ints)
    ints = [p for p in ints if p.ep == ep_addr] if ep_addr is not None else ints
    res["endpoint"] = f"0x{ep_addr:02X}" if ep_addr is not None else None
    res["count"] = len(ints)
    all_ts = [p.ts for p in pkts]
    res["duration_s"] = round(max(all_ts) - min(all_ts), 3) if all_ts else 0
    if len(ints) < 3:
        res["mode"] = "on_change"
        res["note"] = "w spoczynku prawie brak raportów -> urządzenie wysyła tylko przy zmianie"
        return res
    dts = sorted((b.ts - a.ts) * 1000 for a, b in zip(ints, ints[1:]))
    q = lambda f: dts[min(len(dts) - 1, int(f * len(dts)))]  # noqa: E731
    same = sum(1 for a, b in zip(ints, ints[1:]) if a.data == b.data) / (len(ints) - 1)
    same_m = sum(1 for a, b in zip(ints, ints[1:])
                 if _masked(a.data, base, uses_ids) == _masked(b.data, base, uses_ids)) / (len(ints) - 1)
    res.update(period_ms_median=round(statistics.median(dts), 3), period_ms_p10=round(q(0.1), 3),
               period_ms_p90=round(q(0.9), 3), period_ms_min=round(dts[0], 3), period_ms_max=round(dts[-1], 3),
               identical_consecutive_fraction=round(same, 3),
               identical_consecutive_fraction_ignoring_noise=round(same_m, 3),
               report_length=statistics.mode(len(p.data) for p in ints))
    if same >= 0.5:
        res["mode"] = "cyclic"
        res["note"] = "raporty idą cyklicznie (także bez żadnej zmiany)"
    elif base and same_m >= 0.5:
        res["mode"] = "cyclic"
        res["note"] = ("raporty idą cyklicznie; różnią się tylko bitami szumiącymi - nie wiadomo, czy bez szumu "
                       "też by szły (przy stałym okresie ~bInterval to prawie pewne)")
    else:
        res["mode"] = "on_change_or_noise"
        res["note"] = "kolejne raporty zwykle się różnią - nie da się rozstrzygnąć, czy urządzenie wysyła bez zmian"
    return res


def print_timing(t):
    if "error" in t:
        warn(t["error"])
        return
    print(f"  {t['pcap']}: endpoint {t.get('endpoint')}, {t['count']} raportów interrupt IN w {t['duration_s']} s")
    if "period_ms_median" in t:
        print(f"  Okres: mediana {t['period_ms_median']} ms (p10 {t['period_ms_p10']}, p90 {t['period_ms_p90']}, "
              f"min {t['period_ms_min']}, max {t['period_ms_max']}); identyczne kolejne: "
              f"{t['identical_consecutive_fraction'] * 100:.0f}% "
              f"(bez bitów szumiących: {t['identical_consecutive_fraction_ignoring_noise'] * 100:.0f}%)")
    print(f"  Tryb: {t['mode']} - {t['note']}")


def baseline_with_timing(up, iface, reader, seconds, timing_path, uses_ids, rmap, dev):
    p = up.start(iface, timing_path, inject=True)
    t0 = time.perf_counter()
    time.sleep(seconds)
    t1 = time.perf_counter()
    up.stop(p)
    reps = [r for _t, r in reader.between(t0, t1)]
    if reps:
        return reps
    warn("W trakcie baseline nie przyszedł żaden raport (urządzenie wysyła tylko przy zmianie?).")
    for (t, rid) in rmap["lengths"]:
        if t == "Input":
            try:
                r = bytes(dev.get_input_report(rid or 0, report_len(rmap, t, rid) + (0 if uses_ids else 1)))
                if not uses_ids and r and r[0] == 0:
                    r = r[1:]
                reps.append(r)
            except Exception:
                pass
    if reps:
        print("  Użyto get_input_report jako stanu neutralnego.")
        return reps
    ask("  Wciśnij i puść dowolny przycisk, potem odczekaj 2 s i naciśnij Enter: ")
    lst = reader.last_before(time.perf_counter())
    return [r for _t, r in lst]


# --------------------------------------------------------------------------- kalibracja

def state_window(reader, t0, dur):
    """Ostatni raport przed t0 (stan) + wszystkie raporty w oknie."""
    return [r for _t, r in reader.last_before(t0)] + [r for _t, r in reader.between(t0, t0 + dur)]


def analyze_press(held, released, base, rmap):
    uses = rmap["uses_report_ids"]
    res = {"bits": [], "elements": [], "warnings": [], "info": []}
    for rid, b in base.items():
        lst = [r for r in held if rid_of(r, uses) == rid and len(r) == len(b["neutral"])]
        if not lst:
            continue
        N = b["neutral"]
        changed = []
        for i in range(len(N)):
            for k in range(8):
                if (i, k) in b["mask"]:
                    continue
                frac = sum(((x[i] ^ N[i]) >> k) & 1 for x in lst) / len(lst)
                if frac >= 0.8:
                    changed.append((i, k))
                elif frac > 0.2:
                    res["info"].append(f"bit {i}.{k} zmienił się tylko w {frac * 100:.0f}% raportów")
        held_med = bytes(statistics.median_low([x[i] for x in lst]) for i in range(len(N)))
        els = []
        for (i, k) in changed:
            res["bits"].append({"report_id": rid, "byte": i, "bit": k, "neutral": (N[i] >> k) & 1})
            e = element_at(rmap, "Input", rid, i, k)
            if e is not None and e not in els:
                els.append(e)
        for e in els:
            res["elements"].append({"report_id": rid, "usage": e["usage"], "kind": e["kind"], "byte": e["byte"],
                                    "bit": e["bit_in_byte"], "size": e["size"],
                                    "neutral": element_value(N, e, uses), "held": element_value(held_med, e, uses)})
        if changed:
            res.setdefault("held_report", {})[rid_key(rid)] = held_med.hex()
        # pola szumiące: wartość wyraźnie poza zakresem z baseline
        for ne in b["noisy_elements"]:
            e = element_at(rmap, "Input", rid, ne["byte"], ne["bit"])
            if e is None:
                continue
            vals = [element_value(x, e, uses) for x in lst]
            med = statistics.median_low(vals)
            span = max(1, ne["max"] - ne["min"])
            if med < ne["min"] - span or med > ne["max"] + span:
                res["info"].append(f"pole szumiące {ne['usage']} poza zakresem baseline: {med} "
                                   f"(baseline {ne['min']}..{ne['max']}) - ruch gitary albo to wejście?")
        # powrót do neutralnego po puszczeniu
        rel = [r for r in released if rid_of(r, uses) == rid and len(r) == len(N)]
        if rel and changed:
            last = rel[-1]
            stuck = [f"{i}.{k}" for (i, k) in changed if ((last[i] ^ N[i]) >> k) & 1]
            if stuck:
                res["warnings"].append(f"po puszczeniu nie wróciły do stanu neutralnego bity: {', '.join(stuck)}")
    if not res["bits"]:
        res["warnings"].append("nie wykryto żadnej zmiany względem baseline")
    elif len(res["elements"]) > 1:
        res["warnings"].append("zmieniło się kilka pól naraz: " + ", ".join(e["usage"] for e in res["elements"]))
    elif not res["elements"]:
        if len(res["bits"]) > 1:
            res["warnings"].append(f"zmieniło się {len(res['bits'])} bitów spoza znanych pól")
    return res


def print_press(name, res):
    for e in res["elements"]:
        what = f"wartość {e['neutral']} -> {e['held']}" if e["size"] > 1 else f"bit {e['neutral']} -> {e['held']}"
        print(f"    {name}: {e['usage']} (ID {rid_key(e['report_id'])}, bajt {e['byte']} bit {e['bit']}, "
              f"{e['size']} b): {what}")
    if res["bits"] and not res["elements"]:
        print(f"    {name}: bity " + ", ".join(f"{b['byte']}.{b['bit']}" for b in res["bits"]))
    for w in res["warnings"]:
        warn(w)
    for i in res["info"]:
        print(f"    (i) {i}")


def press_signature(res):
    if res["elements"]:
        return frozenset((e["report_id"], e["byte"], e["bit"], e["held"]) for e in res["elements"])
    return frozenset((b["report_id"], b["byte"], b["bit"]) for b in res["bits"])


def calibrate(reader, base, rmap, names, hold_s) -> dict:
    results = {}
    for name in names:
        while True:
            a = ask(f"\n  [{name}] Wciśnij i TRZYMAJ, potem Enter (s = pomiń): ").lower()
            if a == "s":
                results[name] = {"skipped": True}
                break
            t0 = time.perf_counter()
            time.sleep(hold_s)
            held = state_window(reader, t0, hold_s)
            ask(f"  [{name}] PUŚĆ, potem Enter: ")
            t1 = time.perf_counter()
            time.sleep(0.4)
            released = state_window(reader, t1, 0.4)
            res = analyze_press(held, released, base, rmap)
            print_press(name, res)
            if res["warnings"]:
                c = ask("  Enter = zaakceptuj, p = powtórz, s = pomiń: ").lower()
                if c == "p":
                    continue
                if c == "s":
                    results[name] = {"skipped": True}
                    break
            results[name] = res
            break
    # konflikty
    done = [(n, r) for n, r in results.items() if not r.get("skipped") and r["bits"]]
    for i, (n1, r1) in enumerate(done):
        for n2, r2 in done[i + 1:]:
            if press_signature(r1) == press_signature(r2):
                msg = f"KONFLIKT: {n1} i {n2} dają identyczną zmianę - nie da się ich rozróżnić"
                warn(msg)
                r1["warnings"].append(msg)
                r2["warnings"].append(msg)
            else:
                b1 = {(b["report_id"], b["byte"], b["bit"]) for b in r1["bits"]}
                b2 = {(b["report_id"], b["byte"], b["bit"]) for b in r2["bits"]}
                if b1 & b2 and not (r1["elements"] and r2["elements"]
                                    and all(e["size"] > 1 for e in r1["elements"] + r2["elements"])):
                    msg = f"{n1} i {n2} współdzielą bity {sorted(b1 & b2)}"
                    warn(msg)
                    r1["warnings"].append(msg)
    return results


def inputs_json(results):
    out = {}
    for n, r in results.items():
        if r.get("skipped"):
            out[n] = {"skipped": True}
            continue
        out[n] = {"bits": [dict(b, report_id=b["report_id"]) for b in r["bits"]], "fields": r["elements"],
                  "held_report_hex": r.get("held_report", {}), "warnings": r["warnings"], "info": r["info"]}
    return out


# --------------------------------------------------------------------------- profil / porównania

def enum_to_json(en) -> dict:
    d = en["device"]
    f = decode_fields(1, d) if d else {}
    return {
        "usb_address": {"bus": en["key"][0], "address": en["key"][1]} if en["key"] else None,
        "summary": {k: f.get(k) for k in ("bcdUSB", "bcdDevice", "bMaxPacketSize0", "idVendor", "idProduct",
                                           "bDeviceClass", "bNumConfigurations")},
        "descriptors": {
            "device": hx(d),
            "configuration": {str(k): v.hex() for k, v in en["configs"].items()},
            "hid_report": {str(k): v.hex() for k, v in en["hid_report"].items()},
            "hid_class_descriptor": {str(k): v.hex() for k, v in en["hid_desc"].items()},
            "langids": en["langids"],
            "strings": [{"index": i, "langid": l, "hex": s["raw"].hex(), "text": decode_string(s["raw"]) if i else None,
                         "source": s["source"]} for (i, l), s in sorted(en["strings"].items())],
            "bos": hx(en["bos"]),
            "device_qualifier": en["qualifier"],
            "other": {k: v.hex() for k, v in en["other_desc"].items()},
        },
        "string_probe": en.get("string_probe"),
        "interfaces": en["ifaces"],
        "class_vendor_in": en["class_vendor_in"],
        "failed_get_descriptor": en["failed_desc"],
        "control_log": en["control_log"],
        "missing": en["missing_critical"] + en["missing_other"],
    }


def hidd_get_strings(path, indices) -> dict:
    """HidD_GetIndexedString przez systemowy sterownik HID (bez WinUSB). idx -> treść UTF-16LE albo None."""
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hdll = ctypes.WinDLL("hid")
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    hdll.HidD_GetIndexedString.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG]
    hdll.HidD_GetIndexedString.restype = wintypes.BOOLEAN
    p = path.decode(errors="replace") if isinstance(path, bytes) else path
    h = k32.CreateFileW(p, 0, 3, None, 3, 0, None)  # bez praw R/W, FILE_SHARE_READ|WRITE, OPEN_EXISTING
    if h is None or h == ctypes.c_void_p(-1).value:
        warn(f"CreateFile na urządzeniu HID nie powiódł się (błąd {ctypes.get_last_error()})")
        return {}
    def get(i):
        buf = ctypes.create_string_buffer(512)
        if not hdll.HidD_GetIndexedString(h, i, buf, 512):
            return None
        raw, n = buf.raw, 0
        while n + 1 < len(raw) and raw[n:n + 2] != b"\0\0":
            n += 2
        return raw[:n]

    out, good = {}, None
    try:
        for i in indices:
            out[i] = get(i)
            if out[i] is not None:
                good = i if good is None else good
            elif good is not None and get(good) is None:
                # gitara RT-GH po pytaniu o nieistniejący string przestaje odpowiadać na wszystkie stringi
                warn(f"po pytaniu o string {i} urządzenie przestało odpowiadać na stringi - przerywam. "
                     "ODEPNIJ i WEPNIJ urządzenie, zanim użyjesz go dalej.")
                out["hung"] = i
                break
    finally:
        k32.CloseHandle(h)
    return out


def probe_strings(up, iface, info, en, vid, pid, path: Path, extra=False):
    """Pyta o stringi przez HidD_GetIndexedString, nagrywając to USBPcapem (surowe bajty).
    Windows przy enumeracji nie pyta o wszystko (np. iConfiguration), a zapytań huba USBPcap nie widzi.
    Domyślnie tylko indeksy 0..najwyższy użyty w deskryptorach (łapie też ukryte "dziury", np. wersję),
    bo pytanie o nieistniejący string potrafi zawiesić obsługę stringów w urządzeniu."""
    refs = string_indices(en["device"], en["configs"])
    indices = list(range(0, max(refs, default=0) + 1))
    if extra:
        indices += [i for i in range(len(indices), 33)] + [0xEE]
    p = up.start(iface, path, inject=True) if iface else None
    try:
        res = hidd_get_strings(info["path"], indices)
    finally:
        if p:
            up.stop(p)
    hung = res.pop("hung", None)
    if not res or all(v is None for v in res.values()):
        warn("Urządzenie nie odpowiada na żadne zapytanie o string (zawieszone po wcześniejszym teście?). "
             "Odepnij i wepnij je, potem uruchom skrypt ponownie.")
    probe, captured = {}, {}
    if p:
        groups, devices = group_devices(pair_control(parse_usbpcap(read_capture(path))))
        key = pick_group(groups, devices, vid, pid) or en["key"]
        for c in groups.get(key, []):
            if c.is_get_desc(3, 0x80) and c.complete:
                _bm, _br, wv, wi, _wl = c.fields
                i = wv & 0xFF
                k = (0, 0) if i == 0 else (i, wi)
                if c.status == 0 and c.response:
                    if k not in captured or len(c.response) > len(captured[k]):
                        captured[k] = c.response
                    probe[i] = "OK"
                else:
                    probe.setdefault(i, status_name(c.status))
    for k, raw in captured.items():
        if k not in en["strings"]:
            en["strings"][k] = {"raw": raw, "source": "pcap (HidD_GetIndexedString)"}
    if (0, 0) in en["strings"]:
        en["langids"] = langids_of(en["strings"][(0, 0)]["raw"])
    lang = en["langids"][0] if en["langids"] else 0x0409
    for i, body in res.items():
        if body is None:
            probe.setdefault(i, "brak (HidD)")
            continue
        probe.setdefault(i, "OK (HidD, nie złapane w pcap)")
        k = (0, 0) if i == 0 else (i, lang)
        if k not in en["strings"]:
            en["strings"][k] = {"raw": bytes([2 + len(body), 3]) + body, "source": "HidD_GetIndexedString (odtworzony)"}
    if (0, 0) in en["strings"]:
        en["langids"] = langids_of(en["strings"][(0, 0)]["raw"])
    en["string_probe"] = {str(i): s for i, s in sorted(probe.items())}
    if hung is not None:
        en["string_probe"]["hung_after"] = str(hung)
    ok = [i for i in sorted(probe) if probe[i].startswith("OK")]
    print(f"  Stringi odpowiadające: {ok}")
    for (i, l), s in sorted(en["strings"].items()):
        if i:
            tag = refs.get(i, "NIEUŻYWANY w deskryptorach")
            print(f"    {i:3d} 0x{l:04X} {decode_string(s['raw'])!r:<28} {tag}  ({s['source']})")
    bad = [i for i in sorted(probe) if not probe[i].startswith("OK")]
    if bad:
        print(f"  Bez odpowiedzi: {bad[:8]}{' ...' if len(bad) > 8 else ''}"
              + (f"  (0xEE: {probe.get(0xEE)} -> brak MS OS 1.0 descriptor)" if 0xEE in bad else ""))
    check_enum(en)


def langids_of(raw: bytes) -> list[int]:
    n = min(len(raw), raw[0]) if raw else 0
    return [struct.unpack_from("<H", raw, i)[0] for i in range(2, n - 1, 2)]


def fill_strings_from_hidapi(en, info):
    d = en["device"]
    if not d or len(d) < 18 or not (info.get("product_string") or "").strip():
        return  # pusty product = hidapi podaje zastępcze nazwy z Windows, nie stringi z urządzenia
    f = decode_fields(1, d)
    lang = en["langids"][0] if en["langids"] else 0x0409
    src = {f.get("iManufacturer"): info.get("manufacturer_string"), f.get("iProduct"): info.get("product_string"),
           f.get("iSerialNumber"): info.get("serial_number")}
    for idx, text in src.items():
        if idx and text and (idx, lang) not in en["strings"]:
            en["strings"][(idx, lang)] = {"raw": make_string_desc(text), "source": "hidapi (odtworzony, niepewny)"}
            print(f"  String {idx} odtworzony z hidapi: {text!r}")


def compare_bytes(cat, exp, act, namer=None, limit=32):
    if exp is None and act is None:
        return []
    if exp is None:
        return [(cat, "nie ma w profilu, jest w nowym nagraniu")]
    if act is None:
        return [(cat, "BRAK w nowym nagraniu")]
    if exp == act:
        return []
    out = []
    if len(exp) != len(act):
        out.append((cat, f"długość: oryginał {len(exp)} B, Pico {len(act)} B"))
    n = 0
    for i in range(min(len(exp), len(act))):
        if exp[i] != act[i]:
            n += 1
            if n <= limit:
                nm = f" {namer(exp, i)}" if namer else ""
                out.append((cat, f"bajt {i}{nm}: oryginał 0x{exp[i]:02X}, Pico 0x{act[i]:02X}"))
    if n > limit:
        out.append((cat, f"... i {n - limit} kolejnych różnych bajtów"))
    return out


def compare_enum(prof, en) -> list[tuple[str, str]]:
    pd = prof["usb"]["descriptors"]
    cur = enum_to_json(en)["descriptors"]
    diffs = []
    diffs += compare_bytes("device", unhx(pd["device"]), unhx(cur["device"]), desc_field_at)
    for k in sorted(set(pd["configuration"]) | set(cur["configuration"])):
        diffs += compare_bytes(f"configuration#{k}", unhx(pd["configuration"].get(k)),
                               unhx(cur["configuration"].get(k)), desc_field_at)
    for k in sorted(set(pd["hid_report"]) | set(cur["hid_report"])):
        e = unhx(pd["hid_report"].get(k))
        rm = parse_report_descriptor(e) if e else None
        diffs += compare_bytes(f"hid_report[if {k}]", e, unhx(cur["hid_report"].get(k)),
                               (lambda _d, i, rm=rm: item_at(rm, i)) if rm else None)
    ps = {(s["index"], s["langid"]): s for s in pd["strings"]}
    cs = {(s["index"], s["langid"]): s for s in cur["strings"]}
    for k in sorted(set(ps) | set(cs)):
        a, b = ps.get(k), cs.get(k)
        if a and b and a["hex"] != b["hex"]:
            diffs.append((f"string {k[0]} lang 0x{k[1]:04X}", f"oryginał {a['text']!r} [{a['hex']}], Pico {b['text']!r} [{b['hex']}]"))
        elif a and not b:
            if a["source"] == "pcap":
                diffs.append((f"string {k[0]} lang 0x{k[1]:04X}", f"brak w nagraniu Pico (oryginał {a['text']!r})"))
        elif b and not a:
            diffs.append((f"string {k[0]} lang 0x{k[1]:04X}", f"Pico ma string {b['text']!r}, oryginał nie (w nagraniu)"))
    diffs += compare_bytes("bos", unhx(pd["bos"]), unhx(cur["bos"]), desc_field_at)
    psp, csp = prof["usb"].get("string_probe") or {}, en.get("string_probe") or {}
    if psp and csp:
        for k in sorted((set(psp) | set(csp)) - {"hung_after"}, key=int):
            a, b = psp.get(k, "?").startswith("OK"), csp.get(k, "?").startswith("OK")
            if a != b:
                diffs.append((f"string {k}", f"oryginał {'odpowiada' if a else 'nie odpowiada'}, "
                                             f"Pico {'odpowiada' if b else 'nie odpowiada'}"))
    pq, cq = pd.get("device_qualifier"), cur.get("device_qualifier")
    if (pq or {}).get("status") != (cq or {}).get("status") or (pq or {}).get("hex") != (cq or {}).get("hex"):
        diffs.append(("device_qualifier", f"oryginał {pq}, Pico {cq}"))
    pcv = {c["setup"]: c for c in prof["usb"]["class_vendor_in"]}
    ccv = {c["setup"]: c for c in en["class_vendor_in"]}
    for s in sorted(set(pcv) | set(ccv)):
        a, b = pcv.get(s), ccv.get(s)
        if a and b and (a["response"], a["status"]) != (b["response"], b["status"]):
            diffs.append((f"żądanie {s}", f"oryginał {a['status']} {a['response']}, Pico {b['status']} {b['response']}"))
        elif a and not b:
            diffs.append((f"żądanie {s}", "Windows nie wysłał go do Pico (inne deskryptory albo cache usbflags)"))
        elif b and not a:
            diffs.append((f"żądanie {s}", f"tylko u Pico: {b['status']} {b['response']}"))
    # statusy pozostałych żądań (np. SET_IDLE: STALL vs OK)
    def statuses(log):
        m = {}
        for c in log:
            m.setdefault(c["setup"], c["status"])
        return m
    ps_, cs_ = statuses(prof["usb"]["control_log"]), statuses(en["control_log"])
    for s in sorted(set(ps_) & set(cs_)):
        if ps_[s] != cs_[s] and s not in pcv:
            diffs.append((f"status żądania {s}", f"oryginał {ps_[s]}, Pico {cs_[s]}"))
    return diffs


# --------------------------------------------------------------------------- tryby

def hid_in_endpoint(en, info):
    for it in en["ifaces"]:
        if it["class"] == 3 and (info is None or info.get("interface_number") in (None, -1, it["number"])):
            for ep in it["endpoints"]:
                if ep["address"] & 0x80 and (ep["attributes"] & 3) == 3:
                    return ep["address"], ep["bInterval"]
    return None, None


def select_report_descriptor(en, info, hidapi_rd):
    ifn = info.get("interface_number") if info else None
    if ifn in en["hid_report"]:
        return en["hid_report"][ifn], f"pcap (interfejs {ifn})"
    if en["hid_report"]:
        k = sorted(en["hid_report"])[0]
        return en["hid_report"][k], f"pcap (interfejs {k})"
    if hidapi_rd:
        warn("Brak report descriptora w pcap - używam wersji z hidapi (może być zrekonstruowana).")
        return hidapi_rd, "hidapi"
    die("Brak report descriptora (ani w pcap, ani z hidapi).")


def compare_rd(pcap_rd, hid_rd, err):
    if hid_rd is None:
        print(f"  hidapi: {err}")
        return {"available": False, "error": err}
    print(f"  hidapi get_report_descriptor: {len(hid_rd)} B")
    if pcap_rd is None:
        return {"available": True, "hex": hid_rd.hex(), "match": None}
    if hid_rd == pcap_rd:
        print("  Zgodny bajt w bajt z pcap.")
        return {"available": True, "hex": hid_rd.hex(), "match": "bytes"}
    sa = rmap_signature(parse_report_descriptor(pcap_rd))
    sb = rmap_signature(parse_report_descriptor(hid_rd))
    print("  Różni się bajtowo od pcap (na Windows hidapi REKONSTRUUJE deskryptor z preparsed data,\n"
          "  więc to normalne). Do firmware'u używaj wersji z pcap.")
    if sa == sb:
        print("  Semantycznie zgodny (te same pola, offsety, rozmiary, usage, zakresy).")
        return {"available": True, "hex": hid_rd.hex(), "match": "semantic"}
    print("  UWAGA: różnice semantyczne:")
    for x, y in list(zip(sa, sb))[:10]:
        if x != y:
            print(f"    pcap  {x}\n    hidapi {y}")
    if len(sa) != len(sb):
        print(f"    liczba pól: pcap {len(sa)}, hidapi {len(sb)}")
    return {"available": True, "hex": hid_rd.hex(), "match": "different"}


def base_to_json(base):
    return {rid_key(rid): {"neutral_hex": b["neutral"].hex(), "samples": b["count"],
                           "noisy_bits": sorted([list(x) for x in b["noisy"]]),
                           "ignored_bits": sorted([list(x) for x in b["mask"]]),
                           "noisy_byte_ranges": {str(i): list(v) for i, v in b["byte_ranges"].items()},
                           "noisy_fields": b["noisy_elements"]} for rid, b in base.items()}


def run_capture(a, up, tshark, out: Path):
    hid = import_hid()
    step(1, "Urządzenie przez hidapi")
    info = pick_hid(hid, a.product, a.vid, a.pid, a.hid_index)
    vid, pid = info["vendor_id"], info["product_id"]
    print_hid_info(info)

    step(2, "Interfejsy USBPcap")
    iface = choose_iface(up, a.usbpcap, vid, pid, a.product) if not a.from_pcap else None

    step(3, "Enumeracja (USBPcap)")
    enum_path = Path(a.from_pcap) if a.from_pcap else out / "enum.pcap"
    en = enum_loop(a, up, tshark, iface, vid, pid, enum_path, "gitarę")
    info = pick_hid(hid, a.product, vid, pid, a.hid_index, wait_s=15)  # ścieżka mogła się zmienić po re-plugu
    print("\n  Stringi przez HidD_GetIndexedString (nagranie strings.pcap):")
    probe_strings(up, iface, info, en, vid, pid, out / "strings.pcap", a.probe_all_strings)
    fill_strings_from_hidapi(en, info)
    for m in en["missing_critical"] + en["missing_other"]:
        warn(f"nadal brak: {m}")

    step(4, "Report descriptor przez hidapi")
    dev = open_hid(hid, info)
    hid_rd, err = hid_report_descriptor(dev)
    rd, rd_src = select_report_descriptor(en, info, hid_rd)
    rd_cmp = compare_rd(rd if rd_src.startswith("pcap") else None, hid_rd, err)

    step(5, "Mapa pól report descriptora")
    rmap = parse_report_descriptor(rd)
    print_report_items(rmap)
    print_report_map(rmap)
    probes = probe_reports(dev, rmap)

    step(6, "Baseline + okres raportowania")
    uses = rmap["uses_report_ids"]
    in_len = max([report_len(rmap, t, r) for (t, r) in rmap["lengths"] if t == "Input"] or [64])
    reader = HidReader(dev, in_len + 1 + 64, uses)
    reader.start()
    timing = {"error": "pominięto (brak USBPcap)"}
    ep, binterval = hid_in_endpoint(en, info)
    ask(f"  Połóż gitarę nieruchomo i niczego nie dotykaj. Enter = start ({a.baseline_seconds:.0f} s): ")
    if iface:
        reps = baseline_with_timing(up, iface, reader, a.baseline_seconds, out / "timing.pcap", uses, rmap, dev)
    else:
        t0 = time.perf_counter()
        time.sleep(a.baseline_seconds)
        reps = [r for _t, r in reader.between(t0, time.perf_counter())]
    if not reps:
        die("Brak raportów do baseline.")
    base = analyze_baseline(reps, rmap)
    if iface:
        timing = analyze_timing(out / "timing.pcap", vid, pid, ep, en["key"], base, uses)
        timing["bInterval"] = binterval
    print_baseline(base, rmap)
    print_timing(timing)
    if binterval is not None:
        print(f"  (bInterval endpointu IN: {binterval})")

    inputs = {}
    if not a.skip_calibration:
        step(7, "Kalibracja wejść")
        names = [n.strip() for n in a.inputs.split(",") if n.strip()]
        inputs = inputs_json(calibrate(reader, base, rmap, names, a.hold_seconds))
    reader.stop()
    if reader.error:
        warn(f"hidapi read: {reader.error}")
    dev.close()

    step(8, "Zapis profilu")
    main_rid = max(base, key=lambda r: base[r]["count"])
    profile = {
        "format": 1, "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "tool": "capture.py", "product_match": a.product,
        "hidapi": hid_info_json(info),
        "usb": enum_to_json(en),
        "report_descriptor_source": rd_src,
        "report_descriptor_hidapi": rd_cmp,
        "report_map": rmap_to_json(rmap),
        "reports_probe": probes,
        "baseline": base_to_json(base),
        "neutral_report": {"report_id": main_rid, "hex": base[main_rid]["neutral"].hex()},
        "timing": timing,
        "inputs": inputs,
        "files": {"enum_pcap": enum_path.name, "timing_pcap": "timing.pcap" if iface else None},
    }
    if enum_path.resolve().parent != out:
        try:
            shutil.copy2(enum_path, out / "enum.pcap")
            profile["files"]["enum_pcap"] = "enum.pcap"
        except Exception:
            pass
    pf = Path(a.profile) if a.profile else out / "profile.json"
    pf.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Zapisano {pf}")
    skipped = [n for n, r in inputs.items() if r.get("skipped")]
    probl = [n for n, r in inputs.items() if not r.get("skipped") and r["warnings"]]
    if skipped:
        print(f"  Pominięte wejścia: {', '.join(skipped)}")
    if probl:
        print(f"  Wejścia z ostrzeżeniami: {', '.join(probl)}")
    if en["missing_critical"]:
        warn("Profil NIEKOMPLETNY: " + "; ".join(en["missing_critical"]))


def run_verify(a, up, tshark, out: Path):
    pf = Path(a.profile) if a.profile else out / "profile.json"
    if not pf.is_file():
        die(f"Brak {pf} - najpierw uruchom capture.py bez --verify z oryginalną gitarą.")
    prof = json.loads(pf.read_text(encoding="utf-8"))
    vid = a.vid if a.vid is not None else prof["hidapi"]["vendor_id"]
    pid = a.pid if a.pid is not None else prof["hidapi"]["product_id"]
    product = a.product if a.product_given else prof["hidapi"].get("product") or a.product
    hid = import_hid()

    step(1, "Pico przez hidapi")
    info = pick_hid(hid, product, vid, pid, a.hid_index)
    print_hid_info(info)
    step(2, "Interfejsy USBPcap")
    iface = choose_iface(up, a.usbpcap, vid, pid, product) if not a.from_pcap else None
    step(3, "Enumeracja Pico (USBPcap)")
    enum_path = Path(a.from_pcap) if a.from_pcap else out / "verify_enum.pcap"
    en = enum_loop(a, up, tshark, iface, vid, pid, enum_path, "Pico")
    info = pick_hid(hid, product, vid, pid, a.hid_index, wait_s=15)
    print("\n  Stringi przez HidD_GetIndexedString (nagranie verify_strings.pcap):")
    probe_strings(up, iface, info, en, vid, pid, out / "verify_strings.pcap", a.probe_all_strings)

    step(4, "Porównanie deskryptorów")
    diffs = compare_enum(prof, en)
    ph = prof["hidapi"]
    for k, n in (("manufacturer", "manufacturer_string"), ("product", "product_string"), ("serial", "serial_number"),
                 ("usage_page", "usage_page"), ("usage", "usage"), ("release_number", "release_number")):
        if ph.get(k) != info.get(n):
            diffs.append((f"hidapi {k}", f"oryginał {ph.get(k)!r}, Pico {info.get(n)!r}"))

    step(5, "Neutralny raport i timing Pico")
    rd_hex = prof["usb"]["descriptors"]["hid_report"]
    rd = unhx(next(iter(rd_hex.values()))) if rd_hex else unhx(prof["report_descriptor_hidapi"].get("hex"))
    rmap = parse_report_descriptor(rd)
    uses = rmap["uses_report_ids"]
    dev = open_hid(hid, info)
    in_len = max([report_len(rmap, t, r) for (t, r) in rmap["lengths"] if t == "Input"] or [64])
    reader = HidReader(dev, in_len + 1 + 64, uses)
    reader.start()
    ask(f"  Pico w spoczynku (nic nie wciśnięte). Enter = start ({a.baseline_seconds:.0f} s): ")
    ep, _bi = hid_in_endpoint(en, info)
    if iface:
        reps = baseline_with_timing(up, iface, reader, a.baseline_seconds, out / "verify_timing.pcap", uses, rmap, dev)
    else:
        t0 = time.perf_counter()
        time.sleep(a.baseline_seconds)
        reps = [r for _t, r in reader.between(t0, time.perf_counter())]
    reader.stop()
    dev.close()
    base = analyze_baseline(reps, rmap) if reps else {}
    timing = (analyze_timing(out / "verify_timing.pcap", vid, pid, ep, en["key"], base, uses)
              if iface else {"error": "pominięto"})
    print_baseline(base, rmap)
    print_timing(timing)
    for k, pb in prof["baseline"].items():
        rid = key_rid(k)
        exp = bytes.fromhex(pb["neutral_hex"])
        if rid not in base:
            diffs.append((f"neutralny raport ID {k}", "Pico nie wysłał takiego raportu w spoczynku"))
            continue
        act = base[rid]["neutral"]
        ign = {tuple(x) for x in pb["ignored_bits"]}
        if len(exp) != len(act):
            diffs.append((f"neutralny raport ID {k}", f"długość: oryginał {len(exp)} B, Pico {len(act)} B"))
        for i in range(min(len(exp), len(act))):
            bits = [kk for kk in range(8) if ((exp[i] ^ act[i]) >> kk) & 1 and (i, kk) not in ign]
            if bits:
                e = element_at(rmap, "Input", rid, i, bits[0])
                diffs.append((f"neutralny raport ID {k}", f"bajt {i} ({e['usage'] if e else '?'}): oryginał "
                              f"0x{exp[i]:02X}, Pico 0x{act[i]:02X} (bity {bits})"))
        if base[rid]["noisy"]:
            print(f"  (i) Pico ma szum na bitach: {sorted(base[rid]['noisy'])}")
    pt = prof.get("timing", {})
    if "error" not in timing and "error" not in pt:
        if pt.get("mode") != timing.get("mode"):
            diffs.append(("timing", f"tryb: oryginał {pt.get('mode')}, Pico {timing.get('mode')}"))
        pm, cm = pt.get("period_ms_median"), timing.get("period_ms_median")
        if pm and cm and abs(pm - cm) > max(0.25, 0.2 * pm):
            diffs.append(("timing", f"okres: oryginał {pm} ms, Pico {cm} ms"))

    step(6, "Wynik")
    if diffs:
        print(f"  RÓŻNICE ({len(diffs)}):")
        for c, d in diffs:
            print(f"    [{c}] {d}")
    else:
        print("  Brak różnic - Pico zgodne z profilem.")
    rep = {"created": datetime.datetime.now().isoformat(timespec="seconds"), "profile": str(pf),
           "differences": [{"what": c, "detail": d} for c, d in diffs], "pico_usb": enum_to_json(en),
           "pico_hidapi": hid_info_json(info), "pico_baseline": base_to_json(base), "pico_timing": timing}
    (out / "verify_report.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Zapisano {out / 'verify_report.json'}")
    return 1 if diffs else 0


def run_parse_only(a):
    if not a.from_pcap:
        die("--parse-only wymaga --from-pcap PLIK")
    en = analyze_enum(a.from_pcap, a.vid, a.pid)
    print_enum(en)
    if en["key"] is None:
        return 1
    for m in en["missing_critical"] + en["missing_other"]:
        warn(f"brak: {m}")
    for wi, rd in sorted(en["hid_report"].items()):
        print(f"\n  Mapa pól (interfejs {wi}):")
        rmap = parse_report_descriptor(rd)
        print_report_items(rmap)
        print_report_map(rmap)
    v, p = en["devices"][en["key"]]
    ep, _ = hid_in_endpoint(en, None)
    print()
    print_timing(analyze_timing(a.from_pcap, v, p, ep, en["key"]))
    return 0


def main():
    ap = argparse.ArgumentParser(description="Profilowanie gitary USB HID (Windows, USBPcap + hidapi).")
    hexint = lambda s: int(s, 16)  # noqa: E731
    ap.add_argument("--product", default=None, help=f'product string w hidapi (domyślnie "{DEFAULT_PRODUCT}")')
    ap.add_argument("--vid", type=hexint, help="VID (hex), zawęża/zastępuje wyszukiwanie po nazwie")
    ap.add_argument("--pid", type=hexint, help="PID (hex)")
    ap.add_argument("--hid-index", type=int, help="która z pasujących kolekcji HID (gdy jest ich kilka)")
    ap.add_argument("--usbpcap", help=r"interfejs USBPcap, np. \\.\USBPcap1 albo 1 (domyślnie: wykryj)")
    ap.add_argument("--usbpcapcmd", help="ścieżka do USBPcapCMD.exe")
    ap.add_argument("--tshark", help="ścieżka do tshark.exe")
    ap.add_argument("--out", default=".", help="katalog wyjściowy (domyślnie bieżący)")
    ap.add_argument("--profile", help="ścieżka profile.json (domyślnie OUT/profile.json)")
    ap.add_argument("--inputs", default=",".join(DEFAULT_INPUTS), help="lista wejść do kalibracji, po przecinku")
    ap.add_argument("--baseline-seconds", type=float, default=5.0)
    ap.add_argument("--hold-seconds", type=float, default=1.0)
    ap.add_argument("--skip-calibration", action="store_true")
    ap.add_argument("--probe-all-strings", action="store_true",
                    help="pytaj też o stringi 0..32 i 0xEE (UWAGA: oryginalna gitara może się zawiesić - potem re-plug)")
    ap.add_argument("--from-pcap", help="użyj istniejącego nagrania enumeracji zamiast nagrywać")
    ap.add_argument("--parse-only", action="store_true", help="tylko analiza --from-pcap (bez hidapi i USBPcap)")
    ap.add_argument("--verify", action="store_true", help="porównaj podłączone Pico z profile.json")
    a = ap.parse_args()
    a.product_given = a.product is not None
    if a.product is None:
        a.product = DEFAULT_PRODUCT

    if a.parse_only:
        return run_parse_only(a)

    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        die("Ten skrypt jest dla Windows (USBPcap).")
    exe = find_usbpcapcmd(a.usbpcapcmd)
    tshark = find_tshark(a.tshark)
    print(f"USBPcapCMD: {exe or 'NIE ZNALEZIONO'}\ntshark:     {tshark or 'nie znaleziono (cross-check pominięty)'}")
    if not exe:
        die("Nie znaleziono USBPcapCMD.exe. Zainstaluj Wireshark z komponentem USBPcap albo podaj --usbpcapcmd.")
    if not a.from_pcap and not is_admin():
        die("Uruchom jako Administrator (USBPcap wymaga uprawnień do nagrywania).")
    up = UsbPcap(exe)
    if a.verify:
        return run_verify(a, up, tshark, out)
    run_capture(a, up, tshark, out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nPrzerwano.")
        sys.exit(130)
