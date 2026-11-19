#!/usr/bin/env python3
"""
calib_accel.py - kalibracja akcelerometru emulatora RT-GH (MMA7260 na GP26/27/28 Pico).

Oryginalna gitara przelicza napięcia akcelerometru po swojemu na bajty raportu HID. Wzoru nie
znamy, więc mierzymy go: obie gitary ustawiamy w tych samych pozycjach i dopasowujemy
przeliczenie liniowe (ADC Pico -> bajt raportu oryginału) dla każdej osi.

1. Oryginalna gitara do PC (bez Pico):
       py -3.13 calib_accel.py original
2. Gitara z Pico w trybie kalibracji (trzymaj GREEN + RED przy podłączaniu Pico; dioda mruga 2x/s):
       py -3.13 calib_accel.py pico
3. Dopasowanie i zapis do profile.json:
       py -3.13 calib_accel.py fit
4. Przebudowa firmware'u:  powershell -ExecutionPolicy Bypass -File firmware\\build.ps1

Szybki test, czy Pico w ogóle czyta akcelerometr (tryb kalibracji, przechylaj płytkę):
       py -3.13 calib_accel.py live      (tekst w konsoli)
       py -3.13 calib_accel.py view      (okno z paskami i wykresem; działa też dla oryginału)

Pozycje muszą być w obu gitarach takie same. Płytka z akcelerometrem w gitarze z Pico musi być
przykręcona w korpusie tak jak w oryginale (albo trzymana dokładnie w tej samej orientacji).
Wyniki pomiarów: accel_original.json / accel_pico.json (można powtórzyć jeden krok).
"""
import datetime
import json
import statistics
import sys
import time
from pathlib import Path

import hid

VID, PID = 0x0C70, 0x0777
RAW_MARK = 0xA5
ROOT = Path(__file__).resolve().parent
SECONDS = 2.0

POSES = [
    "Leży płasko na plecach (przyciski/struny do góry), gryf w lewo",
    "Leży płasko przodem w dół, gryf w lewo",
    "Pozycja do gry (jak na pasku), gryf poziomo w lewo",
    "Pozycja do gry, gryf uniesiony do góry ok. 45° (jak przy Star Power)",
    "Pozycja do gry, gryf opuszczony w dół ok. 45°",
    "Stoi pionowo na korpusie, gryf prosto do góry",
    "Wisi pionowo, gryf prosto w dół",
    "Leży na boku: górna krawędź korpusu na stole, przyciski w twoją stronę",
]

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass


def s8(b: int) -> int:
    return b - 256 if b > 127 else b


def open_dev():
    devs = hid.enumerate(VID, PID)
    if not devs:
        sys.exit("Nie widzę gitary 0C70:0777. Podłącz ją (oryginał albo Pico) do tego PC.")
    h = hid.device()
    h.open_path(devs[0]["path"])
    return h


def read_reports(h, seconds: float) -> list[bytes]:
    out, t0 = [], time.time()
    while time.time() - t0 < seconds:
        try:
            r = h.read(64, 100)
        except OSError:  # chwilowe zerwanie USB (np. ruszony kabel) - otwórz urządzenie ponownie
            print("   (przerwa w połączeniu USB, łączę ponownie...)")
            h.close()
            for _ in range(50):
                time.sleep(0.2)
                devs = hid.enumerate(VID, PID)
                if devs:
                    try:
                        h.open_path(devs[0]["path"])
                        break
                    except OSError:
                        continue
            else:
                sys.exit("Gitara zniknęła z USB. Sprawdź kabel i uruchom ponownie.")
            out.clear()
            t0 = time.time()
            continue
        if r and len(r) >= 7:
            out.append(bytes(r[:7]))
    return out


def decode_raw(r: bytes) -> list[int]:
    v = int.from_bytes(r[0:4], "little")
    return [((v >> (10 * i)) & 1023) * 4 for i in range(3)]  # z powrotem do skali ADC 0..4095


def record(mode: str):
    h = open_dev()
    probe = read_reports(h, 0.5)
    if not probe:
        sys.exit("Gitara nie wysyła raportów.")
    is_raw = all(r[4] == RAW_MARK for r in probe)
    if mode == "pico" and not is_raw:
        sys.exit("To nie jest Pico w trybie kalibracji (bajt 4 != 0xA5). Odłącz gitarę, trzymaj GREEN + RED "
                 "i podłącz ją ponownie (albo krótko naciśnij BOOTSEL na Pico), potem uruchom skrypt jeszcze raz.")
    if mode == "original" and is_raw:
        sys.exit("To Pico w trybie kalibracji, a krok 'original' jest dla oryginalnej gitary.")
    print(f"Pomiar: {'oryginał' if mode == 'original' else 'Pico (surowe ADC)'}. "
          f"Każda pozycja: {SECONDS:.0f} s bez ruszania gitarą. Enter = mierz, s = pomiń, q = koniec.\n")
    values = []
    for i, pose in enumerate(POSES):
        ans = input(f"[{i + 1}/{len(POSES)}] {pose}\n   -> ustaw i naciśnij Enter: ").strip().lower()
        if ans == "q":
            values += [None] * (len(POSES) - i)
            break
        if ans == "s":
            values.append(None)
            continue
        reps = read_reports(h, SECONDS)
        if mode == "original":
            v = [statistics.median(s8(r[b]) for r in reps) for b in range(5)]
            print("   bajty 0..4:", " ".join(f"{x:+5.0f}" for x in v))
        else:
            reps = [r for r in reps if r[4] == RAW_MARK]
            if not reps:
                sys.exit("Pico wyszło z trybu kalibracji (restart?). Podłącz gitarę ponownie trzymając GREEN + RED "
                         "i powtórz krok 'pico'.")
            v = [statistics.median(decode_raw(r)[c] for r in reps) for c in range(3)]
            print("   ADC0..2 (GP26/27/28):", " ".join(f"{x:5.0f}" for x in v),
                  " = " + " ".join(f"{x * 3.3 / 4095:.2f} V" for x in v))
        values.append(v)
    h.close()
    out = ROOT / f"accel_{mode}.json"
    out.write_text(json.dumps({"created": datetime.datetime.now().isoformat(timespec="seconds"),
                               "poses": POSES, "values": values}, indent=1), encoding="utf-8")
    print(f"\nZapisano {out}")


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            raise ZeroDivisionError
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def lstsq(xs: list[list[float]], ys: list[float], ridge: float = 1e-6) -> list[float]:
    k = len(xs[0])
    ata = [[sum(x[i] * x[j] for x in xs) + (ridge if i == j and i > 0 else 0) for j in range(k)] for i in range(k)]
    aty = [sum(x[i] * y for x, y in zip(xs, ys)) for i in range(k)]
    return solve(ata, aty)


def predict(c: list[float], adc: list[float]) -> float:
    return c[0] + c[1] * adc[0] + c[2] * adc[1] + c[3] * adc[2]


def fit():
    try:
        orig = json.loads((ROOT / "accel_original.json").read_text(encoding="utf-8"))["values"]
        pico = json.loads((ROOT / "accel_pico.json").read_text(encoding="utf-8"))["values"]
    except OSError as e:
        sys.exit(f"Brak pomiaru: {e}. Najpierw kroki 'original' i 'pico'.")
    names = POSES
    pairs = [(o, p, names[i] if i < len(names) else f"#{i + 1}")
             for i, (o, p) in enumerate(zip(orig, pico)) if o is not None and p is not None]
    if len(pairs) < 3:
        sys.exit(f"Za mało wspólnych pozycji ({len(pairs)}), potrzeba co najmniej 3 (najlepiej wszystkie).")
    prof_path = ROOT / "profile.json"
    prof = json.loads(prof_path.read_text(encoding="utf-8"))
    usage = {e["byte"]: e["usage"] for e in prof["report_map"]["elements"] if e.get("size") == 8}
    fits = []
    print(f"Wspólnych pozycji: {len(pairs)}\n")
    # Ten sam czujnik w tej samej orientacji: każdy bajt oryginału zależy od jednej osi akcelerometru.
    # Dopasowanie y = c + k*ADC[ch] dla najlepszego kanału; jedna pozycja wyraźnie odstająca (gitara
    # ustawiona inaczej niż przy drugim pomiarze) jest odrzucana.
    for b in range(5):
        ys = [o[b] for o, _, _ in pairs]
        if max(ys) - min(ys) < 6:
            print(f"bajt {b} ({usage.get(b, '?')}): stały ({statistics.median(ys):+.0f}) - zostaje z raportu neutralnego")
            continue
        pts = [(p, o[b], n) for o, p, n in pairs if abs(o[b]) < 126]  # nasycone (+-127) nie nadają się do prostej
        sat = [(p, o[b], n) for o, p, n in pairs if abs(o[b]) >= 126]

        def fit_ch(points, ch):
            try:
                c, k = lstsq([[1.0, p[ch]] for p, _, _ in points], [y for _, y, _ in points], ridge=0)
            except ZeroDivisionError:
                return None
            res = [c + k * p[ch] - y for p, y, _ in points]
            return (sum(r * r for r in res) / len(res)) ** 0.5, ch, c, k

        if len(pts) < 2:
            print(f"bajt {b} ({usage.get(b, '?')}): za mało nienasyconych punktów - pominięty")
            continue
        best = min((f for f in (fit_ch(pts, ch) for ch in range(3)) if f), key=lambda f: f[0])
        rms, ch, c, k = best
        dropped = None
        if len(pts) >= 4:
            res_abs = sorted(abs(c + k * t[0][ch] - t[1]) for t in pts)
            med = res_abs[len(res_abs) // 2]
            worst = max(pts, key=lambda t: abs(c + k * t[0][ch] - t[1]))
            if abs(c + k * worst[0][ch] - worst[1]) > max(15.0, 3 * med):
                rest = [t for t in pts if t is not worst]
                f2 = fit_ch(rest, ch)
                if f2 and f2[0] < rms * 0.6:
                    rms, ch, c, k = f2
                    dropped = worst
        coef = [c, 0.0, 0.0, 0.0]
        coef[1 + ch] = k
        axis = ("Z/GP26", "Y/GP27", "X/GP28")[ch]
        bad_sat = [n for p, y, n in sat if (predict(coef, p) * y < 0) or abs(predict(coef, p)) < 100]
        print(f"bajt {b} ({usage.get(b, '?')}): oś {axis}, bajt = {c:.1f} {k:+.4f} * ADC, "
              f"0 g przy ADC {(-c / k) if k else 0:.0f}, błąd RMS {rms:.1f}"
              + (f"\n      UWAGA: nasycone pozycje się nie zgadzają: {', '.join(bad_sat)}" if bad_sat else ""))
        for (p, y, n) in pts + sat:
            mark = "  (odrzucona - inne ustawienie gitary?)" if dropped is not None and n == dropped[2] else ""
            print(f"      {n[:46]:46s} ADC {p[ch]:5.0f} -> oryginał {y:+5.0f}, Pico {predict(coef, p):+7.1f}{mark}")
        fits.append({"byte": b, "usage": usage.get(b, "?"), "coef": coef, "rms": rms, "points": len(pts),
                     "axis": axis, "dropped": dropped[2] if dropped else None})
    if not fits:
        sys.exit("Nic do zapisania.")
    prof["accel"] = {"created": datetime.datetime.now().isoformat(timespec="seconds"),
                     "adc_gpio": [26, 27, 28], "fits": fits}
    prof_path.write_text(json.dumps(prof, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nZapisano kalibrację w {prof_path}. Teraz: powershell -ExecutionPolicy Bypass -File firmware\\build.ps1")


def live():
    """Podgląd na żywo: Pico w trybie kalibracji -> napięcia osi; oryginał -> bajty 0..4."""
    h = open_dev()
    print("Podgląd na żywo (Ctrl+C = koniec). Przechylaj płytkę/gitarę - liczby mają się zmieniać.\n")
    lo, hi = [9999] * 3, [0] * 3
    try:
        while True:
            reps = read_reports(h, 0.2)
            if not reps:
                print("\rbrak raportów...                                        ", end="", flush=True)
                continue
            r = reps[-1]
            if r[4] == RAW_MARK:
                a = decode_raw(r)
                for i in range(3):
                    lo[i], hi[i] = min(lo[i], a[i]), max(hi[i], a[i])
                txt = "  ".join(f"{n} {a[i] * 3.3 / 4095:4.2f} V (zakres {(hi[i] - lo[i]) * 3.3 / 4095:4.2f} V)"
                                for i, n in enumerate(("Z/GP26", "Y/GP27", "X/GP28")))
            else:
                txt = ("zwykły raport (nie tryb kalibracji - podłącz trzymając GREEN + RED), bajty 0..4: "
                       + " ".join(f"{s8(r[b]):+5d}" for b in range(5)))
            print("\r" + txt + "   ", end="", flush=True)
    except KeyboardInterrupt:
        print()
    h.close()


def view():
    """Okno z wykresem: Pico w trybie kalibracji -> napięcia 3 osi; oryginał/emulator -> bajty 1, 2, 4."""
    import collections
    import threading
    import tkinter as tk

    h = open_dev()
    lock = threading.Lock()
    state = {"last": None, "stop": False}

    def reader():
        while not state["stop"]:
            try:
                r = h.read(64, 100)
            except OSError:
                break
            if r and len(r) >= 7:
                with lock:
                    state["last"] = bytes(r[:7])

    threading.Thread(target=reader, daemon=True).start()

    W, BAR_H, PLOT_H, PAD = 900, 46, 300, 16
    HIST = 300  # próbek na wykresie (~15 s przy 20 Hz)
    COLORS = ("#e0483e", "#2e9d4c", "#2f6fd6")
    hist = [collections.deque(maxlen=HIST) for _ in range(3)]
    root = tk.Tk()
    root.title("RT-GH akcelerometr")
    status = tk.Label(root, font=("Segoe UI", 11), anchor="w", justify="left")
    status.pack(fill="x", padx=PAD, pady=(PAD, 4))
    cv = tk.Canvas(root, width=W, height=3 * (BAR_H + 8) + PLOT_H + 3 * PAD, bg="white", highlightthickness=0)
    cv.pack(padx=PAD, pady=(0, PAD))
    mode = {"raw": None}

    def norm(raw: bool, v: float) -> float:  # 0..1
        return v / 4095 if raw else (v + 128) / 255

    def redraw():
        with lock:
            r = state["last"]
        cv.delete("all")
        if r is None:
            status.config(text="Czekam na raporty z gitary...")
            root.after(100, redraw)
            return
        raw = r[4] == RAW_MARK
        if mode["raw"] != raw:  # zmiana trybu: wyczyść historię
            mode["raw"] = raw
            for q in hist:
                q.clear()
        if raw:
            vals = decode_raw(r)
            names = ("Z  (pin 13 -> GP26)", "Y  (pin 14 -> GP27)", "X  (pin 15 -> GP28)")
            labels = [f"{v * 3.3 / 4095:.2f} V" for v in vals]
            status.config(text="Pico w trybie kalibracji: surowe napięcia z akcelerometru (0-3,3 V, środek 1,65 V = 0 g). "
                               "Powrót do zwykłego trybu: podłącz gitarę ponownie bez trzymania progów.")
        else:
            vals = [s8(r[1]), s8(r[2]), s8(r[4])]
            names = ("bajt 1 (Y)", "bajt 2 (Z)", "bajt 4 (Ry)")
            labels = [f"{v:+d}" for v in vals]
            status.config(text="Zwykły raport gitary: bajty przechyłu wysyłane do gry (-127..127). "
                               "Pico bez kalibracji wysyła tu stałe wartości - podłącz gitarę trzymając "
                               "GREEN + RED, żeby zobaczyć surowe napięcia.")
        for i, v in enumerate(vals):
            hist[i].append(norm(raw, v))
        # paski
        x0, x1 = 200, W - 110
        for i, v in enumerate(vals):
            y = PAD + i * (BAR_H + 8)
            cv.create_text(8, y + BAR_H / 2, text=names[i], anchor="w", font=("Segoe UI", 11))
            cv.create_rectangle(x0, y, x1, y + BAR_H, outline="#999", fill="#f2f2f2")
            mid = (x0 + x1) / 2
            xv = x0 + (x1 - x0) * norm(raw, v)
            cv.create_rectangle(min(mid, xv), y + 4, max(mid, xv), y + BAR_H - 4, outline="", fill=COLORS[i])
            cv.create_line(mid, y, mid, y + BAR_H, fill="#555", dash=(3, 3))
            cv.create_text(x1 + 10, y + BAR_H / 2, text=labels[i], anchor="w", font=("Consolas", 13, "bold"))
        # wykres w czasie
        top = PAD + 3 * (BAR_H + 8) + PAD
        cv.create_rectangle(0, top, W - 1, top + PLOT_H, outline="#999")
        cv.create_line(0, top + PLOT_H / 2, W, top + PLOT_H / 2, fill="#ccc", dash=(3, 3))
        cv.create_text(6, top + 4, text="ostatnie ~15 s", anchor="nw", fill="#777", font=("Segoe UI", 9))
        for i, q in enumerate(hist):
            if len(q) < 2:
                continue
            pts = []
            for k, v in enumerate(q):
                pts += [W * k / (HIST - 1), top + PLOT_H - 4 - (PLOT_H - 8) * v]
            cv.create_line(*pts, fill=COLORS[i], width=2)
            short = ("Z", "Y", "X") if raw else ("Y (b1)", "Z (b2)", "Ry (b4)")
            cv.create_text(W - 8, top + 8 + 16 * i, text=short[i], anchor="ne", fill=COLORS[i],
                           font=("Segoe UI", 10, "bold"))
        root.after(50, redraw)

    def on_close():
        state["stop"] = True
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    redraw()
    root.mainloop()
    h.close()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode in ("original", "pico"):
        record(mode)
    elif mode == "live":
        live()
    elif mode == "view":
        view()
    elif mode == "fit":
        fit()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
