# EMOSA C lab prototype (not production code; see README.md)
cmake_minimum_required(VERSION 3.16)
project(emosa-c C)
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
add_compile_options(-Wall -Wextra -Werror=implicit-function-declaration)

find_package(PkgConfig REQUIRED)
pkg_check_modules(CJSON REQUIRED libcjson)
pkg_check_modules(CRYPTO REQUIRED libcrypto)

add_library(emosa STATIC src/common.c src/cmdu.c src/wsc.c src/autoconf.c src/ovs.c src/view.c src/control.c src/southbound.c)
target_include_directories(emosa PUBLIC src ${CJSON_INCLUDE_DIRS} ${CRYPTO_INCLUDE_DIRS})
target_link_libraries(emosa ${CRYPTO_LIBRARIES} ${CJSON_LIBRARIES})

add_executable(emosa-vectors tests/vectors.c)
target_link_libraries(emosa-vectors emosa ${CJSON_LIBRARIES})

enable_testing()
add_test(NAME conformance COMMAND emosa-vectors ${CMAKE_CURRENT_SOURCE_DIR}/../spec/conformance)
