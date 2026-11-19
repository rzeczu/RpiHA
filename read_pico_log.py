#!/usr/bin/env python3
"""
read_pico_log.py - odczyt i dekodowanie logu USB zapisanego przez firmware RT-GH Pico we flashu.

1. Odłącz Pico od automatu.
2. Załóż zworkę GP9 <-> GND (fizyczne piny 12 i 13 Pico) i podłącz Pico do komputera.
   Pico zgłosi się jako port szeregowy (COMx, sterownik wbudowany w Windows), dioda mruga podwójnie.
3. py -3.13 read_pico_log.py          (wymaga: py -3.13 -m pip install pyserial)
   Skrypt sam znajdzie port, pobierze log, zdekoduje go i zapisze pico_log_<data>.txt obok.
   --clear: po odczycie kasuje log (nowy zacznie się przy następnym normalnym starcie).
   Inne źródła: --file obraz.uf2|.bin, --picotool.
4. Zdejmij zworkę - bez niej Pico znowu jest gitarą.

Log NIE jest kasowany przy odczycie (chyba że --clear). Kasuje się sam, gdy zostanie mniej niż
64 KB wolnego.
"""
import argparse
import datetime
import os
import string
import struct
import subprocess
import sys
from pathlib import Path

MAGIC = b"RTGHLOG1"
SECTOR, PAGE = 4096, 256
FLASH_BASE = 0x10000000

TYPES = {1: "BOOT", 2: "BUS_RESET", 3: "UNPLUGGED", 4: "SUSPEND", 5: "RESUME", 6: "SETUP", 7: "MOUNT",
         8: "UNMOUNT", 9: "LED_OUT", 10: "CTRL_OUT_DATA", 11: "HID_SET_REPORT", 12: "HID_GET_REPORT",
         13: "STATS", 14: "INPUTS", 15: "TEXT", 16: "G_IN", 17: "G_HID", 18: "G_CTRL", 19: "PSTATS"}
XFER_RES = {0: "OK", 1: "FAILED", 2: "STALLED", 3: "TIMEOUT", 4: "INVALID", 0xEE: "open failed"}


def load_input_fields() -> list[tuple[str, int, int, int, int]]:
    """(nazwa, bajt, bit, rozmiar, wartość) z profile.json - do dekodowania raportów HID gitary."""
    import json
    p = Path(__file__).resolve().parent / "profile.json"
    try:
        prof = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for name, inp in prof.get("inputs", {}).items():
        if inp.get("skipped") or len(inp.get("fields", [])) != 1:
            continue
        f = inp["fields"][0]
        out.append((name, f["byte"], f["bit"], f["size"], f["held"] & ((1 << f["size"]) - 1)))
    return out


INPUT_FIELDS = load_input_fields()


def decode_hid(r: bytes) -> str:
    names = []
    for name, byte, bit, size, val in INPUT_FIELDS:
        if byte >= len(r):
            continue
        v = (int.from_bytes(r[byte:byte + 2], "little") >> bit) & ((1 << size) - 1)
        if v == val:
            names.append(name)
    return ", ".join(names) or "(nic)"
INPUT_NAMES = ["GREEN", "RED", "YELLOW", "BLUE", "ORANGE", "STRUM UP", "STRUM DOWN"]

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass


# ------------------------------------------------------------------ źródła obrazu flasha

def parse_uf2(data: bytes) -> dict[int, bytes]:
    mem = {}
    for off in range(0, len(data) - 511, 512):
        b = data[off:off + 512]
        m0, m1, flags, addr, size, _bno, _nb, _fam = struct.unpack_from("<8I", b, 0)
        if m0 != 0x0A324655 or m1 != 0x9E5D5157 or struct.unpack_from("<I", b, 508)[0] != 0x0AB16F30:
            continue
        if flags & 1:  # "not main flash"
            continue
        mem[addr] = b[32:32 + size]
    return mem


def mem_to_image(mem: dict[int, bytes]) -> tuple[int, bytearray]:
    if not mem:
        return 0, bytearray()
    lo = min(mem)
    hi = max(a + len(d) for a, d in mem.items())
    img = bytearray(b"\xff" * (hi - lo))
    for a, d in mem.items():
        img[a - lo:a - lo + len(d)] = d
    return lo, img


def find_bootsel_drive() -> Path | None:
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:/")
        info = root / "INFO_UF2.TXT"
        try:
            if info.is_file() and "RP2" in info.read_text(errors="replace").upper():
                return root
        except OSError:
            continue
    return None


DUMP_VID, DUMP_PID = 0x2E8A, 0x000A


def open_dump_port(name: str | None):
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        sys.exit("Brak pyserial. Zainstaluj:  py -3.13 -m pip install pyserial")
    if not name:
        ports = [p for p in list_ports.comports() if p.vid == DUMP_VID and p.pid == DUMP_PID]
        if not ports:
            seen = ", ".join(f"{p.device} ({p.description})" for p in list_ports.comports()) or "żadnych"
            sys.exit("Nie widzę Pico w trybie zrzutu (port COM 2E8A:000A). Załóż zworkę GP9-GND "
                     "(piny 12-13), odłącz i podłącz Pico ponownie. Widoczne porty: " + seen)
        name = ports[0].device
    return serial.Serial(name, 115200, timeout=3), name


def read_exact(ser, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = ser.read(n - len(buf))
        if not chunk:
            sys.exit(f"Przerwany odczyt z portu: {len(buf)} z {n} B")
        buf += chunk
    return bytes(buf)


def load_from_port(a) -> tuple[int, bytes, str]:
    ser, name = open_dump_port(a.port)
    with ser:
        ser.dtr = True
        ser.reset_input_buffer()
        ser.write(b"I")
        print(f"{name}: {ser.readline().decode(errors='replace').strip()}")
        ser.write(b"D")
        hdr = read_exact(ser, 16)
        if hdr[:8] != b"RTGHDUMP":
            sys.exit(f"Nieoczekiwana odpowiedź Pico: {hdr!r}")
        addr, size = struct.unpack_from("<II", hdr, 8)
        print(f"Pobieram {size // 1024} KB ...")
        img = read_exact(ser, size)
        if a.clear:
            ser.write(b"C")
            print(f"Kasowanie logu: {ser.readline().decode(errors='replace').strip()}")
    return addr, img, f"{name} (tryb zrzutu)"


def load_image(a) -> tuple[int, bytes, str]:
    if a.file:
        p = Path(a.file)
        data = p.read_bytes()
        if data[:4] == b"UF2\n":
            base, img = mem_to_image(parse_uf2(data))
            return base, bytes(img), str(p)
        return a.base, data, str(p)
    if a.picotool:
        exe = a.picotool if a.picotool is not True else find_picotool()
        out = Path(os.environ.get("TEMP", ".")) / "rtgh_flash.bin"
        for lo, hi in ((0x10180000, 0x10200000), (0x10380000, 0x10400000)):  # Pico 2 MB, Pico 2 4 MB
            r = subprocess.run([exe, "save", "-r", hex(lo), hex(hi), str(out)], capture_output=True, text=True)
            if r.returncode == 0 and MAGIC in out.read_bytes()[:SECTOR]:
                return lo, out.read_bytes(), f"picotool {hex(lo)}..{hex(hi)}"
        sys.exit("picotool nie odczytał logu:\n" + (r.stdout + r.stderr).strip())
    if find_bootsel_drive() is not None:
        sys.exit("Pico jest w trybie BOOTSEL - ten dysk nie pokazuje zawartości flasha. Odłącz je, załóż "
                 "zworkę GP9-GND (piny 12-13) i podłącz BEZ przycisku BOOTSEL.")
    return load_from_port(a)


def find_picotool() -> str:
    for c in (r"C:\pico\tools\picotool\picotool\picotool.exe", "picotool"):
        if Path(c).is_file() or c == "picotool":
            return c
    return "picotool"


# ------------------------------------------------------------------ dekodowanie

STD_REQ = {0: "GET_STATUS", 1: "CLEAR_FEATURE", 3: "SET_FEATURE", 5: "SET_ADDRESS", 6: "GET_DESCRIPTOR",
           7: "SET_DESCRIPTOR", 8: "GET_CONFIGURATION", 9: "SET_CONFIGURATION", 10: "GET_INTERFACE",
           11: "SET_INTERFACE", 12: "SYNCH_FRAME"}
HID_REQ = {1: "GET_REPORT", 2: "GET_IDLE", 3: "GET_PROTOCOL", 9: "SET_REPORT", 10: "SET_IDLE", 11: "SET_PROTOCOL"}
DESC = {1: "DEVICE", 2: "CONFIGURATION", 3: "STRING", 4: "INTERFACE", 5: "ENDPOINT", 6: "DEVICE_QUALIFIER",
        7: "OTHER_SPEED", 0x0F: "BOS", 0x21: "HID", 0x22: "HID_REPORT", 0x23: "HID_PHYSICAL"}
RCPT = {0: "dev", 1: "iface", 2: "ep", 3: "other"}
KNOWN_STRINGS = set(range(0, 7))


def decode_setup(s: bytes) -> tuple[str, bool]:
    """Opis żądania i czy jest 'podejrzane' (nieobsługiwane przez Pico / nietypowe)."""
    bm, br, wv, wi, wl = struct.unpack("<BBHHH", s)
    d = "IN " if bm & 0x80 else "OUT"
    typ = (bm >> 5) & 3
    rc = RCPT.get(bm & 0x1F, f"r{bm & 0x1F}")
    raw = f"[{s.hex(' ')}]"
    if typ == 0:
        name = STD_REQ.get(br, f"STD 0x{br:02X}")
        if br == 6:
            dt, di = wv >> 8, wv & 0xFF
            extra = f" lang 0x{wi:04X}" if dt == 3 else (f" iface {wi}" if rc == "iface" else "")
            odd = dt == 3 and di not in KNOWN_STRINGS or dt not in (1, 2, 3, 0x21, 0x22, 6)
            return f"{d} {name} {DESC.get(dt, f'0x{dt:02X}')} #{di}{extra} len {wl} {raw}", odd
        return f"{d} {name} ({rc}) wValue 0x{wv:04X} wIndex 0x{wi:04X} len {wl} {raw}", br not in (5, 9, 0, 1)
    if typ == 1:
        name = HID_REQ.get(br, f"CLASS 0x{br:02X}") if rc == "iface" and wi == 0 else f"CLASS 0x{br:02X}"
        return (f"{d} {name} ({rc} {wi}) wValue 0x{wv:04X} len {wl} {raw}",
                not (rc == "iface" and wi == 0 and br in (1, 2, 3, 9, 10, 11)))
    return f"{d} VENDOR req 0x{br:02X} ({rc} {wi}) wValue 0x{wv:04X} wIndex 0x{wi:04X} len {wl} {raw}", True


def iter_records(img: bytes, start: int):
    off = start + SECTOR
    while off + PAGE <= len(img):
        pg = img[off:off + PAGE]
        if pg[0] == 0xFF:
            break
        p = 0
        while p + 6 <= PAGE and pg[p] != 0xFF:
            typ, ln = pg[p], pg[p + 1]
            t = struct.unpack_from("<I", pg, p + 2)[0]
            yield typ, t, pg[p + 6:p + 6 + ln]
            p += 6 + ln
        off += PAGE


def decode(img: bytes, base: int) -> list[str]:
    start = None
    for off in range(0, len(img) - 7, SECTOR):
        if img[off:off + 8] == MAGIC:
            start = off
    if start is None:
        sys.exit("W obrazie nie ma logu (brak nagłówka RTGHLOG1). Czy na Pico jest firmware z logiem "
                 "i czy był choć raz uruchomiony? Spróbuj też --picotool.")
    region = struct.unpack_from("<I", img, start + 8)[0]
    out = [f"Log w 0x{base + start:08X}, obszar {region // 1024} KB"]
    session, prev_stats, prev_pstats, last_t = 0, None, None, None
    pages = 0
    for typ, t, data in iter_records(img, start):
        name = TYPES.get(typ, f"TYP{typ}")
        if typ == 15 and data.startswith(b"boot"):
            session += 1
            prev_stats = prev_pstats = None
            out.append("")
            out.append(f"===== SESJA {session}: {data.decode(errors='replace')} =====")
        ts = f"{t / 1000:9.3f}s"
        if typ == 6:
            desc, odd = decode_setup(data)
            out.append(f"{ts} {'!' if odd else ' '} SETUP {desc}")
        elif typ == 13 and len(data) >= 44:
            v = struct.unpack("<11I", data[:44])
            st = {"hid": v[0], "led": v[1], "nak": v[2:10], "drop": v[10]}
            if prev_stats:
                dh, dl = st["hid"] - prev_stats["hid"], st["led"] - prev_stats["led"]
                dn = [a - b for a, b in zip(st["nak"], prev_stats["nak"])]
            else:
                dh, dl, dn = st["hid"], st["led"], list(st["nak"])
            naks = ", ".join(f"EP{n} IN: {c}" for n, c in enumerate(dn) if c)
            out.append(f"{ts}   STATS +{dh} raportów HID, +{dl} pakietów LED OUT"
                       + (f", host odpytywał (NAK) {naks}" if naks else "")
                       + (f", ZGUBIONE rekordy: {st['drop']}" if st["drop"] else ""))
            prev_stats = st
        elif typ == 14:
            m = data[0] if data else 0
            names = [INPUT_NAMES[i] if i < len(INPUT_NAMES) else f"#{i}" for i in range(8) if m >> i & 1]
            out.append(f"{ts}   INPUTS {', '.join(names) or '(nic)'}")
        elif typ in (9, 10):
            out.append(f"{ts} ! {name} ({len(data)} B): {data.hex(' ')}")
        elif typ == 11:
            out.append(f"{ts} ! HID SET_REPORT id {data[0]} typ {data[1]} ({len(data) - 2} B): {data[2:].hex(' ')}")
        elif typ == 12:
            out.append(f"{ts}   HID GET_REPORT id {data[0]} typ {data[1]}")
        elif typ == 15:
            if not data.startswith(b"boot"):
                out.append(f"{ts}   {data.decode(errors='replace')}")
        elif typ == 16 and data:
            out.append(f"{ts} ! GITARA -> AUTOMAT EP 0x{data[0]:02X} ({len(data) - 1} B): {data[1:].hex(' ')}")
        elif typ == 17:
            out.append(f"{ts}   GITARA HID {data.hex(' ')}  [{decode_hid(data)}]")
        elif typ == 18 and len(data) >= 9:
            desc, _odd = decode_setup(data[:8])
            res = XFER_RES.get(data[8], str(data[8]))
            out.append(f"{ts} ! CTRL do gitary: {desc} -> {res}" + (f", dane: {data[9:].hex(' ')}" if len(data) > 9 else ""))
        elif typ == 19 and len(data) >= 44:
            v = struct.unpack("<11I", data[:44])
            ps = dict(zip(("out_fwd", "out_drop", "in82", "in84", "sent82", "sent84", "drop82", "drop84",
                           "hid", "ctrl", "err"), v))
            if prev_pstats:
                d = {k: ps[k] - prev_pstats[k] for k in ps}
            else:
                d = ps
            parts = [f"automat->gitara +{d['out_fwd']}"]
            if d["out_drop"]:
                parts.append(f"zgubione do gitary +{d['out_drop']}")
            parts.append(f"gitara->automat 0x82 +{d['in82']} (odebrane {d['sent82']}), "
                         f"0x84 +{d['in84']} (odebrane {d['sent84']})")
            if d["drop82"] or d["drop84"]:
                parts.append(f"nieodebrane przez automat 0x82 +{d['drop82']} 0x84 +{d['drop84']}")
            parts.append(f"HID gitary +{d['hid']}")
            if d["ctrl"]:
                parts.append(f"CTRL do gitary +{d['ctrl']}")
            if d["err"]:
                parts.append(f"BŁĘDY +{d['err']}")
            out.append(f"{ts}   PROXY " + ", ".join(parts))
            prev_pstats = ps
        else:
            out.append(f"{ts}   {name}")
        last_t = t
    out.append("")
    out.append("Legenda: '!' = rzecz, której Pico nie obsługuje tak jak oryginał (albo nie wiemy jak) - "
               "kandydat na przyczynę.")
    out.append("NAK na EP2/EP4 IN = gra czyta z interfejsu LED i czeka na odpowiedź, której Pico nie wysyła.")
    return out


def main():
    ap = argparse.ArgumentParser(description="Odczyt logu USB z Pico (RT-GH).")
    ap.add_argument("--port", help="port COM Pico w trybie zrzutu (domyślnie wykrywany po 2E8A:000A)")
    ap.add_argument("--clear", action="store_true", help="po odczycie skasuj log na Pico")
    ap.add_argument("--file", help="obraz flasha: .uf2 albo .bin (np. z picotool save)")
    ap.add_argument("--base", type=lambda s: int(s, 0), default=0x10180000, help="adres początku pliku .bin")
    ap.add_argument("--picotool", nargs="?", const=True, help="czytaj przez picotool (opcjonalnie ścieżka)")
    ap.add_argument("--out", help="plik wynikowy (domyślnie pico_log_<data>.txt obok skryptu)")
    a = ap.parse_args()
    base, img, src = load_image(a)
    print(f"Źródło: {src} ({len(img) // 1024} KB od 0x{base:08X})")
    lines = decode(img, base)
    text = "\n".join(lines)
    print(text)
    out = Path(a.out) if a.out else Path(__file__).resolve().parent / \
        f"pico_log_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt"
    out.write_text(text + "\n", encoding="utf-8")
    print(f"\nZapisano {out}")


if __name__ == "__main__":
    main()
