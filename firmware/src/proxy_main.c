// RT-GH proxy: Pico między automatem a ORYGINALNĄ gitarą, zapisuje całą rozmowę do flasha.
//
//   automat --USB (natywny port Pico, rdzeń 0)--> Pico --PIO-USB (GP16 D+, GP17 D-, rdzeń 1)--> gitara
//
// - Do automatu Pico zgłasza się deskryptorami oryginału (jak emulator).
// - Raporty HID gitary (EP 0x81) idą do automatu bez zmian: grasz oryginalną gitarą.
// - Interfejs "RT-GH LED": EP 0x03 (automat -> gitara) i EP 0x82/0x84 (gitara -> automat) przekazywane
//   w obie strony; żądania vendor/class do interfejsu 1 i urządzenia też trafiają do gitary.
// - Log jak w emulatorze + rekordy G_IN / G_CTRL / G_HID / PSTATS; odczyt: zworka GP9-GND -> COM.
// Cały program działa z RAM (copy_to_ram), bo zapis flasha na rdzeniu 0 nie może zatrzymać hosta USB.
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "pico/multicore.h"
#include "pico/util/queue.h"
#include "hardware/structs/usb.h"
#include "hardware/clocks.h"
#include "tusb.h"
#include "device/dcd.h"
#include "device/usbd_pvt.h"
#include "host/hcd.h"
#include "host/usbh_pvt.h"
#include "pio_usb.h"
#include "rtgh_profile.h"
#include "usblog.h"
#include "dump_mode.h"
#include "led_iface.h"

#define PIO_USB_DP_PIN 16  // D+ = GP16 (pin 21), D- = GP17 (pin 22)

// M_POLL: automat odpytał EP 0x82/0x84 Pico (NAK) -> jeden odczyt tego EP z gitary.
// Gitara sama z siebie nie znosi odpytywania 0x82/0x84 (brak odpowiedzi, potem pada też HID),
// więc proxy pyta ją o te EP tylko wtedy, gdy robi to automat.
enum { M_OUT = 1, M_CTRL, M_IN, M_HID, M_CTRL_DONE, M_MOUNT, M_UMOUNT, M_ERR, M_POLL };

typedef struct {
    uint8_t kind, ep, result, len;
    uint32_t seq;
    tusb_control_request_t setup;
    uint8_t data[64];
} pmsg_t;

static queue_t q_host;  // rdzeń 0 -> rdzeń 1 (do gitary)
static queue_t q_dev;   // rdzeń 1 -> rdzeń 0 (od gitary)

// ---------------------------------------------------------------------  diagnostyka do logu
// printf TinyUSB (CFG_TUSB_DEBUG_PRINTF) -> linie tekstu w logu. Limit linii, bo po podłączeniu
// gitary host loguje każdy transfer.
#define TU_LOG_MAX_LINES 400
static volatile uint32_t tu_lines;

int rtgh_tu_printf(const char *fmt, ...) {
    static char line[2][80];
    static uint8_t pos[2];
    if (tu_lines >= TU_LOG_MAX_LINES) return 0;
    char tmp[128];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(tmp, sizeof(tmp), fmt, ap);
    va_end(ap);
    if (n > (int)sizeof(tmp) - 1) n = sizeof(tmp) - 1;
    uint core = get_core_num();
    for (int i = 0; i < n; i++) {
        char c = tmp[i];
        if (c == '\r') continue;
        if (c != '\n') line[core][pos[core]++] = c;
        if (c == '\n' || pos[core] >= sizeof(line[0]) - 4) {
            if (pos[core]) {
                char rec[80];
                int k = snprintf(rec, sizeof(rec), "c%u %.*s", core, pos[core], line[core]);
                usblog_add(LOG_TEXT, rec, (uint8_t)(k < 79 ? k : 79));
                tu_lines++;
            }
            pos[core] = 0;
        }
    }
    return n;
}

static void log_textf(const char *fmt, ...) {
    char t[80];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(t, sizeof(t), fmt, ap);
    va_end(ap);
    usblog_add(LOG_TEXT, t, (uint8_t)(n < 79 ? n : 79));
}

// Stan linii D+/D- (GP16/GP17). Podłączona i zasilona gitara full-speed ciągnie D+ do 3,3 V
// (rezystor 1,5 kΩ), więc w spoczynku powinno być D+=1 D-=0. Oba 0 = brak zasilania / przerwa.
static uint8_t lines_last = 0xFF;
static uint32_t lines_logged;
static void log_lines(const char *when) {
    uint8_t l = (uint8_t)(gpio_get(PIO_USB_DP_PIN) | (gpio_get(PIO_USB_DP_PIN + 1) << 1));
    if (l == lines_last) return;
    lines_last = l;
    lines_logged++;
    log_textf("lines %s: D+(GP%u)=%u D-(GP%u)=%u", when, PIO_USB_DP_PIN, l & 1, PIO_USB_DP_PIN + 1, l >> 1);
}

// =====================================================================  rdzeń 1: host (gitara)

static uint8_t g_daddr;           // adres gitary, 0 = brak
static uint8_t g_hid_ep, g_out_ep, g_in_ep[2];
static uint8_t g_hid_buf[64], g_in_buf[2][64], g_out_buf[64], g_ctrl_buf[64];
static uint32_t g_retry_at[3];    // ponowne uzbrojenie IN po błędzie (0 = nie trzeba): hid, in0, in1
static bool g_out_busy, g_ctrl_busy;
static pmsg_t g_out_fifo[8];
static uint8_t g_out_head, g_out_count;
static pmsg_t g_ctrl_pend;        // żądanie kontrolne czekające na wolny EP0
static bool g_ctrl_has_pend;
static pmsg_t g_ctrl_cur;         // żądanie w toku

static void post_dev(pmsg_t const *m) {
    (void)queue_try_add(&q_dev, m);  // pełna kolejka = zgubione (widać w statystykach rdzenia 0)
}

static void post_err(uint8_t ep, uint8_t result) {
    pmsg_t m = {.kind = M_ERR, .ep = ep, .result = result};
    post_dev(&m);
}

static int slot_of(uint8_t ep) {
    if (ep == g_hid_ep) return 0;
    if (ep == g_in_ep[0]) return 1;
    if (ep == g_in_ep[1]) return 2;
    return -1;
}

static uint8_t *buf_of(int slot) { return slot == 0 ? g_hid_buf : g_in_buf[slot - 1]; }

static void in_cb(tuh_xfer_t *x);

static void arm_in(int slot) {
    uint8_t ep = slot == 0 ? g_hid_ep : g_in_ep[slot - 1];
    if (!g_daddr || !ep) return;
    tuh_xfer_t x = {
        .daddr = g_daddr, .ep_addr = ep, .buflen = 64, .buffer = buf_of(slot), .complete_cb = in_cb, .user_data = 0};
    if (!tuh_edpt_xfer(&x)) {
        g_retry_at[slot] = to_ms_since_boot(get_absolute_time()) + 20;
    }
}

static void in_cb(tuh_xfer_t *x) {
    int slot = slot_of(x->ep_addr);
    if (slot < 0 || x->daddr != g_daddr) return;
    if (x->result == XFER_RESULT_SUCCESS) {
        pmsg_t m = {.kind = slot == 0 ? M_HID : M_IN, .ep = x->ep_addr};
        m.len = (uint8_t)(x->actual_len < 64 ? x->actual_len : 64);
        memcpy(m.data, buf_of(slot), m.len);
        post_dev(&m);
        if (slot == 0) arm_in(slot);  // HID odpytujemy stale (jak każdy host); 0x82/0x84 tylko na M_POLL
    } else {
        post_err(x->ep_addr, x->result);
        if (slot == 0) g_retry_at[slot] = to_ms_since_boot(get_absolute_time()) + 50;
    }
}

static void out_cb(tuh_xfer_t *x) {
    g_out_busy = false;
    if (x->result != XFER_RESULT_SUCCESS) post_err(x->ep_addr, x->result);
}

static void try_send_out(void) {
    if (!g_daddr || !g_out_ep || g_out_busy || g_out_count == 0) return;
    pmsg_t *m = &g_out_fifo[g_out_head];
    memcpy(g_out_buf, m->data, m->len);
    tuh_xfer_t x = {
        .daddr = g_daddr, .ep_addr = g_out_ep, .buflen = m->len, .buffer = g_out_buf, .complete_cb = out_cb};
    if (tuh_edpt_xfer(&x)) {
        g_out_busy = true;
        g_out_head = (uint8_t)((g_out_head + 1) % 8);
        g_out_count--;
    }
}

static void ctrl_done_cb(tuh_xfer_t *x) {
    pmsg_t m = g_ctrl_cur;
    m.kind = M_CTRL_DONE;
    m.result = x->result;
    if (m.setup.bmRequestType_bit.direction == TUSB_DIR_IN) {
        m.len = (uint8_t)(x->actual_len < 64 ? x->actual_len : 64);
        memcpy(m.data, g_ctrl_buf, m.len);
    }
    post_dev(&m);
    g_ctrl_busy = false;
}

static void try_send_ctrl(void) {
    if (!g_ctrl_has_pend || g_ctrl_busy) return;
    if (!g_daddr) {
        g_ctrl_pend.kind = M_CTRL_DONE;
        g_ctrl_pend.result = XFER_RESULT_FAILED;
        g_ctrl_pend.len = 0;
        post_dev(&g_ctrl_pend);
        g_ctrl_has_pend = false;
        return;
    }
    g_ctrl_cur = g_ctrl_pend;
    uint16_t n = g_ctrl_cur.setup.wLength < 64 ? g_ctrl_cur.setup.wLength : 64;
    g_ctrl_cur.setup.wLength = n;  // większych nie obsługujemy (ten sprzęt ma EP0 = 8 B, żądania są krótkie)
    if (g_ctrl_cur.setup.bmRequestType_bit.direction == TUSB_DIR_OUT) memcpy(g_ctrl_buf, g_ctrl_cur.data, n);
    tuh_xfer_t x = {.daddr = g_daddr, .ep_addr = 0, .setup = &g_ctrl_cur.setup, .buffer = g_ctrl_buf,
                    .complete_cb = ctrl_done_cb, .user_data = 0};
    if (tuh_control_xfer(&x)) {
        g_ctrl_busy = true;
        g_ctrl_has_pend = false;
    }
}

void tuh_mount_cb(uint8_t daddr) {
    uint16_t vid = 0, pid = 0;
    tuh_vid_pid_get(daddr, &vid, &pid);
    g_daddr = daddr;
    g_hid_ep = g_out_ep = g_in_ep[0] = g_in_ep[1] = 0;
    g_out_busy = g_ctrl_busy = false;
    // Endpointy z nagranego deskryptora oryginału (ten sam sprzęt)
    uint8_t const *p = rtgh_desc_config, *end = rtgh_desc_config + sizeof(rtgh_desc_config);
    uint8_t itf = 0, n_in = 0;
    while (p < end && p[0]) {
        if (p[1] == TUSB_DESC_INTERFACE) itf = p[2];
        if (p[1] == TUSB_DESC_ENDPOINT) {
            tusb_desc_endpoint_t const *ep = (tusb_desc_endpoint_t const *)p;
            if (tuh_edpt_open(daddr, ep)) {
                uint8_t a = ep->bEndpointAddress;
                if (tu_edpt_dir(a) == TUSB_DIR_OUT) g_out_ep = a;
                else if (itf == 0 && !g_hid_ep) g_hid_ep = a;
                else if (n_in < 2) g_in_ep[n_in++] = a;
            } else {
                post_err(ep->bEndpointAddress, 0xEE);
            }
        }
        p += p[0];
    }
    pmsg_t m = {.kind = M_MOUNT, .len = 4};
    memcpy(m.data, &vid, 2);
    memcpy(m.data + 2, &pid, 2);
    post_dev(&m);
    for (int s = 0; s < 3; s++) g_retry_at[s] = 0;
    arm_in(0);  // tylko HID; 0x82/0x84 na żądanie automatu
    tu_lines = TU_LOG_MAX_LINES;  // enumeracja zapisana; dalsze komunikaty TinyUSB to szum o transferach
}

void tuh_umount_cb(uint8_t daddr) {
    if (daddr != g_daddr) return;
    g_daddr = 0;
    g_out_busy = false;
    if (g_ctrl_busy) {
        g_ctrl_busy = false;
        pmsg_t m = g_ctrl_cur;
        m.kind = M_CTRL_DONE;
        m.result = XFER_RESULT_FAILED;
        m.len = 0;
        post_dev(&m);
    }
    pmsg_t m = {.kind = M_UMOUNT};
    post_dev(&m);
}

static void core1_main(void) {
    log_textf("core1 start, sys %lu kHz", (unsigned long)(clock_get_hz(clk_sys) / 1000));
    // stan linii przed startem hosta (pull-down jak w hoście USB)
    for (uint p = PIO_USB_DP_PIN; p <= PIO_USB_DP_PIN + 1u; p++) {
        gpio_init(p);
        gpio_set_dir(p, GPIO_IN);
        gpio_pull_down(p);
    }
    sleep_ms(10);
    log_lines("before host init");
    pio_usb_configuration_t cfg = PIO_USB_DEFAULT_CONFIG;
    cfg.pin_dp = PIO_USB_DP_PIN;
    bool ok = tuh_configure(BOARD_TUH_RHPORT, TUH_CFGID_RPI_PIO_USB_CONFIGURATION, &cfg);
    ok = tuh_init(BOARD_TUH_RHPORT) && ok;
    log_textf("host init %s (PIO-USB D+ GP%u, D- GP%u)", ok ? "ok" : "FAILED", PIO_USB_DP_PIN, PIO_USB_DP_PIN + 1);
    uint32_t next_lines = 0, alive_at = to_ms_since_boot(get_absolute_time()) + 5000;

    while (true) {
        tuh_task();
        uint32_t t = to_ms_since_boot(get_absolute_time());
        bool conn = hcd_port_connect_status(BOARD_TUH_RHPORT);
        // linie tylko bez urządzenia (potem host nadaje SOF i stan linii skacze); najwyżej 40 wpisów
        if (!conn && lines_logged < 40 && (int32_t)(t - next_lines) >= 0) {
            next_lines = t + 20;
            log_lines("idle");
        }
        if ((int32_t)(t - alive_at) >= 0) {  // czy rdzeń 1 żyje i czy gitara jest
            alive_at = t + (g_daddr ? 60000 : 5000);
            log_textf("core1 alive, guitar %s, port connect %u", g_daddr ? "mounted" : "none", (unsigned)conn);
        }
        pmsg_t m;
        while (queue_try_remove(&q_host, &m)) {
            if (m.kind == M_OUT) {
                if (g_out_count == 8) {  // pełno: wyrzuć najstarszy
                    g_out_head = (uint8_t)((g_out_head + 1) % 8);
                    g_out_count--;
                }
                g_out_fifo[(g_out_head + g_out_count) % 8] = m;
                g_out_count++;
            } else if (m.kind == M_POLL) {
                int s = slot_of(m.ep);
                if (s > 0 && !g_daddr) {  // brak gitary - zwolnij żądanie po stronie rdzenia 0
                    pmsg_t f = {.kind = M_ERR, .ep = m.ep, .result = 0xFD};
                    post_dev(&f);
                } else if (s > 0 && !usbh_edpt_busy(g_daddr, m.ep)) {
                    arm_in(s);  // jeden odczyt; jeśli gitara NAK-uje, czeka aż coś wyśle
                }
            } else if (m.kind == M_CTRL) {
                if (g_ctrl_has_pend) {  // starsze czekające zastępujemy - odpowiedz porażką
                    pmsg_t f = g_ctrl_pend;
                    f.kind = M_CTRL_DONE;
                    f.result = XFER_RESULT_FAILED;
                    f.len = 0;
                    post_dev(&f);
                }
                g_ctrl_pend = m;
                g_ctrl_has_pend = true;
            }
        }
        try_send_out();
        try_send_ctrl();
        uint32_t now = to_ms_since_boot(get_absolute_time());
        for (int s = 0; s < 3; s++) {
            if (g_retry_at[s] && (int32_t)(now - g_retry_at[s]) >= 0) {
                g_retry_at[s] = 0;
                arm_in(s);
            }
        }
    }
}

// =====================================================================  rdzeń 0: urządzenie (automat)

typedef struct {
    uint8_t len;
    uint8_t data[64];
} pkt_t;

static uint8_t latest_hid[RTGH_REPORT_LEN];
static uint8_t hid_logged[RTGH_REPORT_LEN];
static uint32_t hid_logged_ms;
static bool guitar_present;
static pkt_t in_fifo[2][8];
static uint8_t in_head[2], in_count[2];
static pkt_t in_last_logged[2];
static bool in_logged_any[2];
static rtgh_pstats_t pst, pst_logged;
static rtgh_stats_t stats, stats_logged;
static uint32_t traffic_ms;

static volatile uint32_t setup_seq;
static bool ctrl_pend;
static uint32_t ctrl_pend_seq;
static uint8_t ctrl_rhport;
static tusb_control_request_t ctrl_req;
static uint8_t ctrl_reply[64];

static int in_slot(uint8_t ep) {
    if (ep == 0x82) return 0;
    if (ep == 0x84) return 1;
    return -1;
}

static bool poll_req[2];        // odczyt 0x82/0x84 z gitary zlecony, czeka na wynik
static uint8_t err_logged[16];  // ile błędów danego EP (numer) zapisano w tej sesji gitary

// Automat odpytuje EP2/EP4 IN Pico, a my nie mamy danych -> NAK -> zleć odczyt z gitary.
static void poll_cabinet_naks(void) {
    uint32_t st = usb_hw->ep_nak_stall_status;
    if (!st) return;
    usb_hw->ep_nak_stall_status = st;  // W1C
    for (int n = 0; n < 8; n++)
        if (st & (1u << (2 * n))) stats.nak_in[n]++;
    static uint8_t const eps[2] = {0x82, 0x84};
    for (int s = 0; s < 2; s++) {
        uint8_t num = eps[s] & 0x0F;
        if (!(st & (1u << (2 * num))) || !guitar_present || poll_req[s] || in_count[s]) continue;
        pmsg_t m = {.kind = M_POLL, .ep = eps[s]};
        if (queue_try_add(&q_host, &m)) poll_req[s] = true;
    }
}

void tud_event_hook_cb(uint8_t rhport, uint32_t eventid, bool in_isr) {
    (void)rhport;
    (void)in_isr;
    switch (eventid) {
        case DCD_EVENT_BUS_RESET: usblog_add(LOG_BUS_RESET, NULL, 0); break;
        case DCD_EVENT_UNPLUGGED: usblog_add(LOG_UNPLUGGED, NULL, 0); break;
        case DCD_EVENT_SUSPEND: usblog_add(LOG_SUSPEND, NULL, 0); break;
        case DCD_EVENT_RESUME: usblog_add(LOG_RESUME, NULL, 0); break;
        case DCD_EVENT_SETUP_RECEIVED: {
            uint8_t s[8];
            for (int i = 0; i < 8; i++) s[i] = usb_dpram->setup_packet[i];
            usblog_add(LOG_SETUP, s, 8);
            setup_seq++;
            break;
        }
        default: break;
    }
}

// --- haki interfejsu LED (led_iface.c)

void rtgh_led_out_hook(uint8_t const *d, uint16_t n) {
    traffic_ms = to_ms_since_boot(get_absolute_time());
    pmsg_t m = {.kind = M_OUT, .ep = 0x03, .len = (uint8_t)(n < 64 ? n : 64)};
    memcpy(m.data, d, m.len);
    if (guitar_present && queue_try_add(&q_host, &m)) pst.out_fwd++;
    else pst.out_drop++;
}

void rtgh_led_in_done_hook(uint8_t ep) {
    int s = in_slot(ep);
    if (s >= 0) pst.in_sent[s]++;
}

static void forward_ctrl(tusb_control_request_t const *req, uint8_t const *data, uint16_t n) {
    pmsg_t m = {.kind = M_CTRL, .setup = *req, .seq = setup_seq, .len = (uint8_t)(n < 64 ? n : 64)};
    if (n) memcpy(m.data, data, m.len);
    if (guitar_present && queue_try_add(&q_host, &m)) pst.ctrl_fwd++;
}

int rtgh_ctrl_hook(uint8_t rhport, uint8_t stage, tusb_control_request_t const *req, uint8_t *buf, uint16_t buflen) {
    if (stage == CONTROL_STAGE_SETUP) {
        if (req->bmRequestType_bit.direction == TUSB_DIR_IN) {
            if (!guitar_present) return 0;  // STALL
            pmsg_t m = {.kind = M_CTRL, .setup = *req, .seq = setup_seq};
            if (!queue_try_add(&q_host, &m)) return 0;
            pst.ctrl_fwd++;
            // odpowiedź wyślemy, gdy przyjdzie od gitary (host w tym czasie dostaje NAK)
            ctrl_pend = true;
            ctrl_pend_seq = setup_seq;
            ctrl_rhport = rhport;
            ctrl_req = *req;
            return 1;
        }
        if (req->wLength == 0) {
            forward_ctrl(req, NULL, 0);
            return tud_control_xfer(rhport, req, NULL, 0) ? 1 : 0;
        }
        return tud_control_xfer(rhport, req, buf, tu_min16(req->wLength, buflen)) ? 1 : 0;
    }
    if (stage == CONTROL_STAGE_DATA && req->bmRequestType_bit.direction == TUSB_DIR_OUT) {
        uint16_t n = tu_min16(req->wLength, buflen);
        usblog_add(LOG_CTRL_OUT_DATA, buf, (uint8_t)n);
        forward_ctrl(req, buf, n);
    }
    return 1;
}

static void ctrl_reply_from_guitar(pmsg_t const *m) {
    // log: [setup 8][wynik][dane]
    uint8_t l[8 + 1 + 64];
    memcpy(l, &m->setup, 8);
    l[8] = m->result;
    memcpy(l + 9, m->data, m->len);
    usblog_add(LOG_G_CTRL, l, (uint8_t)(9 + m->len));
    if (m->setup.bmRequestType_bit.direction != TUSB_DIR_IN) return;
    if (!ctrl_pend || m->seq != ctrl_pend_seq || setup_seq != ctrl_pend_seq) return;  // host już porzucił
    ctrl_pend = false;
    if (m->result == XFER_RESULT_SUCCESS) {
        memcpy(ctrl_reply, m->data, m->len);
        tud_control_xfer(ctrl_rhport, &ctrl_req, ctrl_reply, m->len);
    } else {
        dcd_edpt_stall(ctrl_rhport, 0x00);
        dcd_edpt_stall(ctrl_rhport, 0x80);
    }
}

// --- HID: raport gitary do automatu

uint16_t tud_hid_get_report_cb(uint8_t instance, uint8_t report_id, hid_report_type_t report_type,
                               uint8_t *buffer, uint16_t reqlen) {
    (void)instance;
    uint8_t l[2] = {report_id, (uint8_t)report_type};
    usblog_add(LOG_HID_GET_REPORT, l, 2);
    if (report_type != HID_REPORT_TYPE_INPUT) return 0;
    uint16_t n = reqlen < RTGH_REPORT_LEN ? reqlen : RTGH_REPORT_LEN;
    memcpy(buffer, latest_hid, n);
    return n;
}

void tud_hid_set_report_cb(uint8_t instance, uint8_t report_id, hid_report_type_t report_type,
                           uint8_t const *buffer, uint16_t bufsize) {
    (void)instance;
    uint8_t l[2 + 62];
    uint16_t n = bufsize < 62 ? bufsize : 62;
    l[0] = report_id;
    l[1] = (uint8_t)report_type;
    memcpy(l + 2, buffer, n);
    usblog_add(LOG_HID_SET_REPORT, l, (uint8_t)(2 + n));
    tusb_control_request_t req = {
        .bmRequestType = 0x21, .bRequest = 0x09, .wValue = (uint16_t)((report_type << 8) | report_id),
        .wIndex = 0, .wLength = n};
    forward_ctrl(&req, buffer, n);
}

void tud_mount_cb(void) { usblog_add(LOG_MOUNT, NULL, 0); }
void tud_umount_cb(void) { usblog_add(LOG_UNMOUNT, NULL, 0); }

static void handle_from_guitar(pmsg_t const *m) {
    uint32_t now = to_ms_since_boot(get_absolute_time());
    switch (m->kind) {
        case M_MOUNT: {
            uint16_t vid, pid;
            memcpy(&vid, m->data, 2);
            memcpy(&pid, m->data + 2, 2);
            char t[48];
            snprintf(t, sizeof(t), "guitar mounted %04x:%04x", vid, pid);
            usblog_text(t);
            printf("%s\n", t);
            guitar_present = true;
            in_logged_any[0] = in_logged_any[1] = false;
            poll_req[0] = poll_req[1] = false;
            memset(err_logged, 0, sizeof(err_logged));
            break;
        }
        case M_UMOUNT:
            usblog_text("guitar unmounted");
            guitar_present = false;
            poll_req[0] = poll_req[1] = false;
            memcpy(latest_hid, rtgh_neutral_report, RTGH_REPORT_LEN);
            break;
        case M_HID: {
            pst.g_hid++;
            uint8_t n = m->len < RTGH_REPORT_LEN ? m->len : RTGH_REPORT_LEN;
            memcpy(latest_hid, m->data, n);
            // log zmian poza szumiącymi osiami (bajt 1 = Y, bajt 4 = Ry), najwyżej co 30 ms
            bool diff = false;
            for (int i = 0; i < RTGH_REPORT_LEN; i++)
                if (i != 1 && i != 4 && latest_hid[i] != hid_logged[i]) diff = true;
            if (diff && now - hid_logged_ms >= 30) {
                usblog_add(LOG_G_HID, latest_hid, RTGH_REPORT_LEN);
                memcpy(hid_logged, latest_hid, RTGH_REPORT_LEN);
                hid_logged_ms = now;
            }
            break;
        }
        case M_IN: {
            int s = in_slot(m->ep);
            if (s < 0) break;
            poll_req[s] = false;
            pst.g_in[s]++;
            traffic_ms = now;
            if (!in_logged_any[s] || in_last_logged[s].len != m->len ||
                memcmp(in_last_logged[s].data, m->data, m->len) != 0) {
                uint8_t l[1 + 64];
                l[0] = m->ep;
                memcpy(l + 1, m->data, m->len);
                usblog_add(LOG_G_IN, l, (uint8_t)(1 + m->len));
                in_last_logged[s].len = m->len;
                memcpy(in_last_logged[s].data, m->data, m->len);
                in_logged_any[s] = true;
            }
            if (in_count[s] == 8) {  // automat nie odbiera: wyrzuć najstarszy
                in_head[s] = (uint8_t)((in_head[s] + 1) % 8);
                in_count[s]--;
                pst.in_drop[s]++;
            }
            pkt_t *p = &in_fifo[s][(in_head[s] + in_count[s]) % 8];
            p->len = m->len;
            memcpy(p->data, m->data, m->len);
            in_count[s]++;
            break;
        }
        case M_CTRL_DONE: ctrl_reply_from_guitar(m); break;
        case M_ERR: {
            int s = in_slot(m->ep);
            if (s >= 0) poll_req[s] = false;
            if (m->result == 0xFD) break;  // tylko zwolnienie żądania
            pst.g_err++;
            uint8_t n = m->ep & 0x0F;
            if (err_logged[n] < 5) {  // pierwsze błędy z treścią, dalej tylko licznik w PROXY
                err_logged[n]++;
                char t[72];
                snprintf(t, sizeof(t), "guitar xfer err ep %02x res %u%s", m->ep, m->result,
                         err_logged[n] == 5 ? " (dalej tylko licznik)" : "");
                usblog_text(t);
            }
            break;
        }
        default: break;
    }
}

static void feed_in_endpoints(void) {
    static uint8_t const eps[2] = {0x82, 0x84};
    for (int s = 0; s < 2; s++) {
        if (in_count[s] == 0 || led_iface_in_busy(eps[s])) continue;
        pkt_t *p = &in_fifo[s][in_head[s]];
        if (led_iface_in_send(eps[s], p->data, p->len)) {
            in_head[s] = (uint8_t)((in_head[s] + 1) % 8);
            in_count[s]--;
        }
    }
}

int main(void) {
    set_sys_clock_khz(120000, true);  // PIO-USB wymaga wielokrotności 12 MHz
    stdio_init_all();
#ifdef PICO_DEFAULT_LED_PIN
    gpio_init(PICO_DEFAULT_LED_PIN);
    gpio_set_dir(PICO_DEFAULT_LED_PIN, GPIO_OUT);
#endif
    gpio_init(PIN_DUMP);
    gpio_set_dir(PIN_DUMP, GPIO_IN);
    gpio_pull_up(PIN_DUMP);
    sleep_ms(2);
    bool gp9 = gpio_get(PIN_DUMP);
    if (dump_requested_at_boot() || !gp9) {
        rtgh_dump_mode = true;
        dump_mode_run();
    }

    memcpy(latest_hid, rtgh_neutral_report, RTGH_REPORT_LEN);
    memcpy(hid_logged, rtgh_neutral_report, RTGH_REPORT_LEN);
    usblog_init();
    usblog_text("boot rtgh_proxy " __DATE__ " " __TIME__);
    log_textf("GP9 (zworka zrzutu) = %u", (unsigned)gp9);
    printf("\nRT-GH proxy: gitara na GP16/GP17 (PIO-USB), automat na porcie USB Pico\n");

    queue_init(&q_host, sizeof(pmsg_t), 32);
    queue_init(&q_dev, sizeof(pmsg_t), 32);
    multicore_reset_core1();
    multicore_launch_core1(core1_main);

    tud_init(BOARD_TUD_RHPORT);

    absolute_time_t next_stats = make_timeout_time_ms(2000);
    while (true) {
        tud_task();
        pmsg_t m;
        while (queue_try_remove(&q_dev, &m)) handle_from_guitar(&m);
        poll_cabinet_naks();
        feed_in_endpoints();

        // Raport HID w każdym odpytaniu hosta (jak oryginał); bez gitary - neutralny.
        if (tud_mounted() && tud_hid_ready()) {
            if (tud_hid_report(0, latest_hid, RTGH_REPORT_LEN)) stats.hid_reports++;
        }

        if (time_reached(next_stats)) {
            next_stats = make_timeout_time_ms(2000);
            stats.led_out = rtgh_led_packets;
            stats.dropped = usblog_dropped();
            if (memcmp(&stats, &stats_logged, sizeof(stats)) != 0) {
                usblog_add(LOG_STATS, &stats, sizeof(stats));
                stats_logged = stats;
            }
            if (memcmp(&pst, &pst_logged, sizeof(pst)) != 0) {
                usblog_add(LOG_PSTATS, &pst, sizeof(pst));
                pst_logged = pst;
            }
        }
        usblog_task();
        dump_bootsel_poll();

#ifdef PICO_DEFAULT_LED_PIN
        uint32_t now = to_ms_since_boot(get_absolute_time());
        bool on;
        if (!tud_mounted()) on = (now / 500) & 1;            // wolno: automat/PC nie skonfigurował
        else if (!guitar_present) on = (now / 125) & 1;      // szybko: brak gitary na GP16/17
        else on = true;                                      // świeci: wszystko połączone
        if (now - traffic_ms < 40) on = !on;                 // mrugnięcie: ruch na interfejsie LED
        if (usblog_full()) on = (now / 50) & 1;
        gpio_put(PICO_DEFAULT_LED_PIN, on);
#endif
    }
}
