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

add_library(emosa STATIC src/common.c src/cmdu.c src/wsc.c src/autoconf.c src/ovs.c src/view.c src/control.c src/southbound.c src/fleet.c src/operation.c src/stats.c src/ovsdb.c src/ethernet.c src/canon.c src/vault.c src/jschema.c src/journal.c src/engine.c src/scope.c src/scope_ap.c src/scope_telemetry.c src/mqtt.c src/reporting.c src/scope_steering.c src/scope_watch.c src/scope_uplink.c src/bhsteer.c src/early.c src/lifecycle.c src/channel_store.c src/jsonrpc.c src/proc.c src/gtp.c)
target_include_directories(emosa PUBLIC src ${CJSON_INCLUDE_DIRS} ${CRYPTO_INCLUDE_DIRS} ${SQLITE_INCLUDE_DIRS})
target_link_libraries(emosa ${CRYPTO_LIBRARIES} ${CJSON_LIBRARIES} ${SQLITE_LIBRARIES} m)

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
if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/tests/units.c)
    add_executable(emosa-units tests/units.c)
    target_link_libraries(emosa-units emosa ${CJSON_LIBRARIES})
    add_test(NAME units COMMAND sh -c "rm -rf units && exec ./emosa-units ${CMAKE_CURRENT_SOURCE_DIR}/../schemas units")
    set_tests_properties(units PROPERTIES ENVIRONMENT "ASAN_OPTIONS=detect_leaks=1:fast_unwind_on_malloc=0")
endif()

# Fuzz targets for the parsers of untrusted input (QUALITY.md §2). With
# EMOSA_FUZZ (clang) they are libFuzzer binaries; otherwise each is linked with a
# replay driver and ctest runs it over its seed corpus. Not in the adapter kit either.
set(EMOSA_FUZZ_TARGETS cmdu stats rows wsc fleet gtp)
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
            "ASAN_OPTIONS=detect_leaks=1:fast_unwind_on_malloc=0;EMOSA_VECTORS=${CMAKE_CURRENT_SOURCE_DIR}/../spec/conformance")
    endif()
endforeach()

