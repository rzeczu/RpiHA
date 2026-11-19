#pragma once

#ifdef __cplusplus
extern "C" {
#endif

#ifndef CFG_TUSB_MCU
#error CFG_TUSB_MCU musi być zdefiniowane (robi to Pico SDK przez tinyusb_device)
#endif

#ifndef CFG_TUSB_OS
#define CFG_TUSB_OS OPT_OS_PICO
#endif

#ifndef BOARD_TUD_RHPORT
#define BOARD_TUD_RHPORT 0
#endif

#define CFG_TUD_ENABLED 1

#ifndef CFG_TUSB_MEM_ALIGN
#define CFG_TUSB_MEM_ALIGN __attribute__((aligned(4)))
#endif

// Oryginał ma bMaxPacketSize0 = 8 (sprawdzane w main.c względem rtgh_profile.h)
#define CFG_TUD_ENDPOINT0_SIZE 8

#define CFG_TUD_HID    1
#define CFG_TUD_CDC    1   // tylko w trybie zrzutu logu (dump_mode.c); w trybie gitary nieużywane
#define CFG_TUD_CDC_RX_BUFSIZE 64
#define CFG_TUD_CDC_TX_BUFSIZE 1024
#define CFG_TUD_CDC_EP_BUFSIZE 64
#define CFG_TUD_MSC    0
#define CFG_TUD_MIDI   0
#define CFG_TUD_VENDOR 0   // interfejs "RT-GH LED" obsługuje własny sterownik w led_iface.c

#define CFG_TUD_HID_EP_BUFSIZE 16

// Proxy (rtgh_proxy): dodatkowo host na porcie PIO-USB (GP16 = D+, GP17 = D-) do oryginalnej gitary.
// Bez sterowników klas - endpointy gitary obsługuje proxy_main.c przez tuh_edpt_xfer().
#ifdef RTGH_PROXY
#define CFG_TUH_ENABLED 1
#define CFG_TUH_RPI_PIO_USB 1
#define BOARD_TUH_RHPORT 1
#define CFG_TUH_API_EDPT_XFER 1
#define CFG_TUH_ENUMERATION_BUFSIZE 256
#define CFG_TUH_HUB 0
#define CFG_TUH_DEVICE_MAX 1
#define CFG_TUH_HID 0
#define CFG_TUH_CDC 0
#define CFG_TUH_MSC 0
#define CFG_TUH_VENDOR 0
// Diagnostyka: komunikaty hosta TinyUSB (enumeracja gitary, błędy) trafiają do logu we flashu.
#undef CFG_TUSB_DEBUG   // Pico SDK podaje 0 w linii poleceń
#define CFG_TUSB_DEBUG 1
#define CFG_TUH_LOG_LEVEL 1
#define CFG_TUD_LOG_LEVEL 3   // > CFG_TUSB_DEBUG = urządzenie (strona automatu) bez logów
#define CFG_TUSB_DEBUG_PRINTF rtgh_tu_printf
#endif

#ifdef __cplusplus
}
#endif
