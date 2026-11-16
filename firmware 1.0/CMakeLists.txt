cmake_minimum_required(VERSION 3.16)
set(PROJECT_VER "1.0.0" CACHE STRING "SmartCharge firmware semantic version")

set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/smartcharge")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(smartcharge)
set(SC_SUBTYPE ota_0)
set(SC_SLOT_SIZE 0x2F0000)
include(cmake/artifact_checks.cmake)
# IDF otherwise flashes to the first/factory app. Keep Recovery untouched.
foreach(sc_target flash app-flash)
    foreach(sc_property FLASH_FILE FLASH_ENTRY IMAGES)
        get_property(sc_values TARGET ${sc_target} PROPERTY ${sc_property})
        set(sc_replaced)
        foreach(sc_value IN LISTS sc_values)
            # The app filename may still be a generator expression at this point.
            # Only the app uses offset 0x10000 in this exact, runtime-checked layout.
            string(REPLACE "0x10000" "0x110000" sc_value "${sc_value}")
            list(APPEND sc_replaced "${sc_value}")
        endforeach()
        set_property(TARGET ${sc_target} PROPERTY ${sc_property} "${sc_replaced}")
    endforeach()
endforeach()
