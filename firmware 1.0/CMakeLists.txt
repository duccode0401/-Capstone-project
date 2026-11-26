cmake_minimum_required(VERSION 3.16)
set(PROJECT_VER "2.0.0" CACHE STRING "SmartCharge firmware semantic version")

set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/smartcharge")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(smartcharge)
set(SC_SUBTYPE factory)
set(SC_SLOT_SIZE 0x3F0000)
include(cmake/artifact_checks.cmake)
