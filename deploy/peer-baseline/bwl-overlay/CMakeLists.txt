cmake_minimum_required(VERSION 3.16)
project(emosa_peer_bwl_overlay LANGUAGES C CXX)
# Reuse the upstream BWL target and the pinned artifact's exported headers/libraries.
# Only libbwl is rebuilt; all native executables and other native libraries stay pinned.
set(prplmesh_VERSION 6.0.0)
set(prplmesh_VERSION_MAJOR 6)
set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
set(BUILD_SHARED_LIBS ON)
set(BUILD_TESTS OFF)
set(BWL_TYPE NL80211 CACHE STRING "Real nl80211 backend" FORCE)
set(TARGET_PLATFORM linux)
set(TMP_PATH /tmp/beerocks)
set(CMAKE_MODULE_PATH "${PEER_SOURCE}/cmake")
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -pthread -fno-exceptions")
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -pthread -fno-exceptions")
add_compile_definitions(BEEROCKS_LINUX BEEROCKS_VERSION="6.0.0"
    ELPP_THREAD_SAFE ELPP_FORCE_USE_STD_THREAD ELPP_NO_DEFAULT_LOG_FILE
    ELPP_STL_LOGGING ELPP_LOG_UNORDERED_MAP ELPP_LOG_UNORDERED_SET ELPP_SYSLOG)
find_package(nl-3 REQUIRED)
find_package(nl-route-3 REQUIRED)
foreach(lib bcl elpp tlvf prplmesh_hostapd)
    add_library(${lib} SHARED IMPORTED)
    set_target_properties(${lib} PROPERTIES
        IMPORTED_LOCATION "${PEER_INSTALL}/lib/lib${lib}.so.6.0.0"
        INTERFACE_INCLUDE_DIRECTORIES "${PEER_INSTALL}/include")
endforeach()
set_target_properties(bcl PROPERTIES INTERFACE_LINK_LIBRARIES "nl-3;nl-route-3;tlvf;elpp")
set_target_properties(prplmesh_hostapd PROPERTIES INTERFACE_INCLUDE_DIRECTORIES
    "${PEER_SOURCE}/common/beerocks/hostapd/include")
set(HOSTAPD_INCLUDE_DIR "${HOSTAP_SOURCE}" CACHE PATH "Pinned hostap control sources")
set(PLATFORM_BUILD_DIR "${CMAKE_BINARY_DIR}")
set(PLATFORM_STAGING_DIR "${PEER_INSTALL}")
add_subdirectory("${PEER_SOURCE}/common/beerocks/bwl" bwl)
if(NOT BWL_TYPE STREQUAL "NL80211")
    message(FATAL_ERROR "Native baseline requires the real nl80211 HAL")
endif()
set_target_properties(bwl PROPERTIES INSTALL_RPATH "$ORIGIN")
if(EMOSA_HE_REGRESSION)
    add_executable(emosa-he-length "${EMOSA_HE_REGRESSION}")
    target_include_directories(emosa-he-length PRIVATE
        "${PEER_SOURCE}/common/beerocks/bwl/shared"
        "${PEER_SOURCE}/common/beerocks/bwl/include")
    target_link_libraries(emosa-he-length PRIVATE bwl)
endif()
