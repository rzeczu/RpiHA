# Uproszczona wersja <PICO_SDK_PATH>/external/pico_sdk_import.cmake.
# Ścieżkę do SDK ustawia rozszerzenie VS Code (blok w CMakeLists.txt) albo zmienna PICO_SDK_PATH.

if (DEFINED ENV{PICO_SDK_PATH} AND (NOT PICO_SDK_PATH))
    set(PICO_SDK_PATH $ENV{PICO_SDK_PATH})
    message("Using PICO_SDK_PATH from environment ('${PICO_SDK_PATH}')")
endif ()

set(PICO_SDK_PATH "${PICO_SDK_PATH}" CACHE PATH "Path to the Raspberry Pi Pico SDK")

if (NOT PICO_SDK_PATH)
    message(FATAL_ERROR "Nie znam ścieżki do Pico SDK. Zaimportuj projekt rozszerzeniem Raspberry Pi Pico "
            "do VS Code albo ustaw PICO_SDK_PATH.")
endif ()

get_filename_component(PICO_SDK_PATH "${PICO_SDK_PATH}" REALPATH BASE_DIR "${CMAKE_BINARY_DIR}")
if (NOT EXISTS ${PICO_SDK_PATH}/pico_sdk_init.cmake)
    message(FATAL_ERROR "W '${PICO_SDK_PATH}' nie ma pico_sdk_init.cmake - to nie jest Pico SDK.")
endif ()

set(PICO_SDK_PATH ${PICO_SDK_PATH} CACHE PATH "Path to the Raspberry Pi Pico SDK" FORCE)

include(${PICO_SDK_PATH}/pico_sdk_init.cmake)
