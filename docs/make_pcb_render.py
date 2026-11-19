#!/usr/bin/env python3
"""
make_pcb_render.py - draws docs/pcb_render.svg: a simplified, to-scale map of the Guitar Hero
Arcade fret board (Raw Thrills PCB1259C, ASSY #500-00022-04, revision 4) with the 14 solder
points used by RpiHA.

Every coordinate below is in millimetres, measured on 300 dpi scans of the component side.
Origin: top-left corner of the board at the P1 connector end. x runs across the board (0..25),
y runs along it toward the fret switches. Parts are drawn as simple shapes; their positions and
sizes follow the scans.

    py -3 docs/make_pcb_render.py
"""
from pathlib import Path

OUT = Path(__file__).resolve().parent / "pcb_render.svg"

W_PX, H_PX = 1500, 1280
BOARD_W, BOARD_L = 25.0, 350.0
FRET_PITCH = 23.98

# ------------------------------------------------------------------------------------- colours
PAGE = "#f4f2ed"
PANEL = "#ffffff"
PANEL_EDGE = "#d5d1c8"
INK = "#1f2328"
MUTED = "#5b636c"
BOARD = "#c3272e"
BOARD_EDGE = "#7a1418"
SILK = "#f6f3ec"
PAD = "#d9d8d2"
PAD_EDGE = "#8e8d87"
TARGET = "#ffd21f"
MARK_TEXT = "#1c3fd1"
MARK_EDGE = "#0f2a8a"

FRET_COLORS = {"ORANGE": "#ee8a00", "BLUE": "#1f6fe0", "YELLOW": "#e8b800", "RED": "#e03131", "GREEN": "#2f9e44"}
GROUP_COLORS = {"power": "#1f2328", "usb": "#8250df", "strum": "#0b8a8f", "tilt": "#0969da"}


def f(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


# ------------------------------------------------------------------------------- board drawing
board: list[str] = []      # SVG elements in board millimetres
targets: dict[str, tuple[float, float, float, float]] = {}   # name -> pad rect (cx, cy, w, h)


def rect(cx, cy, w, h, cls="", extra=""):
    cls_attr = f' class="{cls}"' if cls else ""
    return f'<rect x="{f(cx - w / 2)}" y="{f(cy - h / 2)}" width="{f(w)}" height="{f(h)}"{cls_attr} {extra}/>'


def silk_text(x, y, s, size=0.95, anchor="middle", rot=0, length=None):
    t = f' transform="rotate({rot} {f(x)} {f(y)})"' if rot else ""
    ln = f' textLength="{f(length)}" lengthAdjust="spacingAndGlyphs"' if length else ""
    board.append(f'<text x="{f(x)}" y="{f(y)}" font-size="{f(size)}" text-anchor="{anchor}" class="st"{t}{ln}>{s}</text>')


def chip(cx, cy, orient, size, kind, name=None):
    """Two-terminal SMD part. orient 'v' = pads top/bottom, 'h' = pads left/right."""
    L, Wd, pl, pw, silk = {
        "0402": (1.0, 0.5, 0.5, 0.6, (1.9, 1.3)),
        "0603": (1.6, 0.8, 0.75, 0.9, (3.2, 1.55)),
        "0805": (2.0, 1.25, 0.85, 1.3, (3.3, 1.8)),
    }[size]
    body = "res" if kind == "res" else "cap"
    if orient == "v":
        board.append(rect(cx, cy, silk[1], silk[0], "silk"))
        board.append(rect(cx, cy, Wd, L, body))
        pads = [(cx, cy - L / 2 + pl / 2 - 0.15, pw, pl), (cx, cy + L / 2 - pl / 2 + 0.15, pw, pl)]
    else:
        board.append(rect(cx, cy, silk[0], silk[1], "silk"))
        board.append(rect(cx, cy, L, Wd, body))
        pads = [(cx - L / 2 + pl / 2 - 0.15, cy, pl, pw), (cx + L / 2 - pl / 2 + 0.15, cy, pl, pw)]
    for p in pads:
        board.append(rect(*p, "pad"))
    if name:
        targets[name + ".1"], targets[name + ".2"] = pads   # .1 = top / left, .2 = bottom / right
    return pads


def sot23(cx, cy, single="up", name=None):
    if single == "up":
        board.append(rect(cx, cy, 2.9, 1.3, "ic"))
        pins = [(cx - 0.95, cy + 1.05), (cx + 0.95, cy + 1.05), (cx, cy - 1.05)]
        pw, ph = 0.6, 0.75
    else:  # single pin to the right
        board.append(rect(cx, cy, 1.3, 2.9, "ic"))
        pins = [(cx - 1.05, cy - 0.95), (cx - 1.05, cy + 0.95), (cx + 1.05, cy)]
        pw, ph = 0.75, 0.6
    for px, py in pins:
        board.append(rect(px, py, pw, ph, "pad"))
    if name:
        targets[name + ".3"] = (pins[2][0], pins[2][1], pw, ph)


def tantalum(cx, cy, bw, bh, plus, silk):
    board.append(rect(cx, cy, silk[0], silk[1], "silk"))
    board.append(rect(cx, cy - bh / 2 + 0.45, bw * 0.85, 0.9, "pad"))
    board.append(rect(cx, cy + bh / 2 - 0.45, bw * 0.85, 0.9, "pad"))
    board.append(rect(cx, cy, bw, bh - 1.2, "tant"))
    band_y = cy - (bh - 1.2) / 2 + 0.3 if plus == "top" else cy + (bh - 1.2) / 2 - 0.3
    board.append(rect(cx, band_y, bw, 0.6, "tband"))


def led(cx, cy):
    board.append(rect(cx, cy, 4.4, 2.2, "silk"))
    board.append(rect(cx - 1.35, cy, 1.0, 1.4, "pad"))
    board.append(rect(cx + 1.35, cy, 1.0, 1.4, "pad"))
    board.append(rect(cx, cy, 1.8, 1.2, "", 'fill="#eee7cf"'))
    board.append(f'<circle cx="{f(cx)}" cy="{f(cy)}" r="0.32" fill="#f3d36b"/>')


def qfp44(cx, cy):
    for i in range(11):
        o = -4.0 + 0.8 * i
        board.append(rect(cx + o, cy - 5.6, 0.36, 1.2, "lead"))
        board.append(rect(cx + o, cy + 5.6, 0.36, 1.2, "lead"))
        board.append(rect(cx - 5.6, cy + o, 1.2, 0.36, "lead"))
        board.append(rect(cx + 5.6, cy + o, 1.2, 0.36, "lead"))
    board.append(rect(cx, cy, 10.0, 10.0, "ic", 'rx="0.3"'))


def hole(cx, cy, d, ring):
    board.append(f'<circle cx="{f(cx)}" cy="{f(cy)}" r="{f(ring / 2)}" fill="#a81b21"/>')
    board.append(f'<circle cx="{f(cx)}" cy="{f(cy)}" r="{f(d / 2)}" fill="#ffffff" stroke="#c9c6bf" stroke-width="0.12"/>')


def th_pad(cx, cy, square=False):
    if square:
        board.append(rect(cx, cy, 1.4, 1.4, "pad"))
    else:
        board.append(f'<circle cx="{f(cx)}" cy="{f(cy)}" r="0.72" class="pad"/>')
    board.append(f'<circle cx="{f(cx)}" cy="{f(cy)}" r="0.36" fill="#3b3b3b"/>')


def lug(cx, cy, s=2.0):
    board.append(rect(cx, cy, s, s, "", 'fill="#b8773d" stroke="#8a5427" stroke-width="0.1"'))
    board.append(f'<circle cx="{f(cx)}" cy="{f(cy)}" r="0.34" fill="#3b3b3b"/>')


def membrane(cx, cy, w=11.9, h=7.8):
    r = h / 2
    board.append(rect(cx, cy, w, h, "", f'rx="{f(r)}" fill="#151515"'))
    x, i = cx - w / 2 + 1.0, 0
    while x < cx + w / 2 - 0.9:
        dx = abs(x - cx)
        flat = w / 2 - r
        half = r if dx <= flat else max(0.0, (r * r - (dx - flat) ** 2) ** 0.5)
        half -= 0.7
        if half > 0.4:
            y0, y1 = cy - half + (0.6 if i % 2 else 0), cy + half - (0 if i % 2 else 0.6)
            board.append(f'<line x1="{f(x)}" y1="{f(y0)}" x2="{f(x)}" y2="{f(y1)}" stroke="#6a6a6a" stroke-width="0.2"/>')
        x += 0.62
        i += 1


def draw_board():
    # outline: straight top, torn edge at the bottom where the scan ends
    pts = [(0, 0), (BOARD_W, 0), (BOARD_W, BOARD_L)]
    n = 20
    for i in range(1, n):
        pts.append((BOARD_W - BOARD_W * i / n, BOARD_L + (0.9 if i % 2 else 0.0)))
    pts.append((0, BOARD_L))
    board.append('<path d="M' + " L".join(f"{f(x)} {f(y)}" for x, y in pts) +
                 f' Z" fill="{BOARD}" stroke="{BOARD_EDGE}" stroke-width="0.25"/>')

    # --- P1 connector (through-hole, connector body sits on the back) and top hole
    board.append(f'<rect x="1.0" y="0.4" width="12.4" height="14.3" class="silk"/>')
    for i in range(5):
        for row, y in enumerate((10.2, 12.2)):
            x = 3.2 + 2.0 * i
            th_pad(x, y, square=(row == 1 and i == 4))
    for x in (1.7, 12.8):
        board.append(f'<circle cx="{x}" cy="8.7" r="0.5" fill="#3b3b3b"/>')
    silk_text(14.9, 2.6, "P1", 1.1, "start", 90)
    hole(19.9, 7.3, 3.6, 6.8)
    lug(21.2, 13.5, 1.8)

    # --- diodes / protection parts near the connector
    for x, name in ((7.8, "D64"), (11.9, "D62"), (16.7, "D68")):
        sot23(x, 17.4, "up")
    silk_text(7.8, 21.3, "D64")
    silk_text(12.2, 20.0, "D62")
    silk_text(16.7, 15.2, "D68")
    chip(3.25, 16.65, "h", "0603", "cap")
    sot23(3.3, 19.75, "up")
    silk_text(0.95, 19.75, "D66", 0.9, rot=-90)
    sot23(3.4, 25.6, "up")
    silk_text(0.95, 25.6, "D67", 0.9, rot=-90)
    sot23(11.95, 23.6, "up", name="D63")
    sot23(16.55, 23.6, "up", name="D65")
    silk_text(9.4, 24.0, "D63")
    silk_text(18.9, 21.5, "D65")

    # --- LEDs on the right edge
    for y in (23.95, 27.05, 30.15):
        led(21.75, y)
    silk_text(21.75, 22.4, "D1")

    # --- resistor row, big tantalum C27
    for x in (11.3, 13.05, 14.8, 16.55):
        chip(x, 28.3, "v", "0603", "res")
    tantalum(7.45, 30.65, 3.0, 4.2, "top", (3.6, 4.9))
    silk_text(7.45, 27.6, "C27")
    silk_text(5.25, 28.2, "+", 1.1)

    # --- left column
    for y in (31.9, 38.05):
        chip(3.25, y, "h", "0603", "cap")
    sot23(3.2, 35.0, "up")
    for y in (40.45, 42.0, 43.9, 46.9):
        chip(3.25, y, "h", "0603", "cap")

    # --- right column
    for y in (33.15, 34.95, 37.25, 39.6, 41.95, 44.4, 46.4, 48.85, 50.85):
        chip(21.65, y, "h", "0603", "res")
    silk_text(23.75, 53.6, "R1", 0.9, rot=-90)

    # --- USB / power area just above U1 (points 1, 2, 5, 6)
    chip(6.15, 34.9, "v", "0805", "cap")
    for x, name in ((8.85, "RU1"), (10.7, "RU2"), (12.55, "RU3")):
        chip(x, 34.9, "v", "0805", "res", name=name)
    chip(14.95, 35.05, "v", "0402", "cap", name="CPWR")
    tantalum(17.2, 34.6, 2.0, 3.6, "bottom", (2.6, 4.6))
    silk_text(19.25, 37.4, "+", 1.1)

    # --- U1 MC9S08JM60 (LQFP-44) - to be removed
    qfp44(12.75, 44.6)
    silk_text(5.1, 51.0, "U1")

    # --- crystal and parts below U1
    board.append(rect(6.35, 55.85, 7.9, 6.0, "silk"))
    for dx in (-3.55, 3.55):
        for dy in (-2.55, 2.55):
            board.append(rect(6.35 + dx, 55.85 + dy, 1.3, 1.1, "pad"))
    board.append(rect(6.35, 55.85, 6.7, 4.7, "", 'rx="0.4" fill="#8d9094" stroke="#5f6266" stroke-width="0.15"'))
    board.append(rect(6.35, 55.85, 5.6, 3.6, "", 'rx="0.3" fill="#a3a6aa"'))
    silk_text(2.6, 51.9, "Y1")
    chip(11.3, 53.3, "v", "0603", "res")
    chip(14.35, 53.3, "v", "0603", "cap")
    chip(16.6, 53.3, "v", "0603", "cap")
    silk_text(11.3, 56.1, "R11", 0.85)
    silk_text(14.35, 56.1, "C19", 0.85)
    silk_text(16.6, 56.1, "C20", 0.85)
    board.append('<circle cx="18.8" cy="52.5" r="0.45" class="pad"/><circle cx="18.8" cy="52.5" r="0.2" fill="#3b3b3b"/>')
    chip(8.3, 60.6, "h", "0603", "cap")
    silk_text(11.25, 61.0, "C11", 0.85)
    for x in (3.9, 6.5, 9.0):
        th_pad(x, 63.2)

    # --- U2: empty accelerometer footprint on rev 4
    for (x, y, dx, dy) in ((12.8, 59.7, 1, 1), (18.8, 59.7, -1, 1), (12.8, 65.9, 1, -1), (18.8, 65.9, -1, -1)):
        board.append(f'<polyline points="{f(x + dx * 1.2)},{f(y)} {f(x)},{f(y)} {f(x)},{f(y + dy * 1.2)}" class="silk"/>')
    for i in range(4):
        o = 14.1 + 1.07 * i
        for (px, py) in ((o, 60.15), (o, 65.55)):
            board.append(f'<circle cx="{f(px)}" cy="{f(py)}" r="0.24" class="pad"/>')
        o = 61.25 + 1.04 * i
        for (px, py) in ((13.1, o), (18.5, o)):
            board.append(f'<circle cx="{f(px)}" cy="{f(py)}" r="0.24" class="pad"/>')
    silk_text(19.6, 58.7, "U2", 0.9)
    chip(20.75, 61.4, "v", "0603", "cap")
    silk_text(22.75, 61.75, "C7", 0.9)

    # --- accelerometer output resistors (points 7, 8, 9) and U3
    for x, name in ((12.65, "R8"), (14.65, "R9"), (16.65, "R10")):
        chip(x, 68.8, "v", "0603", "res", name=name)
    silk_text(10.95, 69.1, "R8")
    silk_text(18.65, 69.1, "R10")
    lug(2.1, 69.4)
    lug(22.3, 69.4)
    board.append(rect(14.1, 73.35, 5.9, 4.6, "silk"))
    board.append(rect(14.1, 73.35, 5.4, 4.1, "ic", 'rx="0.2"'))
    silk_text(12.6, 76.75, "U3")
    chip(10.1, 73.5, "v", "0603", "cap")
    silk_text(8.4, 73.85, "C21")

    # --- sticker and silkscreen near it
    board.append('<rect x="4.85" y="77.95" width="6.3" height="22.7" rx="1" fill="#ffffff" stroke="#cfcac0" stroke-width="0.12"/>')
    silk_text(3.3, 79.2, "WWW.RIGHTHANDTECH.COM", 0.95, "start", 90, length=21.5)
    silk_text(1.45, 85.8, "PCB1259C", 0.95, "start", 90, length=6.4)

    hole(20.5, 121.4, 3.6, 6.8)

    # --- maker text block (identifies the revision: ASSY #500-00022-04)
    silk_text(17.2, 188.6, "RAW THRILLS, INC.", 1.9, "start", 90, length=19.0)
    silk_text(13.4, 190.5, "GUITAR HERO FRET", 1.45, "start", 90, length=14.0)
    silk_text(9.8, 188.7, "ASSY #500-00022-04", 1.45, "start", 90, length=17.0)
    silk_text(6.9, 192.0, "Copyright 2009", 1.45, "start", 90, length=11.0)

    # --- fret switches
    frets = [("ORANGE", "C14", "R29", "D59", "R20"), ("BLUE", "C15", "R28", "D61", "R21"),
             ("YELLOW", "C16", "R27", "D58", "R22"), ("RED", "C17", "R26", "D60", "R23"),
             ("GREEN", "C18", "R25", "D57", "R24")]
    for i, (color, c, r_top, d, r_side) in enumerate(frets):
        yh = 226.9 + FRET_PITCH * i          # hole above the switch
        ym = yh - 1.25                        # row with C1x / R2x (solder point)
        hole(12.8, yh, 3.3, 7.0)
        if color in ("RED", "GREEN"):
            hole(21.85, yh, 3.6, 5.0)
        chip(3.3, ym, "h", "0603", "cap", name=c)
        chip(6.9, ym, "h", "0603", "res")
        sot23(4.85, yh + 1.7, "right")
        chip(7.7, yh + 1.7, "v", "0603", "res")
        silk_text(3.3, ym - 1.15, c, 0.9)
        silk_text(6.9, ym - 1.15, r_top, 0.9)
        silk_text(1.75, yh + 1.7, d, 0.9, rot=-90)
        silk_text(7.7, yh + 4.25, r_side, 0.9)
        # switch: four corner holes, outline, membrane contact
        y0, y1 = 233.6 + FRET_PITCH * i, 244.4 + FRET_PITCH * i
        board.append(f'<path d="M5.2 {f(y0)} H20 M5.2 {f(y1)} H20 M4.2 {f(y0 + 1)} V{f(y1 - 1)} '
                     f'M21 {f(y0 + 1)} V{f(y1 - 1)}" class="silk"/>')
        for x in (4.2, 21.0):
            for y in (y0, y1):
                hole(x, y, 1.6, 2.3)
        membrane(12.55, (y0 + y1) / 2)
        label = "SW_" + color
        silk_text(21.8, y0 - 0.6, label, 2.0, "start", 90, length=1.37 * len(label))
    hole(12.8, 226.9 + FRET_PITCH * 5, 3.3, 7.0)


# ------------------------------------------------------------------------------------- panels
class View:
    """Board millimetres -> canvas pixels for one panel."""

    def __init__(self, x, y, k, mm_x0, mm_y0, mm_w, mm_h, cid):
        self.x, self.y, self.k = x, y, k
        self.mx, self.my = mm_x0, mm_y0
        self.w, self.h = mm_w * k, mm_h * k
        self.cid = cid

    def px(self, mx, my):
        return self.x + (mx - self.mx) * self.k, self.y + (my - self.my) * self.k

    def clip(self):
        return f'<clipPath id="{self.cid}"><rect x="{f(self.x)}" y="{f(self.y)}" width="{f(self.w)}" height="{f(self.h)}" rx="4"/></clipPath>'

    def open(self):
        tx, ty = self.x - self.mx * self.k, self.y - self.my * self.k
        return (f'<g clip-path="url(#{self.cid})"><rect x="{f(self.x)}" y="{f(self.y)}" width="{f(self.w)}" '
                f'height="{f(self.h)}" fill="#e9e6df"/>'
                f'<use href="#board" transform="translate({f(tx)} {f(ty)}) scale({f(self.k)})"/>')

    def target(self, name):
        cx, cy, w, h = targets[name]
        x, y = self.px(cx - w / 2, cy - h / 2)
        pad = 0.08 * self.k
        return (f'<rect x="{f(x - pad)}" y="{f(y - pad)}" width="{f(w * self.k + 2 * pad)}" height="{f(h * self.k + 2 * pad)}" '
                f'fill="{TARGET}" stroke="#1b1b1b" stroke-width="{f(max(0.8, 0.05 * self.k))}"/>')


def disc(x, y, n, r, font=None):
    font = font or r * 1.25
    return (f'<circle cx="{f(x)}" cy="{f(y)}" r="{f(r)}" fill="#ffffff" stroke="{MARK_EDGE}" stroke-width="{f(max(1.0, r / 9))}"/>'
            f'<text x="{f(x)}" y="{f(y + font * 0.36)}" font-size="{f(font)}" font-weight="700" fill="{MARK_TEXT}" '
            f'text-anchor="middle">{n}</text>')


def text(x, y, s, size=13, weight=400, fill=INK, anchor="start", extra=""):
    return (f'<text x="{f(x)}" y="{f(y)}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
            f'text-anchor="{anchor}" {extra}>{s}</text>')


def panel(x, y, w, h, title=None):
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{PANEL}" stroke="{PANEL_EDGE}"/>']
    if title:
        out.append(text(x + 14, y + 24, title, 15, 700))
    return "".join(out)


# marker positions (as placed on the hand-marked photos) and the pad each one points at
MARKS = {
    1: ((14.9, 33.5), "CPWR.1"), 2: ((14.9, 36.7), "CPWR.2"),
    3: ((11.85, 21.6), "D63.3"), 4: ((16.45, 21.6), "D65.3"),
    5: ((12.55, 36.6), "RU3.2"), 6: ((8.75, 36.6), "RU1.2"),
    7: ((12.6, 67.0), "R8.1"), 8: ((14.65, 67.0), "R9.1"), 9: ((16.65, 67.0), "R10.1"),
}
for i, c in enumerate(("C14", "C15", "C16", "C17", "C18")):
    MARKS[10 + i] = ((4.2, 225.65 + FRET_PITCH * i), c + ".2")

ROWS = [  # number, signal, group colour, where on the board, Pico
    (1, "GND (USB)", GROUP_COLORS["power"], "small cap left of tantalum, upper pad", "GND, pin 38"),
    (2, "+5 V (USB)", "#d1242f", "same cap, lower pad", "VBUS, pin 40"),
    (3, "STRUM UP", GROUP_COLORS["strum"], "D63, single pin", "GP7, pin 10"),
    (4, "STRUM DOWN", GROUP_COLORS["strum"], "D65, single pin", "GP8, pin 11"),
    (5, "USB D+", GROUP_COLORS["usb"], "3rd resistor above U1, lower pad", "TP3 (underside)"),
    (6, "USB D−", GROUP_COLORS["usb"], "1st resistor above U1, lower pad", "TP2 (underside)"),
    (7, "ZOUT", GROUP_COLORS["tilt"], "R8 upper pad (rev 3: lower)", "GP26 / ADC0, pin 31"),
    (8, "YOUT", GROUP_COLORS["tilt"], "R9 upper pad (rev 3: lower)", "GP27 / ADC1, pin 32"),
    (9, "XOUT", GROUP_COLORS["tilt"], "R10 upper pad (rev 3: lower)", "GP28 / ADC2, pin 34"),
    (10, "ORANGE", FRET_COLORS["ORANGE"], "C14, pad next to R29", "GP6, pin 9"),
    (11, "BLUE", FRET_COLORS["BLUE"], "C15, pad next to R28", "GP5, pin 7"),
    (12, "YELLOW", FRET_COLORS["YELLOW"], "C16, pad next to R27", "GP4, pin 6"),
    (13, "RED", FRET_COLORS["RED"], "C17, pad next to R26", "GP3, pin 5"),
    (14, "GREEN", FRET_COLORS["GREEN"], "C18, pad next to R25", "GP2, pin 4"),
]


def build() -> str:
    draw_board()
    s: list[str] = []
    defs: list[str] = []

    s.append(f'<rect width="{W_PX}" height="{H_PX}" fill="{PAGE}"/>')
    s.append(text(30, 50, "RpiHA: where to solder on the Guitar Hero Arcade fret board", 26, 700))
    s.append(text(30, 78, "Raw Thrills PCB1259C, ASSY #500-00022-04 (revision 4), component side. "
                          "Parts are simplified; positions and sizes are to scale, measured from 300 dpi scans.", 14, 400, MUTED))

    # ---------------------------------------------------------------- overview
    s.append(panel(20, 100, 290, 1165, "Whole board"))
    ov = View(105, 150, 3.0, 0, 0, BOARD_W, BOARD_L + 1.5, "cov")
    defs.append(ov.clip())
    s.append(ov.open() + "</g>")
    for mm in range(0, 351, 10):
        _, y = ov.px(0, mm)
        long_tick = mm % 50 == 0
        s.append(f'<line x1="{f(92 if long_tick else 96)}" y1="{f(y)}" x2="100" y2="{f(y)}" stroke="{MUTED}" stroke-width="1"/>')
        if long_tick:
            s.append(text(88, y + 4, str(mm), 10, 400, MUTED, "end"))
    s.append(text(88, 140, "mm", 10, 400, MUTED, "end"))
    for n, ((mx, my), _) in MARKS.items():
        x, y = ov.px(mx, my)
        s.append(f'<circle cx="{f(x)}" cy="{f(y)}" r="2.6" fill="#ffffff" stroke="{MARK_EDGE}" stroke-width="1"/>')
    for letter, y0, y1, label in (("A", 12.5, 79, "points 1 to 9"), ("B", 219, 351.5, "points 10 to 14")):
        x0, py0 = ov.px(-2, y0)
        x1, py1 = ov.px(27, y1)
        s.append(f'<rect x="{f(x0)}" y="{f(py0)}" width="{f(x1 - x0)}" height="{f(py1 - py0)}" fill="none" '
                 f'stroke="{INK}" stroke-width="1.2" stroke-dasharray="5 3"/>')
        s.append(f'<rect x="{f(x1 + 4)}" y="{f(py0)}" width="18" height="18" rx="3" fill="{INK}"/>')
        s.append(text(x1 + 13, py0 + 13.5, letter, 12, 700, "#ffffff", "middle"))
        s.append(text(x1 + 27, py0 + 13.5, label, 11, 400, MUTED))
    notes = [(7.3, "P1, guitar cable"), (44.6, "U1 JM60, remove"), (73.3, "U3 accelerometer"),
             (89.3, "sticker"), (121.4, "mounting hole"), (196.0, "ASSY #500-00022-04"),
             (203.0, "(rev 4)")]
    for my, label in notes:
        _, y = ov.px(0, my)
        s.append(text(196, y + 4, label, 11, 400, MUTED))
    for i, (name, col) in enumerate(FRET_COLORS.items()):
        _, y = ov.px(0, 239.0 + FRET_PITCH * i)
        s.append(f'<rect x="196" y="{f(y - 6)}" width="10" height="10" rx="2" fill="{col}"/>')
        s.append(text(211, y + 3.5, f"{name}  ({10 + i})", 11, 600, INK))

    # ---------------------------------------------------------------- A: controller area
    s.append(panel(325, 100, 360, 865, "A   Controller area, points 1 to 9"))
    va = View(337, 140, 12.0, -1.5, 12.5, 28.0, 67.5, "ca")
    defs.append(va.clip())
    s.append(va.open())
    x, y = va.px(12.75, 44.6)
    s.append(f'<rect x="{f(x - 78)}" y="{f(y - 78)}" width="156" height="156" fill="none" stroke="{TARGET}" '
             f'stroke-width="2" stroke-dasharray="6 4"/>')
    s.append(text(x, y - 8, "U1  MC9S08JM60", 12, 700, "#ffffff", "middle"))
    s.append(text(x, y + 12, "REMOVE", 13, 700, TARGET, "middle"))
    for n in range(1, 10):
        s.append(va.target(MARKS[n][1]))
    for n in range(1, 10):
        x, y = va.px(*MARKS[n][0])
        s.append(disc(x, y, n, 9.5, 13))
    s.append("</g>")

    # ---------------------------------------------------------------- B: frets
    s.append(panel(700, 100, 205, 865, "B   Frets, 10 to 14"))
    vb = View(718, 140, 6.0, -1.5, 219.0, 28.0, 134.0, "cb")
    defs.append(vb.clip())
    s.append(vb.open())
    for n in range(10, 15):
        s.append(vb.target(MARKS[n][1]))
        x, y = vb.px(*MARKS[n][0])
        s.append(disc(x, y, n, 8.5, 10))
    s.append("</g>")

    # ---------------------------------------------------------------- C: one fret enlarged
    s.append(panel(920, 100, 560, 300, "C   One fret circuit, enlarged (ORANGE shown)"))
    vc = View(936, 140, 20.0, -0.5, 221.6, 16.0, 10.2, "cc")
    defs.append(vc.clip())
    s.append(vc.open())
    s.append(vc.target("C14.2"))
    tx, ty, tw, th = targets["C14.2"]
    x, y = vc.px(5.15, 222.75)
    px_, py_ = vc.px(tx + tw / 2, ty - th / 2)
    s.append(f'<line x1="{f(x)}" y1="{f(y + 13)}" x2="{f(px_ + 1)}" y2="{f(py_ - 1)}" stroke="#ffffff" stroke-width="2"/>')
    s.append(disc(x, y, 10, 13, 15))
    s.append("</g>")
    s.append(text(936, 370, "All five frets use the same circuit. The wire goes to the pad of C1x", 12.5, 400, INK))
    s.append(text(936, 388, "that faces R2x (the white dot on the marked photo).", 12.5, 400, INK))
    s.append(text(1305, 160, "Fret", 12, 700, MUTED))
    s.append(text(1360, 160, "Solder to", 12, 700, MUTED))
    for i, (name, col) in enumerate(FRET_COLORS.items()):
        y = 186 + i * 27
        s.append(disc(1316, y - 4, 10 + i, 10, 11))
        s.append(f'<rect x="1332" y="{y - 10}" width="9" height="12" rx="2" fill="{col}"/>')
        s.append(text(1360, y, f"C{14 + i}, by R{29 - i}", 12.5, 400, INK))

    # ---------------------------------------------------------------- D: rev 4 vs rev 3
    s.append(panel(920, 415, 560, 235, "D   Accelerometer resistors: rev 4 vs rev 3"))
    for j, (title, pads, ymark, cap) in enumerate((
            ("Rev 4 (this board)", ".1", 67.0, "upper pads of R8, R9, R10"),
            ("Rev 3", ".2", 70.6, "lower pads of R8, R9, R10"))):
        x0 = 950 + j * 270
        s.append(text(x0, 465, title, 13, 700, INK))
        vd = View(x0, 475, 24.0, 10.0, 65.9, 9.8, 5.6, f"cd{j}")
        defs.append(vd.clip())
        s.append(vd.open())
        for n, r in zip((7, 8, 9), ("R8", "R9", "R10")):
            s.append(vd.target(r + pads))
            x, _ = vd.px(MARKS[n][0][0], 0)
            _, y = vd.px(0, ymark)
            s.append(disc(x, y, n, 11, 13))
        s.append("</g>")
        s.append(text(x0, 632, cap, 12.5, 400, INK))

    # ---------------------------------------------------------------- E: table
    s.append(panel(920, 665, 560, 435, "Wiring list"))
    cols = (936, 975, 1080, 1325)
    for cx_, h in zip(cols[1:], ("Signal", "Solder point on the board", "Raspberry Pi Pico")):
        s.append(text(cx_, 717, h, 12, 700, MUTED))
    s.append(f'<line x1="936" y1="725" x2="1464" y2="725" stroke="{PANEL_EDGE}"/>')
    for i, (n, sig, col, where, pico) in enumerate(ROWS):
        y = 750 + i * 25
        if i % 2:
            s.append(f'<rect x="930" y="{y - 17}" width="540" height="25" fill="#f6f5f1"/>')
        s.append(disc(cols[0] + 11, y - 4.5, n, 10, 11))
        s.append(f'<rect x="{cols[1]}" y="{y - 11}" width="4" height="14" rx="1" fill="{col}"/>')
        s.append(text(cols[1] + 10, y, sig, 12.5, 600, INK))
        s.append(text(cols[2], y, where, 12.5, 400, INK))
        s.append(text(cols[3], y, pico, 12.5, 400, INK))

    # ---------------------------------------------------------------- G: notes
    s.append(panel(920, 1115, 560, 150, "Before you start"))
    lines = [
        "1.  Desolder U1 (MC9S08JM60). The Pico takes over its USB lines and inputs.",
        "2.  Never plug in the Pico's micro-USB and the guitar cable at the same time.",
        "3.  Rev 3 boards: points 7, 8 and 9 go to the lower pads of R8, R9 and R10.",
        "4.  Compare with SKAN PCB/MCU.png, SKAN PCB/FRET.png and the original scans.",
    ]
    for i, ln in enumerate(lines):
        s.append(text(936, 1166 + i * 24, ln, 12.5, 400, INK))

    # ---------------------------------------------------------------- F: Pico
    s.append(panel(325, 980, 580, 285, "Raspberry Pi Pico (RP2040), top view"))
    s.append(pico_drawing(372, 1050, 9.0))

    body = "\n".join(s)
    style = (f"<style>text{{font-family:'Segoe UI',Helvetica,Arial,sans-serif}}"
             f".silk{{fill:none;stroke:{SILK};stroke-width:.15}}"
             f".st{{fill:{SILK};font-family:Arial,Helvetica,sans-serif}}"
             f".pad{{fill:{PAD};stroke:{PAD_EDGE};stroke-width:.05}}"
             f".lead{{fill:#cfcfc8;stroke:#8e8d87;stroke-width:.04}}"
             f".ic{{fill:#1c1c1e}}.res{{fill:#232323}}.cap{{fill:#c4a07b}}"
             f".tant{{fill:#e39b2f}}.tband{{fill:#b06d17}}</style>")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W_PX}" height="{H_PX}" viewBox="0 0 {W_PX} {H_PX}">\n'
            f'{style}\n<defs>\n<g id="board">\n' + "\n".join(board) + "\n</g>\n" + "\n".join(defs) +
            "\n</defs>\n" + body + "\n</svg>\n")


def pico_drawing(ox, oy, k) -> str:
    """Pico 51 x 21 mm, USB on the left. Top row: pins 40..21, bottom row: pins 1..20."""
    out = []
    L, Wd = 51.0, 21.0
    used = {  # pin -> (marker, label)
        4: (14, "GP2"), 5: (13, "GP3"), 6: (12, "GP4"), 7: (11, "GP5"), 9: (10, "GP6"),
        10: (3, "GP7"), 11: (4, "GP8"),
        31: (7, "GP26"), 32: (8, "GP27"), 34: (9, "GP28"), 38: (1, "GND"), 40: (2, "VBUS"),
    }
    gnd = {3, 8, 13, 18, 23, 28, 33, 38}
    P = lambda mx, my: (ox + mx * k, oy + my * k)
    x0, y0 = P(0, 0)
    out.append(f'<rect x="{f(x0)}" y="{f(y0)}" width="{f(L * k)}" height="{f(Wd * k)}" rx="{f(k)}" fill="#1e7a3e" stroke="#11512a" stroke-width="1.5"/>')
    ux, uy = P(-1.3, 6.8)
    out.append(f'<rect x="{f(ux)}" y="{f(uy)}" width="{f(5.7 * k)}" height="{f(7.4 * k)}" rx="3" fill="#c9ccd1" stroke="#7d8187" stroke-width="1.2"/>')
    out.append(text(ux + 5.7 * k + 8, P(0, 10.5)[1] + 4, "USB", 11, 700, "#d9f0df"))
    cx, cy = P(36.0, 10.5)
    out.append(text(cx, cy - 2, "Raspberry Pi Pico", 15, 700, "#e9f6ec", "middle"))
    out.append(text(cx, cy + 16, "GND = any square pad", 11.5, 400, "#bfe3c9", "middle"))
    for pin in range(1, 41):
        if pin <= 20:
            mx, my = 1.37 + (pin - 1) * 2.54, Wd - 1.61
        else:
            mx, my = 1.37 + (40 - pin) * 2.54, 1.61
        px, py = P(mx, my)
        r = 0.8 * k
        if pin in gnd:
            out.append(f'<rect x="{f(px - r)}" y="{f(py - r)}" width="{f(2 * r)}" height="{f(2 * r)}" fill="#d6b45c"/>')
        else:
            out.append(f'<circle cx="{f(px)}" cy="{f(py)}" r="{f(r)}" fill="#d6b45c"/>')
        out.append(f'<circle cx="{f(px)}" cy="{f(py)}" r="{f(0.45 * k)}" fill="#3a3a3a"/>')
        if pin in used:
            n, label = used[pin]
            out.append(f'<circle cx="{f(px)}" cy="{f(py)}" r="{f(r + 2.5)}" fill="none" stroke="{TARGET}" stroke-width="3"/>')
            top = pin > 20
            dy = -26 if top else 26
            out.append(f'<line x1="{f(px)}" y1="{f(py + (-r if top else r))}" x2="{f(px)}" y2="{f(py + dy * 0.6)}" stroke="{INK}" stroke-width="1"/>')
            out.append(disc(px, py + dy, n, 9.5, 11))
            ty = py + (r + 6 if top else -(r + 6))
            anchor = "end" if top else "start"
            out.append(f'<text x="{f(px + 4)}" y="{f(ty)}" font-size="10.5" font-weight="700" fill="#ffffff" '
                       f'text-anchor="{anchor}" transform="rotate(-90 {f(px + 4)} {f(ty)})">{label}</text>')
        elif pin in (1, 20, 21):
            ty = py + (r + 12 if pin == 21 else -(r + 5))
            out.append(text(px, ty, str(pin), 10, 700, "#bfe3c9", "middle"))
    # USB data test pads on the underside
    nx = ox + 30.0 * k
    ny = oy - 34
    out.append(disc(nx, ny, 5, 9.5, 11))
    out.append(text(nx + 15, ny + 4.5, "USB D+  →  TP3", 12.5, 600, INK))
    out.append(disc(nx + 140, ny, 6, 9.5, 11))
    out.append(text(nx + 155, ny + 4.5, "USB D−  →  TP2", 12.5, 600, INK))
    out.append(text(nx, ny + 22, "test pads on the underside, next to the USB socket", 11, 400, MUTED))
    return "".join(out)


if __name__ == "__main__":
    OUT.write_text(build(), encoding="utf-8")
    print(f"written {OUT}")
