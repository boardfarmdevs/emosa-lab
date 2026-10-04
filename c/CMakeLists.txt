# EMOSA C (see README.md)
cmake_minimum_required(VERSION 3.16)
project(emosa-c C)
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
add_compile_options(-Wall -Wextra -Werror=implicit-function-declaration)

# The bar's gates (QUALITY.md), on in CI: its warning set as errors, and the
# sanitizers for the tests.
option(EMOSA_STRICT "the bar's warning set, as errors" OFF)
option(EMOSA_SANITIZE "AddressSanitizer, LeakSanitizer and UndefinedBehaviorSanitizer" OFF)
if(EMOSA_STRICT)
    add_compile_options(-Wpedantic -Wshadow -Wformat=2 -Wstrict-prototypes -Wmissing-prototypes
                        -Wvla -Wnull-dereference -Werror)
endif()
if(EMOSA_SANITIZE)
    add_compile_options(-fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer)
    add_link_options(-fsanitize=address,undefined)
endif()
option(EMOSA_FUZZ "libFuzzer fuzz targets (clang); the library instrumented for them" OFF)
if(EMOSA_FUZZ)
    add_compile_options(-fsanitize=fuzzer-no-link)
endif()

find_package(PkgConfig REQUIRED)
pkg_check_modules(CJSON REQUIRED libcjson)
pkg_check_modules(CRYPTO REQUIRED libcrypto)
pkg_check_modules(SQLITE REQUIRED sqlite3)
include(GNUInstallDirs)

# The version the programs report (--version, their start message): the reference's
# release from ../pyproject.toml and the git revision in this repository, or the adapter
# kit's VERSION ("VERSION REVISION"); a distribution build passes -DEMOSA_REVISION.
set(EMOSA_VERSION "0.0.0")
set(revision "unknown")
if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/../pyproject.toml)
    file(STRINGS ${CMAKE_CURRENT_SOURCE_DIR}/../pyproject.toml line REGEX "^version = \"[^\"]+\"" LIMIT_COUNT 1)
    string(REGEX REPLACE "^version = \"([^\"]+)\".*" "\\1" EMOSA_VERSION "${line}")
    find_package(Git QUIET)
    if(GIT_FOUND)
        execute_process(COMMAND ${GIT_EXECUTABLE} -C ${CMAKE_CURRENT_SOURCE_DIR} describe --always --abbrev=12
                                --dirty=+dirty --match=v[0-9]*
                        OUTPUT_VARIABLE git_head OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET RESULT_VARIABLE git_rc)
        if(git_rc EQUAL 0 AND git_head)
            set(revision ${git_head})
        endif()
    endif()
elseif(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/../VERSION)
    file(STRINGS ${CMAKE_CURRENT_SOURCE_DIR}/../VERSION line LIMIT_COUNT 1)
    string(REPLACE " " ";" words "${line}")
    list(GET words 0 EMOSA_VERSION)
    list(LENGTH words count)
    if(count GREATER 1)
        list(GET words 1 revision)
    endif()
endif()
if(NOT EMOSA_REVISION)
    set(EMOSA_REVISION ${revision})
endif()
if(NOT EMOSA_VERSION MATCHES "^[0-9A-Za-z.+~-]+$" OR NOT EMOSA_REVISION MATCHES "^[0-9A-Za-z.+~-]+$")
    message(FATAL_ERROR "EMOSA version '${EMOSA_VERSION}' or revision '${EMOSA_REVISION}' unusable")
endif()
configure_file(src/version.h.in ${CMAKE_CURRENT_BINARY_DIR}/version.h @ONLY)

# Where the programs find their data when the environment does not say (EMOSA_SCHEMAS,
# EMOSA_PROFILES): the adapter kit's places by default; a distribution sets its own.
set(EMOSA_SCHEMAS_DIR "/opt/emosa-adapter/share/schemas" CACHE PATH "the JSON schemas")
set(EMOSA_PROFILES_DIR "/usr/share/emosa/profiles" CACHE PATH "the pod profiles")

# RDK's logger instead of stderr (log.h): rdk-logger's rdk_logger.h and librdkloggers.
option(EMOSA_RDK_LOGGER "log through RDK's logger" OFF)
set(EMOSA_RDK_LOG_DIR "/rdklogs/logs/" CACHE STRING "the RDK log directory (at most 31 characters)")
set(EMOSA_RDK_LOG_MAXSIZE 262144 CACHE STRING "bytes per RDK log file")
set(EMOSA_RDK_LOG_MAXCOUNT 2 CACHE STRING "RDK log files kept per program")

add_library(emosa STATIC src/log.c src/common.c src/cmdu.c src/wsc.c src/autoconf.c src/ovs.c src/view.c src/control.c src/southbound.c src/fleet.c src/operation.c src/stats.c src/ovsdb.c src/ethernet.c src/canon.c src/vault.c src/jschema.c src/journal.c src/engine.c src/scope.c src/scope_ap.c src/scope_telemetry.c src/mqtt.c src/reporting.c src/scope_steering.c src/scope_watch.c src/scope_uplink.c src/bhsteer.c src/early.c src/lifecycle.c src/channel_store.c src/jsonrpc.c src/proc.c src/gtp.c)
target_include_directories(emosa PUBLIC src ${CMAKE_CURRENT_BINARY_DIR} ${CJSON_INCLUDE_DIRS} ${CRYPTO_INCLUDE_DIRS}
                           ${SQLITE_INCLUDE_DIRS})
target_link_libraries(emosa ${CRYPTO_LIBRARIES} ${CJSON_LIBRARIES} ${SQLITE_LIBRARIES} m)
target_compile_definitions(emosa PUBLIC EMOSA_SCHEMAS_DIR="${EMOSA_SCHEMAS_DIR}"
                           EMOSA_PROFILES_DIR="${EMOSA_PROFILES_DIR}")
if(EMOSA_RDK_LOGGER)
    find_path(RDK_LOGGER_INCLUDE_DIR rdk_logger.h)
    find_library(RDK_LOGGER_LIBRARY rdkloggers)
    if(NOT RDK_LOGGER_INCLUDE_DIR OR NOT RDK_LOGGER_LIBRARY)
        message(FATAL_ERROR "EMOSA_RDK_LOGGER: rdk_logger.h or librdkloggers not found")
    endif()
    target_compile_definitions(emosa PRIVATE EMOSA_RDK_LOGGER EMOSA_RDK_LOG_DIR="${EMOSA_RDK_LOG_DIR}"
                               EMOSA_RDK_LOG_MAXSIZE=${EMOSA_RDK_LOG_MAXSIZE}L
                               EMOSA_RDK_LOG_MAXCOUNT=${EMOSA_RDK_LOG_MAXCOUNT}L)
    target_include_directories(emosa PRIVATE ${RDK_LOGGER_INCLUDE_DIR})
    target_link_libraries(emosa ${RDK_LOGGER_LIBRARY})
endif()

add_executable(emosa-agent-c src/agent.c)
target_link_libraries(emosa-agent-c emosa ${CJSON_LIBRARIES} ${CRYPTO_LIBRARIES})
install(TARGETS emosa-agent-c DESTINATION bin)

# The fleet (spec §4): the front port every pod is handed to, an agent for each
add_executable(emosa-fleet-c src/fleetd.c)
target_link_libraries(emosa-fleet-c emosa ${CJSON_LIBRARIES} ${CRYPTO_LIBRARIES})
install(TARGETS emosa-fleet-c DESTINATION bin)

# The GRE termination point (spec §8.2): the gateway end of the pods' OpenSync GRE
add_executable(emosa-gtp-c src/gtpd.c)
target_link_libraries(emosa-gtp-c emosa ${CJSON_LIBRARIES} ${CRYPTO_LIBRARIES})
install(TARGETS emosa-gtp-c DESTINATION bin)

# A distribution's package (EMOSA_INSTALL_DATA): with the programs, the schemas and pod
# profiles at the places above, the systemd units (packaging/systemd: the fleet, an agent
# per pod, the GTP), the agent's link helper and the example configurations (the adapter
# kit's, deploy/adapter/files), /etc/default/emosa and the bill of materials. The kit
# installs its own.
option(EMOSA_INSTALL_DATA "install the data files, units and helper of a distribution's package" OFF)
set(EMOSA_SYSTEMD_UNIT_DIR "${CMAKE_INSTALL_PREFIX}/lib/systemd/system" CACHE PATH "where the units go")
set(EMOSA_DNSMASQ "/usr/sbin/dnsmasq" CACHE FILEPATH "the dnsmasq the GTP's unit runs")
# Where the units find the configurations, and the state and run roots a fleet configuration
# names on this platform (in /etc/default/emosa, for tools): a gateway keeps them on its
# persistent storage, the run root on a RAM disk (meta-cmf-bananapi-vcpe: /nvram, /run).
set(EMOSA_FLEET_CONFIG "/etc/emosa-fleet.json" CACHE FILEPATH "the fleet's configuration")
set(EMOSA_AGENT_CONFIG_DIR "/etc/emosa" CACHE PATH "the agents' configurations (the fleet configuration's config_dir)")
set(EMOSA_GTP_CONFIG "/etc/emosa-gtp.json" CACHE FILEPATH "the GTP's configuration")
set(EMOSA_STATE_ROOT "/var/lib/emosa" CACHE PATH "the state root a fleet configuration names here")
set(EMOSA_RUN_ROOT "" CACHE PATH "the run root (a RAM disk) a fleet configuration names here, or none")
if(EMOSA_INSTALL_DATA)
    set(EMOSA_BINDIR ${CMAKE_INSTALL_FULL_BINDIR})
    set(EMOSA_HELPERDIR ${CMAKE_INSTALL_FULL_LIBEXECDIR}/emosa)
    install(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/../schemas/ DESTINATION ${EMOSA_SCHEMAS_DIR}
            FILES_MATCHING PATTERN "*.schema.json")
    install(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/../src/emosa/profiles/ DESTINATION ${EMOSA_PROFILES_DIR}
            FILES_MATCHING PATTERN "*.json")
    foreach(unit emosa-fleet.service emosa-agent@.service emosa-gtp.service)
        configure_file(packaging/systemd/${unit}.in ${CMAKE_CURRENT_BINARY_DIR}/units/${unit} @ONLY)
        install(FILES ${CMAKE_CURRENT_BINARY_DIR}/units/${unit} DESTINATION ${EMOSA_SYSTEMD_UNIT_DIR})
    endforeach()
    set(kit ${CMAKE_CURRENT_SOURCE_DIR}/../deploy/adapter/files)
    install(PROGRAMS ${kit}/emosa-agent-link DESTINATION ${CMAKE_INSTALL_LIBEXECDIR}/emosa)
    configure_file(packaging/default-emosa.in ${CMAKE_CURRENT_BINARY_DIR}/default-emosa @ONLY)
    install(FILES ${CMAKE_CURRENT_BINARY_DIR}/default-emosa DESTINATION ${CMAKE_INSTALL_SYSCONFDIR}/default RENAME emosa)
    install(FILES ${kit}/fleet.example.json ${kit}/gtp.example.json DESTINATION ${CMAKE_INSTALL_DATADIR}/emosa)
    install(FILES ${CMAKE_CURRENT_SOURCE_DIR}/../LICENSE DESTINATION ${CMAKE_INSTALL_DOCDIR})
    # the bill of materials (packaging/sbom.py, SPDX 2.3): the package's files, the
    # libraries as pkg-config reports them here, the programs the units run
    find_package(Python3 COMPONENTS Interpreter REQUIRED)
    set(sbom ${CMAKE_CURRENT_BINARY_DIR}/emosa-c.spdx.json)
    set(sbom_args --revision ${EMOSA_REVISION})
    if(EMOSA_RDK_LOGGER)
        list(APPEND sbom_args --rdk-logger)
    endif()
    add_custom_command(OUTPUT ${sbom}
                       COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/packaging/sbom.py ${sbom} ${sbom_args}
                       DEPENDS packaging/sbom.py emosa-agent-c emosa-fleet-c emosa-gtp-c VERBATIM)
    add_custom_target(emosa-sbom ALL DEPENDS ${sbom})
    install(FILES ${sbom} DESTINATION ${CMAKE_INSTALL_DATADIR}/emosa)
endif()

# The conformance harness (not in the adapter kit, which carries the agent only)
if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/tests/vectors.c)
    add_executable(emosa-vectors tests/vectors.c tests/fixture.c)
    target_link_libraries(emosa-vectors emosa ${CJSON_LIBRARIES})
    enable_testing()
    add_test(NAME conformance COMMAND emosa-vectors ${CMAKE_CURRENT_SOURCE_DIR}/../spec/conformance)
    # a simulated crash leaves the engine's memory behind, as a real one would
    set_tests_properties(conformance PROPERTIES ENVIRONMENT
        "ASAN_OPTIONS=detect_leaks=1:fast_unwind_on_malloc=0;LSAN_OPTIONS=suppressions=${CMAKE_CURRENT_SOURCE_DIR}/tests/lsan.supp")
endif()
if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/tests/log_rdk.c)
    # the RDK logger backend against a stub of rdk-logger's interface (tests/rdk)
    add_executable(emosa-log-rdk tests/log_rdk.c src/log.c src/common.c)
    target_include_directories(emosa-log-rdk BEFORE PRIVATE tests/rdk src ${CJSON_INCLUDE_DIRS})
    target_compile_definitions(emosa-log-rdk PRIVATE EMOSA_RDK_LOGGER EMOSA_RDK_LOG_DIR="/rdklogs/logs/"
                               EMOSA_RDK_LOG_MAXSIZE=4096L EMOSA_RDK_LOG_MAXCOUNT=3L)
    target_link_libraries(emosa-log-rdk ${CJSON_LIBRARIES})
    add_test(NAME log-rdk COMMAND emosa-log-rdk)
endif()
if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/tests/units.c)
    add_executable(emosa-units tests/units.c)
    target_link_libraries(emosa-units emosa ${CJSON_LIBRARIES})
    add_test(NAME units COMMAND sh -c "rm -rf units && exec ./emosa-units ${CMAKE_CURRENT_SOURCE_DIR}/../schemas units")
    set_tests_properties(units PROPERTIES ENVIRONMENT "ASAN_OPTIONS=detect_leaks=1:fast_unwind_on_malloc=0")
endif()

# Fuzz targets for the parsers of untrusted input (QUALITY.md §2). With
# EMOSA_FUZZ (clang) they are libFuzzer binaries; otherwise each is linked with a
# replay driver and ctest runs it over its seed corpus. Not in the adapter kit either.
set(EMOSA_FUZZ_TARGETS cmdu stats rows wsc fleet gtp control mqtt ovsdb config)
if(NOT EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/fuzz/replay.c)
    set(EMOSA_FUZZ_TARGETS)
endif()
foreach(target ${EMOSA_FUZZ_TARGETS})
    if(EMOSA_FUZZ)
        add_executable(emosa-fuzz-${target} fuzz/${target}.c tests/fixture.c)
        target_compile_options(emosa-fuzz-${target} PRIVATE -fsanitize=fuzzer)
        target_link_options(emosa-fuzz-${target} PRIVATE -fsanitize=fuzzer)
    else()
        add_executable(emosa-fuzz-${target} fuzz/${target}.c tests/fixture.c fuzz/replay.c)
    endif()
    target_link_libraries(emosa-fuzz-${target} emosa ${CJSON_LIBRARIES})
    if(NOT EMOSA_FUZZ)
        add_test(NAME fuzz-${target} COMMAND emosa-fuzz-${target} ${CMAKE_CURRENT_SOURCE_DIR}/fuzz/corpus/${target})
        set_tests_properties(fuzz-${target} PROPERTIES ENVIRONMENT
            "ASAN_OPTIONS=detect_leaks=1:fast_unwind_on_malloc=0;EMOSA_VECTORS=${CMAKE_CURRENT_SOURCE_DIR}/../spec/conformance;EMOSA_SCHEMAS=${CMAKE_CURRENT_SOURCE_DIR}/../schemas")
    endif()
endforeach()

