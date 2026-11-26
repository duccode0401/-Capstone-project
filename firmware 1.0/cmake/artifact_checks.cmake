# Explicit checks are necessary: IDF's default check accepts an image fitting ANY app slot.
if(NOT CONFIG_ESPTOOLPY_FLASHSIZE_4MB OR NOT CONFIG_PARTITION_TABLE_CUSTOM)
    message(FATAL_ERROR "Require ESP32 4MB and the custom single-app partition table")
endif()
get_filename_component(sc_expected_csv "${CMAKE_CURRENT_LIST_DIR}/../partitions.csv" REALPATH)
get_filename_component(sc_actual_csv "${CONFIG_PARTITION_TABLE_CUSTOM_FILENAME}" REALPATH BASE_DIR "${PROJECT_DIR}")
if(NOT sc_actual_csv STREQUAL sc_expected_csv)
    message(FATAL_ERROR "SmartCharge must use firmware 1.0/partitions.csv")
endif()
idf_build_get_property(sc_python PYTHON)
idf_build_get_property(sc_build BUILD_DIR)
partition_table_add_check_size_target(sc_slot_check
    DEPENDS gen_project_binary BINARY_PATH "${sc_build}/${PROJECT_NAME}.bin"
    PARTITION_TYPE app PARTITION_SUBTYPE ${SC_SUBTYPE})
add_custom_target(sc_strict_size ALL
    COMMAND ${sc_python} "${CMAKE_CURRENT_LIST_DIR}/../tools/check_artifact.py"
            "${sc_build}/${PROJECT_NAME}.bin" ${SC_SLOT_SIZE}
    DEPENDS gen_project_binary sc_slot_check VERBATIM)
