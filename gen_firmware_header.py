#!/usr/bin/env python3
"""
gen_firmware_header.py - z profile.json robi firmware/src/rtgh_profile.h dla firmware'u Pico.

Wszystkie deskryptory idą bajt w bajt z nagrania oryginału. Mapa wejść z kalibracji.
Uruchom po każdym nowym profile.json:
    py -3.13 gen_firmware_header.py [profile.json] [firmware/src/rtgh_profile.h]
"""
import json
import re
import sys
from pathlib import Path

# typy deskryptorów, które TinyUSB umie przejść przy SET_CONFIGURATION
TINYUSB_OK_TYPES = {0x02, 0x04, 0x05, 0x0B, 0x21, 0x22, 0x24, 0x25}


def c_array(name: str, data: bytes, comment: str = "") -> str:
    lines = [f"// {comment}" if comment else ""]
    lines.append(f"static const uint8_t {name}[{len(data)}] = {{")
    for i in range(0, len(data), 12):
        lines.append("    " + ", ".join(f"0x{b:02X}" for b in data[i:i + 12]) + ",")
    lines.append("};")
    return "\n".join(l for l in lines if l != "") + "\n"


def sanitize_config(cfg: bytes) -> tuple[bytes, list[str]]:
    """Wersja configuration descriptora bez deskryptorów, na których TinyUSB robi assert (np. OTG 0x09)."""
    out, removed, i = bytearray(cfg[:9]), [], 9
    while i + 2 <= len(cfg):
        ln = cfg[i]
        if ln < 2:
            break
        d = cfg[i:i + ln]
        if d[1] in TINYUSB_OK_TYPES:
            out += d
        else:
            removed.append(d.hex(" "))
        i += ln
    out[2:4] = len(out).to_bytes(2, "little")
    return bytes(out), removed


def c_float(v: float) -> str:
    s = f"{v:.7g}"
    if not any(ch in s for ch in ".eEn"):  # "0" -> "0.0" (w C "0f" jest błędem)
        s += ".0"
    return s + "f"


def pin_macro(name: str) -> str:
    return "PIN_" + re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def main():
    root = Path(__file__).resolve().parent
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "profile.json"
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else root / "firmware" / "src" / "rtgh_profile.h"
    p = json.loads(src.read_text(encoding="utf-8"))
    d = p["usb"]["descriptors"]

    dev = bytes.fromhex(d["device"])
    cfg = bytes.fromhex(d["configuration"]["0"])
    hid_ifaces = sorted(d["hid_report"], key=int)
    rd = bytes.fromhex(d["hid_report"][hid_ifaces[0]])
    cfg_tusb, removed = sanitize_config(cfg)
    if p["report_map"]["uses_report_ids"]:
        sys.exit("Ten generator obsługuje raporty bez report ID (jak oryginał RT-GH).")
    neutral = bytes.fromhex(p["neutral_report"]["hex"])

    strings = {}
    for s in d["strings"]:
        if s["index"] in strings and not s["source"].startswith("pcap"):
            continue
        strings[s["index"]] = bytes.fromhex(s["hex"])
    max_idx = max(strings) if strings else 0

    out = [
        "// WYGENEROWANE przez gen_firmware_header.py z profile.json - nie edytuj ręcznie.",
        f"// Profil: {p.get('created')}, VID:PID {dev[9]:02X}{dev[8]:02X}:{dev[11]:02X}{dev[10]:02X}",
        "#pragma once",
        "#include <stddef.h>",
        "#include <stdint.h>",
        '#include "pins.h"',
        "",
        c_array("rtgh_desc_device", dev, "device descriptor (dokładnie jak oryginał)"),
        c_array("rtgh_desc_config", cfg, "configuration descriptor (dokładnie jak oryginał) - wysyłany na GET_DESCRIPTOR"),
        c_array("rtgh_desc_config_tusb", cfg_tusb,
                "ten sam bez " + (", ".join(f"[{r}]" for r in removed) or "niczego")
                + " - tylko dla parsera TinyUSB przy SET_CONFIGURATION"),
        c_array("rtgh_desc_hid_report", rd, f"HID report descriptor interfejsu {hid_ifaces[0]}"),
    ]
    for i in range(max_idx + 1):
        if i in strings:
            txt = "LANGID" if i == 0 else bytes(strings[i][2:]).decode("utf-16-le", "replace")
            out.append(c_array(f"rtgh_str_{i}", strings[i], f"string {i}: {txt!r}"))
    out.append(f"#define RTGH_STRING_COUNT {max_idx + 1}")
    out.append("static const uint8_t *const rtgh_strings[RTGH_STRING_COUNT] = {")
    out += [f"    {'rtgh_str_' + str(i) if i in strings else 'NULL'}," for i in range(max_idx + 1)]
    out.append("};\n")

    out.append(f"#define RTGH_REPORT_LEN {len(neutral)}")
    out.append(c_array("rtgh_neutral_report", neutral, "raport w spoczynku (mediana z baseline)"))

    ep_in = next((e for it in p["usb"]["interfaces"] if it["class"] == 3 for e in it["endpoints"]
                  if e["address"] & 0x80), None)
    if ep_in:
        out.append(f"#define RTGH_HID_EP_IN 0x{ep_in['address']:02X}  // bInterval {ep_in['bInterval']} ms")
    out.append(f"#define RTGH_EP0_SIZE {dev[7]}\n")

    out.append("typedef struct {\n    const char *name;\n    uint8_t pin;\n    uint8_t byte;   // bajt w raporcie\n"
               "    uint8_t bit;    // pierwszy bit pola\n    uint8_t size;   // rozmiar pola w bitach\n"
               "    uint8_t value;  // wartość pola, gdy wciśnięte\n} rtgh_input_t;\n")
    out.append("static const rtgh_input_t rtgh_inputs[] = {")
    n = 0
    for name, inp in p["inputs"].items():
        if inp.get("skipped"):
            out.append(f"    // {name}: pominięte w kalibracji")
            continue
        if len(inp["fields"]) != 1:
            out.append(f"    // {name}: niejednoznaczne ({len(inp['fields'])} pól) - pominięte")
            continue
        f = inp["fields"][0]
        val = f["held"] & ((1 << f["size"]) - 1)
        out.append(f'    {{ "{name}", {pin_macro(name)}, {f["byte"]}, {f["bit"]}, {f["size"]}, {val} }},'
                   f"  // {f['usage']}, spoczynek {f['neutral']}")
        n += 1
    out.append("};")
    out.append(f"#define RTGH_INPUT_COUNT {n}\n")

    # Akcelerometr (calib_accel.py): bajt = c0 + c1*adc0 + c2*adc1 + c3*adc2 (ADC 0..4095), obcięte do -127..127
    acc = p.get("accel") or {}
    fits = acc.get("fits", [])
    out.append("// akcelerometr MMA7260 na GP26/27/28 (ADC0..2) - przeliczenie z kalibracji calib_accel.py")
    out.append(f"#define RTGH_ACCEL_COUNT {len(fits)}")
    if fits:
        out.append("static const uint8_t rtgh_accel_byte[RTGH_ACCEL_COUNT] = {"
                   + ", ".join(str(f["byte"]) for f in fits) + "};")
        out.append("static const float rtgh_accel_coef[RTGH_ACCEL_COUNT][4] = {")
        for f in fits:
            out.append("    {" + ", ".join(c_float(c) for c in f["coef"]) + "},"
                       + f"  // bajt {f['byte']} ({f.get('usage', '?')}), błąd RMS {f.get('rms', 0):.1f}")
        out.append("};")
    out.append("")

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(out), encoding="utf-8")
    print(f"Zapisano {dst} ({n} wejść, {max_idx + 1} stringów"
          + (f", dla TinyUSB usunięto: {removed}" if removed else "") + ")")
    missing = [pin_macro(nm) for nm, i in p["inputs"].items() if not i.get("skipped")]
    print("Upewnij się, że firmware/src/pins.h definiuje: " + ", ".join(missing))


if __name__ == "__main__":
    main()
