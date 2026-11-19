cmake_minimum_required(VERSION 3.13)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

# (Przy "Import Project" rozszerzenie Raspberry Pi Pico do VS Code wstawi tu swój blok konfiguracji.)

if (NOT DEFINED PICO_BOARD)
    set(PICO_BOARD pico CACHE STRING "Board type")   # pico / pico2 / pico_w
endif ()

include(pico_sdk_import.cmake)

project(rtgh_pico C CXX ASM)

pico_sdk_init()

# ---------------------------------------------------------------- emulator gitary (rtgh_pico.uf2)
add_executable(rtgh_pico
    src/main.c
    src/usb_descriptors.c
    src/led_iface.c
    src/usblog.c
    src/dump_mode.c
    src/settings.c
)

target_include_directories(rtgh_pico PRIVATE ${CMAKE_CURRENT_LIST_DIR}/src)

# ostrzeżenia tylko dla własnego kodu (nie dla SDK)
set_source_files_properties(src/main.c src/usb_descriptors.c src/led_iface.c src/usblog.c src/dump_mode.c
    src/proxy_main.c src/settings.c
    PROPERTIES COMPILE_OPTIONS "-Wall;-Wextra")

target_link_libraries(rtgh_pico PRIVATE pico_stdlib tinyusb_device hardware_flash hardware_adc)

# log na UART0 (GP0 TX); USB zajmuje TinyUSB, więc stdio po USB wyłączone
pico_enable_stdio_uart(rtgh_pico 1)
pico_enable_stdio_usb(rtgh_pico 0)

pico_add_extra_outputs(rtgh_pico)   # rtgh_pico.uf2

# ---------------------------------------------------------------- proxy (rtgh_proxy.uf2, tylko RP2040)
# Automat <-> Pico (natywny USB) <-> oryginalna gitara (PIO-USB na GP16/GP17).
# Wymaga biblioteki Pico-PIO-USB w lib/tinyusb/hw/mcu/raspberry_pi/Pico-PIO-USB.
set(PIO_USB_DIR ${PICO_SDK_PATH}/lib/tinyusb/hw/mcu/raspberry_pi/Pico-PIO-USB)
if (PICO_PLATFORM STREQUAL "rp2040" AND EXISTS ${PIO_USB_DIR}/src/pio_usb.c)
    add_executable(rtgh_proxy
        src/proxy_main.c
        src/usb_descriptors.c
        src/led_iface.c
        src/usblog.c
        src/dump_mode.c
        ${PIO_USB_DIR}/src/pio_usb.c
        ${PIO_USB_DIR}/src/pio_usb_host.c
        ${PIO_USB_DIR}/src/usb_crc.c
        ${PICO_SDK_PATH}/lib/tinyusb/src/portable/raspberrypi/pio_usb/hcd_pio_usb.c
    )
    target_include_directories(rtgh_proxy PRIVATE ${CMAKE_CURRENT_LIST_DIR}/src ${PIO_USB_DIR}/src)
    target_compile_definitions(rtgh_proxy PRIVATE RTGH_PROXY=1 PIO_USB_USE_TINYUSB)
    pico_generate_pio_header(rtgh_proxy ${PIO_USB_DIR}/src/usb_tx.pio)
    pico_generate_pio_header(rtgh_proxy ${PIO_USB_DIR}/src/usb_rx.pio)
    target_link_libraries(rtgh_proxy PRIVATE pico_stdlib pico_multicore pico_util tinyusb_device tinyusb_host
        hardware_flash hardware_pio hardware_dma)
    pico_set_binary_type(rtgh_proxy copy_to_ram)   # zapis logu do flasha nie zatrzymuje hosta na rdzeniu 1
    pico_enable_stdio_uart(rtgh_proxy 1)
    pico_enable_stdio_usb(rtgh_proxy 0)
    pico_add_extra_outputs(rtgh_proxy)
endif ()
