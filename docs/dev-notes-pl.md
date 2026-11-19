# Klon gitary RT-GH na Raspberry Pi Pico

Część 1: `capture.py` profiluje oryginalną gitarę na Windows (deskryptory USB, mapa
wejść, neutralny raport, timing) i zapisuje `profile.json`. Tym samym skryptem
(`--verify`) sprawdzasz potem, czy Pico wygląda dla hosta tak samo.

`analyze_pcap.py` pozostaje bez zmian i czyta te same pliki pcap z USBPcap.

## Windows: instalacja

### Python + hidapi

Skrypt działa na Pythonie 3. Pakiet `hidapi` ma gotowe paczki (wheel) tylko dla
wydanych już wersji Pythona. Na najnowszym Pythonie (np. 3.15) `pip` próbuje go
kompilować i kończy się błędem. Najprościej użyć Pythona 3.13:

```bash
py install 3.13
```

```bash
py -3.13 -m pip install hidapi
```

Nie instaluj pakietu `hid` (to inny pakiet o tej samej nazwie modułu). Jeśli już
jest: `py -3.13 -m pip uninstall hid`.

### Wireshark + USBPcap

1. Zainstaluj Wiresharka z https://www.wireshark.org/. W instalatorze **zaznacz
   „Install USBPcap”** (osobny instalator USBPcap uruchomi się w trakcie).
2. **Uruchom ponownie komputer** (sterownik USBPcap ładuje się dopiero po restarcie).
3. Skrypt sam znajduje `tshark.exe` (`C:\Program Files\Wireshark`) i `USBPcapCMD.exe`
   (`C:\Program Files\USBPcap` albo `Wireshark\extcap`). Inna ścieżka:
   `--tshark ...`, `--usbpcapcmd ...`.

Gitara zostaje na systemowym sterowniku HID: bez Zadiga, libusb i WinUSB.

## Profilowanie oryginału

Otwórz terminal **jako Administrator** (USBPcap tego wymaga):

```bash
py -3.13 capture.py
```

Kolejne kroki:

1. hidapi szuka urządzenia z product string `RT-GH INPUT` (inny: `--product`,
   albo `--vid 0x.... --pid 0x....`) i wypisuje VID/PID, stringi, usage i ścieżkę.
2. Wypisuje interfejsy USBPcapN z drzewem urządzeń i sam znajduje gitarę po
   VID/PID (krótkie nagrania z `--inject-descriptors`). Ręcznie: `--usbpcap 1`.
3. Nagrywa `enum.pcap`. Na polecenie **odepnij gitarę i wepnij ją do tego samego
   portu**, a po zakończeniu instalacji przez Windows naciśnij Enter. Skrypt
   wyciąga surowe deskryptory: device, configuration (interfejsy, endpointy, HID),
   HID report descriptor, stringi z LANGID, BOS/qualifier, żądania class/vendor
   (np. MS OS descriptors) i statusy (STALL) wszystkich żądań. Jeśli czegoś
   brakuje, wypisuje co i proponuje powtórkę. tshark służy do kontroli (liczba
   pakietów).
   Zapytań huba Windows (LANGID, iProduct, 0xEE) USBPcap nie widzi, a o część
   stringów (np. iConfiguration) Windows w ogóle nie pyta. Dlatego skrypt pyta o
   stringi 0..najwyższy używany indeks przez `HidD_GetIndexedString` (systemowy
   sterownik HID) i nagrywa to do `strings.pcap`, co daje surowe bajty. W ten
   sposób wychodzą też ukryte stringi (u RT-GH: indeks 3 = `V0.1`).
   **Uwaga:** oryginalna gitara po pytaniu o nieistniejący string przestaje
   odpowiadać na wszystkie stringi (hidapi pokazuje wtedy pustą nazwę), dopóki jej
   nie odepniesz i nie wepniesz. Szersze skanowanie (0..32 i 0xEE) jest tylko na
   żądanie: `--probe-all-strings`.
4. Porównuje report descriptor z pcapa z `get_report_descriptor()` z hidapi.
   **Na Windowsie hidapi rekonstruuje deskryptor z preparsed data**, więc bajty
   zwykle się różnią. Skrypt porównuje wtedy pola semantycznie. Do firmware'u
   używaj wersji z pcapa.
5. Wypisuje mapę pól (report ID, bajt.bit, rozmiar, usage, zakres) i odpytuje
   report ID typu Feature/Input (`get_feature_report` / `get_input_report`).
6. Baseline (domyślnie 5 s, `--baseline-seconds`): gitara leży nieruchomo.
   Skrypt wyznacza bity szumiące (akcelerometr) i neutralny raport (mediana).
   Pole HID z szumem jest ignorowane w całości. Równolegle nagrywa `timing.pcap`:
   okres raportowania i to, czy raporty idą cyklicznie, liczy z timestampów
   interrupt IN, nie z hidapi.
7. Kalibracja: GREEN, RED, YELLOW, BLUE, ORANGE, STRUM UP, STRUM DOWN. Dla każdego
   wejścia: wciśnij i trzymaj, Enter, puść, Enter. `s` pomija wejście, `p` powtarza
   pomiar przy ostrzeżeniu. Lista: `--inputs "GREEN,RED,..."`. Pominięcie całej
   kalibracji: `--skip-calibration`. Skrypt ostrzega o konfliktach (dwa wejścia
   dają tę samą zmianę), o kilku polach zmienionych naraz i o bitach, które nie
   wróciły do stanu neutralnego.
8. Zapis: `profile.json`, `enum.pcap`, `timing.pcap` w bieżącym katalogu
   (`--out KATALOG`).

Analiza samego pliku, bez gitary i bez uprawnień administratora:

```bash
py -3.13 capture.py --parse-only --from-pcap enum.pcap --vid 0x1234 --pid 0x5678
```

## Weryfikacja Pico

Podłącz Pico z firmware'em (oryginał odłącz, jeśli ma ten sam VID/PID) i
uruchom jako Administrator:

```bash
py -3.13 capture.py --verify
```

Skrypt nagrywa enumerację Pico (`verify_enum.pcap`, z odpięciem i wpięciem) i
baseline z timingiem (`verify_timing.pcap`). Porównuje bajt po bajcie z
`profile.json`: deskryptory z nazwą pola (np. `ENDPOINT@27.bInterval`), stringi,
odpowiedzi class/vendor, statusy żądań, stringi z hidapi, neutralny raport (bez
bitów szumiących oryginału) i okres raportowania. Różnice wypisuje i zapisuje do
`verify_report.json`. Kod wyjścia to 1, jeśli są różnice.

## Firmware Pico (`firmware/`)

Pico udaje oryginalną gitarę: ten sam VID:PID `0C70:0777`, deskryptory, stringi
(też ukryty `V0.1`), report descriptor i neutralny raport, wszystko bajt w bajt z
`profile.json`. Raport 7 B idzie cyklicznie co 32 ms, jak u oryginału. Interfejs
„RT-GH LED” istnieje i przyjmuje wszystko, co wyśle gra, ale niczego nie odsyła
(protokół nieznany). Gra może więc nie dostać odpowiedzi, których oczekuje od
oryginału. Odebrane dane idą do logu.

| Plik | Co robi |
|---|---|
| `src/rtgh_profile.h` | **generowany** z `profile.json`: `py -3.13 gen_firmware_header.py` |
| `src/pins.h` | przypisanie przycisków do pinów, debounce |
| `src/main.c` | odczyt przycisków, budowa raportu, dioda, log |
| `src/usb_descriptors.c` | deskryptory dla TinyUSB |
| `src/led_iface.c` | interfejs „RT-GH LED”: przyjmuje i loguje |

### Podłączenie przycisków

Każde wejście to pin GPIO z wewnętrznym pull-upem. **Wciśnięcie = zwarcie pinu
do GND.** Wystarczy przewód albo zwykły mikroprzełącznik (NO) między pinem a GND.
Rezystory nie są potrzebne.

| Wejście | GPIO | Pin fizyczny | Co dostaje gra |
|---|---|---|---|
| GREEN | GP2 | 4 | Button 1 (bajt 5, bit 4) |
| RED | GP3 | 5 | Button 2 (bajt 5, bit 5) |
| YELLOW | GP4 | 6 | Button 4 (bajt 5, bit 7) |
| BLUE | GP5 | 7 | Button 3 (bajt 5, bit 6) |
| ORANGE | GP6 | 9 | Button 5 (bajt 6, bit 0) |
| STRUM UP | GP7 | 10 | Hat = 0 |
| STRUM DOWN | GP8 | 11 | Hat = 2 |
| GND | | 3, 8, 13, 18 ... | wspólna masa przycisków |
| (zworka do GND przy starcie) | GP9 | 12 | tryb zrzutu logu zamiast gitary |

Strum góra i dół jednocześnie dają neutralny hat (15). Inne piny ustawisz w
`firmware/src/pins.h`.

Dioda na Pico: miga wolno, gdy brak połączenia USB z hostem. Świeci, gdy host
skonfigurował urządzenie. Gaśnie, gdy trzymasz przycisk. Krótko mruga, gdy gra
wyśle coś do interfejsu LED.

Log (opcjonalny): UART0, **GP0 = TX** (pin 1), 115200 8N1, przez przejściówkę
USB-UART 3,3 V (TX Pico do RX przejściówki, plus GND). Widać w nim wciśnięcia,
żądania kontrolne i każde nowe dane z gry do interfejsu LED (`LED OUT ...`). To
podstawa do rozpracowania podświetlenia.

### Gotowe pliki i wgrywanie

Zbudowane pliki leżą w `firmware\bin\`:

| Płytka | Plik |
|---|---|
| Raspberry Pi Pico / Pico H (RP2040) | `rtgh_pico_rp2040.uf2` |
| Raspberry Pi Pico 2 (RP2350) | `rtgh_pico2_rp2350.uf2` |

Wgrywanie: przytrzymaj **BOOTSEL**, podłącz Pico kablem USB i puść przycisk.
Pojawi się dysk `RPI-RP2` (albo `RP2350`). Przeciągnij na niego właściwy `.uf2`.
Pico sam się zrestartuje jako gitara.

Na Pico W/2 W firmware też zadziała, ale bez diody (tam dioda wisi na układzie
Wi-Fi).

### Przebudowa (narzędzia w `C:\pico`)

W `C:\pico` są już SDK 2.3.1, kompilator ARM, CMake, Ninja i picotool (wersje
przenośne, nic nie jest zainstalowane w systemie). Po zmianie `pins.h` albo po
nowym `profile.json`, ze zwykłego PowerShella w katalogu projektu:

```bash
powershell -ExecutionPolicy Bypass -File firmware\build.ps1
```

Skrypt generuje `rtgh_profile.h` z `profile.json`, kopiuje źródła do
`C:\pico\rtgh_pico`, buduje obie płytki i wrzuca `.uf2` do `firmware\bin\`.

### Budowanie w VS Code (alternatywa)

**Skopiuj katalog `firmware` do ścieżki bez spacji i polskich znaków**, np.
`C:\pico\rtgh_pico`. Toolchain ARM na Windowsie potrafi się wyłożyć na ścieżce
typu `C:\Users\Gdańsk Arcade Museum\...`.

1. Zainstaluj VS Code i rozszerzenie **Raspberry Pi Pico**
   (`raspberry-pi.raspberry-pi-pico`). Rozszerzenie samo pobiera Pico SDK,
   toolchain ARM, CMake, Ninja i picotool. Nie trzeba ich instalować osobno.
2. W panelu Raspberry Pi Pico wybierz **Import Project** i wskaż
   `C:\pico\rtgh_pico`. Ustaw wersję SDK (najnowszą) i płytkę (`pico`, `pico2`
   albo `pico_w`).
3. **Compile Project** (pasek stanu albo panel Pico). Wynik:
   `build\rtgh_pico.uf2`.
4. Wgrywanie: przytrzymaj BOOTSEL, podłącz Pico i przeciągnij `.uf2` na dysk
   `RPI-RP2` / `RP2350`. Albo użyj **Run Project (USB)**, które wgrywa przez
   picotool.

Po nowym `profile.json`: `py -3.13 gen_firmware_header.py`, skopiuj
`firmware\src\rtgh_profile.h` do kopii w `C:\pico\rtgh_pico\src` i przebuduj.

### Test na PC, potem na automacie

1. **PC:** odłącz oryginalną gitarę, podłącz Pico. W `joy.cpl` (Kontrolery gier,
   Właściwości) zwarcie GP2..GP6 do GND zapala przyciski 1–5, a GP7/GP8 ruszają
   hat (POV).
2. **PC, porównanie z oryginałem** (terminal jako Administrator):
   `py -3.13 capture.py --verify`. Powinno wyjść „Brak różnic”, ewentualnie
   różnice tylko w bitach szumiących.
3. **Automat:** podłącz Pico w miejsce kabla USB gitary. Pico jest zasilane z
   USB. Na menu powinny działać strum i progi. Jeśli gra nie widzi gitary albo
   zgłasza błąd, sprawdź w logu UART, czy przyszło `USB: skonfigurowane przez
   hosta` i czy gra wysyła coś na `LED OUT` albo `CTRL IN -> STALL`.
   Ten ostatni wpis znaczy, że gra o coś pyta, a my nie znamy odpowiedzi.

### Diagnostyka na automacie: log we flashu Pico

Firmware zapisuje w ostatnich 512 KB flasha, co robi z nim host:
- każde żądanie kontrolne (SETUP),
- bus reset, suspend i resume,
- dane wysłane na interfejs LED,
- SET/GET_REPORT,
- zmiany wejść,
- co 2 s statystyki: ile raportów HID poszło i **czy host odpytywał endpointy IN
  0x82/0x84 interfejsu LED**. Pico na nie nic nie odsyła, więc sprzęt RP2040
  odnotowuje NAK.

Log przetrwa odłączenie zasilania. Kolejne podłączenia dopisują się jako nowe
sesje.

1. Wgraj `firmware\bin\rtgh_pico_rp2040.uf2`. Przy pierwszym starcie Pico
   kasuje obszar logu (ok. 2 s, zanim zgłosi się po USB).
2. Podłącz Pico do automatu, odczekaj, aż gra wystartuje, spróbuj „guitar
   mating” i powciskaj przyciski.
3. Odłącz Pico od automatu. Załóż zworkę między **GP9 i GND (fizyczne piny 12
   i 13**, sąsiednie) i podłącz Pico do tego PC normalnie, **bez BOOTSEL**.
   Pico zgłosi się wtedy jako port szeregowy (COMx, sterownik wbudowany w
   Windows), a dioda mruga podwójnie. Uruchom (jednorazowo
   `py -3.13 -m pip install pyserial`):

   ```bash
   py -3.13 read_pico_log.py
   ```

   Skrypt sam znajdzie port, pobierze log (512 KB, kilka sekund), zdekoduje go i
   zapisze `pico_log_<data>.txt`. Z `--clear` po odczycie skasuje log, żeby
   następny test zaczął się od czystej karty. Zdejmij zworkę, a Pico znowu
   będzie gitarą. Dysk BOOTSEL nie nadaje się do odczytu: na RP2040 pokazuje
   tylko `INDEX.HTM` i `INFO_UF2.TXT`, a nie zawartość flasha.

W logu `!` oznacza rzeczy, których Pico nie obsługuje tak jak oryginał:
żądania vendor, dziwne deskryptory, dane na LED. Najważniejsze pytania:
- Czy jest `MOUNT`? Jeśli nie, host odrzucił urządzenie już przy enumeracji.
- Czy gra wysyła `VENDOR` / `LED_OUT`? To może być handshake albo parowanie.
- Czy `STATS` pokazuje NAK na EP2/EP4 IN? Wtedy gra czeka na odpowiedź od
  gitary.

Warto też zrobić jedną sesję na tym PC (normalne podłączenie bez zworki), żeby
mieć porównanie z enumeracją przez Windows.

### Proxy: nagranie rozmowy automatu z oryginalną gitarą (`rtgh_proxy_rp2040.uf2`)

Pico siedzi między automatem a **działającą oryginalną gitarą** i przekazuje
wszystko w obie strony:
- raporty HID (grasz oryginałem),
- interfejs „RT-GH LED”: EP 0x03 od automatu oraz EP 0x82/0x84 od gitary,
- żądania vendor/class.

Każda nowa treść w obie strony trafia do logu we flashu. Tylko RP2040.

Podłączenie gniazda USB-A (gitara):

| Gniazdo USB-A | Pico |
|---|---|
| 1 VBUS (5 V) | VBUS, pin 40 (5 V z kabla USB Pico) |
| 2 D- | GP17, pin 22 |
| 3 D+ | GP16, pin 21 |
| 4 GND | GND, pin 23 |

Rezystory 22 Ω w D+/D- są opcjonalne. Gitara dostaje zasilanie z tego samego
źródła co Pico: z automatu albo z PC.

Dioda Pico:
- miga wolno: automat lub PC nie skonfigurował Pico,
- miga szybko: Pico nie widzi gitary na GP16/GP17,
- świeci: wszystko połączone,
- krótkie mrugnięcia: ruch na interfejsie LED.

Procedura:
1. Wgraj `firmware\bin\rtgh_proxy_rp2040.uf2` przez BOOTSEL. Stary log zostaje,
   nowe sesje mają w nagłówku `boot rtgh_proxy`.
2. **Test na PC:** gitara do gniazda USB-A, Pico do PC. Dioda ma świecić, a w
   `joy.cpl` progi i strum gitary mają działać przez Pico.
3. **Automat:** wyłącz go, podłącz Pico (z gitarą) w miejsce gitary, włącz.
   Zrób switch test, a potem mating oryginałem: ORANGE, trzymaj GREEN, P1/P2
   start.
4. Odczyt: zworka GP9–GND, Pico do PC, `py -3.13 read_pico_log.py`. W logu
   szukaj `LED_OUT` (automat → gitara), `GITARA -> AUTOMAT EP 0x8x` (odpowiedzi
   gitary) i `CTRL do gitary`.

Jeśli dioda miga szybko mimo podłączonej gitary, sprawdź, czy D+ i D- nie są
zamienione. Błędy transferów gitary log pokazuje jako `guitar xfer err`.

Budowanie proxy wymaga biblioteki Pico-PIO-USB w
`C:\pico\pico-sdk\lib\tinyusb\hw\mcu\raspberry_pi\Pico-PIO-USB` (wersja
`fe9133fc`, przypięta przez TinyUSB). `build.ps1` nakłada też na `usbh.c`
TinyUSB małą łatkę: host pomija wtedy deskryptor OTG z konfiguracji gitary.

### Wersja docelowa: montaż w gitarze (firmware 1.0)

Schemat podłączeń: [schemat_podlaczenia.svg](schemat_podlaczenia.svg). W skrócie:
- Kabel gitary zasila Pico (5 V → VBUS, pin 40). Linie danych z padów
  USBDP/USBDN po JM60 idą na TP3/TP2 na spodzie Pico.
- Płytka ma własne 3,3 V. Przewód z testów 3V3 Pico → VDD płytki trzeba usunąć.
- Progi GP2–GP6, strum GP7/GP8 (jeszcze do podłączenia), akcelerometr
  GP26–GP28.

Firmware: `firmware\bin\rtgh_pico_rp2040.uf2`. Zawiera:
- deskryptory 1:1 z oryginałem,
- progi, strum z autopolaryzacją, akcelerometr,
- mating (P1/P2 zapamiętywane we flashu),
- log we flashu,
- watchdog: restart po zawieszeniu dłuższym niż 1 s, odnotowany w logu.

Obsługa zakręconej gitary. Progi trzymasz w chwili podłączania kabla gitary do
PC, strum zostaw w spokoju:

| Progi | Tryb |
|---|---|
| GREEN + RED | kalibracja akcelerometru (surowe ADC w raporcie) |
| BLUE + ORANGE | odczyt logu (port COM, `read_pico_log.py`) |
| GREEN + ORANGE | wgrywanie firmware'u: pojawi się dysk RPI-RP2, przeciągnij `.uf2` |

Przy otwartej gitarze działa też BOOTSEL: krótko włącza i wyłącza
kalibrację, a przytrzymany ≥ 1 s włącza odczyt logu.

Raz trzeba zrobić **kalibrację akcelerometru z oryginalną gitarą** (sekcja
niżej). Kombinacja przycisków albo krótkie BOOTSEL tylko włącza tryb
pomiaru. Samo przeliczenie liczy `calib_accel.py fit` na PC, a potem
`build.ps1` wbudowuje je w firmware. Bez kalibracji gra dostaje stałe wartości
przechyłu, więc przechylanie gitary nie działa.

Wgrywanie przy zakręconej gitarze: trzymaj GREEN + ORANGE i podłącz kabel
gitary do PC. Na otwartej możesz też użyć BOOTSEL i micro-USB Pico, ale wtedy z
odłączonym kablem gitary. **Nigdy nie podłączaj micro-USB i kabla gitary
naraz.**

### Mating: co wymienia automat z gitarą (z nagrania proxy, 2026-11-12)

„Guitar mating” przypisuje gitarze numer gracza:
- Gra wysyła na interfejs „RT-GH LED” (EP 0x03) 64-bajtowy pakiet `03 0n …`,
  gdzie `n = 0` oznacza P1, a `n = 1` oznacza P2.
- Oryginalna gitara potwierdza to po około 50 ms w **raporcie HID: bajt 6, bity
  4..7**. Zapalony jest jeden bit `4+n`, więc `0x14` to P1, a `0x24` to P2.
  Niższe bity (ORANGE, Button 7) zostają bez zmian.
- Endpointy 0x82/0x84 i żądania kontrolne nie są używane.
- Bez potwierdzenia gra powtarza `03 0n` co 250 ms. Po starcie automatu ponawia
  przez około 9 s i rezygnuje.

Emulator (`rtgh_pico_*.uf2`) robi to samo i zapisuje numer gracza we flashu, w
sektorze przed logiem. Do logu trafiają wpisy `player P1` przy starcie i
`player set P2` po matingu.

Log można też odczytać bez zworki: gdy firmware działa, przytrzymaj **BOOTSEL
przez około 1 s**. Pico zrestartuje się w tryb zrzutu (port COM).

### Akcelerometr (MMA7260QT) i kalibracja przechyłu

Akcelerometr ma wyjścia analogowe (~1,65 V przy 0 g, ±0,8 V/g), wyprowadzone
przez rezystory 1 kΩ („102”). Pico podłączamy za rezystorami, od strony dawnego
JM60:

| MMA7260 | Pico |
|---|---|
| pin 13 ZOUT | GP26 / ADC0 (pin 31) |
| pin 14 YOUT | GP27 / ADC1 (pin 32) |
| pin 15 XOUT | GP28 / ADC2 (pin 34) |
| masa | GND |

Pin 12 (Sleep) musi być w stanie wysokim.

Oryginalna gitara przelicza napięcia na bajty raportu 1 (Y), 2 (Z) i 4 (Ry) po
swojemu. Dlatego przelicznik się mierzy: obie gitary ustawiamy w tych samych
pozycjach.

1. Oryginalna gitara do PC: `py -3.13 calib_accel.py original`. Skrypt
   prowadzi przez 8 pozycji, po 2 s każda.
2. Pico w trybie kalibracji: trzymaj GREEN + RED przy podłączaniu (dioda mruga
   2×/s, raport niesie surowe ADC). Potem `py -3.13 calib_accel.py pico`, te
   same pozycje.
3. `py -3.13 calib_accel.py fit` dopasowuje przelicznik dla każdego bajtu i
   zapisuje go w `profile.json` (sekcja `accel`).
4. `powershell -ExecutionPolicy Bypass -File firmware\build.ps1`

Bez kalibracji emulator wysyła w tych bajtach stałe wartości neutralne.

### Oryginalny fretboard zamiast przycisków (płytka z MC9S08JM60)

Na płytce z wylutowanym MCU membranowe progi zwierają ścieżkę do GND. Druga
strona ścieżki idzie przez 3-pinowy element „D…” (najpewniej podwójna dioda
ochronna ESD: sygnał na środkowej nóżce, skrajne do GND i VCC) do pinu MCU.
Pico podłącza się w miejsce MCU, czyli do **pól lutowniczych po MCU** (albo do
środkowej nóżki „D…”):

| Pad MCU (JM60) | Próg | Pico |
|---|---|---|
| PTF0 / TPM1CH2 | GREEN | GP2 (pin 4) |
| PTF1 / TPM1CH3 | RED | GP3 (pin 5) |
| PTF4 / TPM2CH0 | YELLOW | GP4 (pin 6) |
| PTF5 / TPM2CH1 | BLUE | GP5 (pin 7) |
| PTE0 / TxD1 | ORANGE | GP6 (pin 9) |
| VSS (masa) | | GND (pin 3 albo 8) |
| VDD (zasilanie MCU) | | 3V3 OUT (pin 36), patrz niżej |

Zasilanie szyny VDD płytki jest potrzebne. Bez niego pull-up Pico płynie przez
diodę ochronną do niezasilonej szyny, pin wisi w okolicy 1 V i próg może się
czytać jako „wciśnięty”. Na czas testu, gdy **kabel USB samej gitary jest
odłączony**:
1. Multimetrem sprawdź, że VDD–GND na płytce to nie zwarcie (ponad 100 Ω).
2. Połącz VDD płytki z **3V3 OUT Pico (pin 36)**. Wtedy żaden sygnał na płytce
   nie przekroczy 3,3 V.
3. **Nigdy nie podłączaj w tym układzie kabla USB gitary.** Jeśli VDD płytki
   wisi na VBUS, 5 V poszłoby wstecz do Pico.

Membrany z grafitem mają zwykle od kilkuset Ω do kilku kΩ. Przy pull-upie Pico
(ok. 50 kΩ) to daje wyraźne zero. Działa, jeśli dioda Pico gaśnie przy każdym
progu, a w `joy.cpl` zapalają się przyciski 1–5.

**Strum (czujnik optyczny, 4 przewody: VCC, GND, 2 sygnały).** Na później:
- VCC i GND czujnika muszą być zasilone. Sprawdź ciągłość: czy przewód VCC
  czujnika łączy się z VDD płytki (wtedy jest już zasilony z 3V3 Pico) czy z
  VBUS złącza USB. W tym drugim wypadku podaj na niego też 3V3 Pico.
- Zanim podłączysz sygnały do Pico, zmierz napięcie na każdym z nich w obu
  położeniach strum. **Nie może przekroczyć 3,3 V.**
- Sygnały do GP7 (UP) i GP8 (DOWN). Jeśli kierunki wyjdą odwrotnie, zamień
  przewody.
- Firmware sam ustala przy starcie stan spoczynkowy każdego sygnału strum
  (`STRUM_AUTO_POLARITY` w `pins.h`), więc działa czujnik o dowolnej logice i
  zwykły przycisk do GND. Warunek: nie ruszaj strum przy podłączaniu Pico. Wynik
  widać w logu jako `strum idle: up=H down=H`.

Pico wysyła stały raport neutralny dla osi (akcelerometr/tilt, X, Z, Rx) i
pozostałych przycisków. W spoczynku oryginał ma ustawione Button 7 i 9 (bajt 6 =
`0x14`), więc Pico też. Inne wejścia gitary (start/select, whammy, tilt)
skalibrujesz przez
`py -3.13 capture.py --inputs "GREEN,RED,YELLOW,BLUE,ORANGE,STRUM UP,STRUM DOWN,START,SELECT"`.
Podaj pełną listę, bo profil zapisuje tylko wejścia z bieżącego przebiegu. Potem
dopisz piny (`PIN_START` itd.) w `pins.h` i przebuduj.

## Windows i cache urządzeń

Windows zapamiętuje informacje o urządzeniu po VID/PID (i numerze seryjnym).
Jeśli Pico z tym samym VID/PID co oryginał zachowuje się dziwnie po zmianie
deskryptorów (stara nazwa, zła mapa przycisków, brak nowego interfejsu):

1. Menedżer urządzeń, **Widok, Pokaż ukryte urządzenia**.
2. Odinstaluj wszystkie wpisy tego urządzenia (także wyszarzone): „Urządzenie
   kompozytowe USB”, „Urządzenie wejściowe USB”, „Kontroler gier zgodny z HID”
   i interfejs „RT-GH LED”.
3. Wepnij Pico ponownie.

Windows pyta też o MS OS string descriptor (indeks 0xEE) **tylko przy pierwszym
podłączeniu danego VID/PID/bcdDevice** i zapamiętuje wynik w rejestrze. Żeby
nagrać to żądanie jeszcze raz (w `capture.py` albo `--verify`), usuń klucz jako
Administrator (VVVV = VID, PPPP = PID, RRRR = bcdDevice, hex):

```bash
reg delete "HKLM\SYSTEM\CurrentControlSet\Control\usbflags\VVVVPPPPRRRR" /f
```
