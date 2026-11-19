#pragma once
// Przypisanie wejść do pinów GPIO (numery GPxx z nadruku na Pico).
// Każdy pin ma wewnętrzny pull-up: przycisk zwiera pin do GND (dowolny pin GND).
// Zmień tu i przebuduj. Nie używaj GP0/GP1 (UART z logiem) ani GP23-25 (zasilanie/LED na Pico).

#define PIN_GREEN       2   // fizyczny pin 4
#define PIN_RED         3   // fizyczny pin 5
#define PIN_YELLOW      4   // fizyczny pin 6
#define PIN_BLUE        5   // fizyczny pin 7
#define PIN_ORANGE      6   // fizyczny pin 9
#define PIN_STRUM_UP    7   // fizyczny pin 10
#define PIN_STRUM_DOWN  8   // fizyczny pin 11
// GND najbliżej: fizyczny pin 3 albo 8

// Zwarty do GND przy podłączaniu (zworka fizyczny pin 12 <-> pin 13 GND) = tryb zrzutu logu (port COM).
#define PIN_DUMP        9

// Strum: 1 = poziom spoczynkowy odczytany przy starcie (działa dla przycisku do GND i dla czujnika
// optycznego dowolnej polaryzacji - nie trzymaj strum przy podłączaniu). 0 = zawsze aktywny stan niski.
#define STRUM_AUTO_POLARITY 1

#define DEBOUNCE_MS     5   // tyle ms stan musi być stabilny

// Akcelerometr MMA7260 (wyjścia analogowe za rezystorami 1 kΩ): pin 13 ZOUT -> GP26 (ADC0),
// pin 14 YOUT -> GP27 (ADC1), pin 15 XOUT -> GP28 (ADC2). Przeliczenie na bajty raportu
// z kalibracji (calib_accel.py -> profile.json -> rtgh_profile.h).
// Tryb kalibracji (surowe ADC w raporcie): trzymaj GREEN + RED przy podłączaniu Pico.
#define ACCEL_ADC_FIRST_GPIO 26
