// Exercise the native controller's unchanged conversion implementation.
// EasyMesh byte counters are 32-bit values on the wire; conversion is uint64_t.
#include "db/db.h"
#include <array>
#include <cstdint>
#include <iostream>

INITIALIZE_EASYLOGGINGPP

int main()
{
    son::db::sDbMasterConfig config{};
    beerocks::logging logger("emosa-counter-regression");
    son::db database(config, logger, {}, nullptr);
    using Units = wfa_map::tlvProfile2ApCapability::eByteCounterUnits;
    const std::array<Units, 3> units{{Units::BYTES, Units::KIBIBYTES, Units::MEBIBYTES}};
    const std::array<uint64_t, 3> scale{{1, 1024, 1024 * 1024}};
    const std::array<uint64_t, 4> values{{0, 1, 1234567, UINT32_MAX}};
    unsigned passed = 0;
    for (size_t i = 0; i < units.size(); ++i) {
        for (auto value : values) {
            const auto actual = database.recalculate_attr_to_byte_units(units[i], value);
            if (actual != value * scale[i]) {
                std::cerr << "native counter conversion failed\n";
                return 1;
            }
            ++passed;
        }
    }
    std::cout << "{\"passed\": " << passed
              << ", \"implementation\": \"native db::recalculate_attr_to_byte_units\", "
                 "\"scope\": \"bytes/KiB/MiB; zero, one, typical and maximum 32-bit counter\"}\n";
}
