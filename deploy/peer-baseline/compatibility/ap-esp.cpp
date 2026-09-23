// SPDX-License-Identifier: BSD-2-Clause-Patent
// Parse actual pinned TLVF wire objects using the helper called by the controller.
#include "ap_metrics_esp.h"
#include <easylogging++.h>
#include <iostream>
#include <sstream>
#include <vector>

INITIALIZE_EASYLOGGINGPP

std::vector<uint8_t> packet(uint8_t flags, const std::vector<uint8_t> &payload)
{
    std::vector<uint8_t> data{0x94, 0, uint8_t(10 + payload.size()),
                              2, 0, 0, 0, 0, 1, 123, 0, 1, flags};
    data.insert(data.end(), payload.begin(), payload.end());
    return data;
}

int main()
{
    // Reproduce the old fixed VI offset against the real TLVF bounds check.
    auto old_bytes = packet(0x90, {0x01, 0x12, 0x23, 0x02, 0x34, 0x45});
    wfa_map::tlvApMetrics old_tlv(old_bytes.data(), old_bytes.size(), true);
    std::ostringstream diagnostic;
    auto original_stdout = std::cout.rdbuf(diagnostic.rdbuf());
    const bool baseline_invalid = old_tlv.isInitialized() &&
        old_tlv.estimated_service_info_field_length() == 6 &&
        old_tlv.estimated_service_info_field(9) == nullptr;
    std::cout.rdbuf(original_stdout);
    std::cerr << diagnostic.str();
    if (!baseline_invalid) return 1;

    unsigned valid = 0, rejected = 0;
    for (unsigned mask = 0; mask < 16; ++mask) {
        std::vector<uint8_t> payload;
        // Distinct bytes detect wrong category offsets even without a crash.
        for (unsigned i = 0; i < 4; ++i) {
            if (mask & (8 >> i)) {
                payload.push_back(0x10 + i);
                payload.push_back(0x40 + i);
                payload.push_back(0x80 + i);
            }
        }
        for (unsigned size = 0; size <= 14; ++size) {
            auto input = payload;
            input.resize(size, 0xee);
            auto bytes = packet(mask << 4, input);
            wfa_map::tlvApMetrics tlv(bytes.data(), bytes.size(), true);
            son::ApMetricEspFields output;
            for (auto &field : output) { field.present = true; field.value.fill(0xaa); }
            const bool want = (mask & 8) && size == payload.size();
            if (son::decode_ap_metric_esp(tlv, output) != want) return 2;
            if (!want) {
                for (const auto &field : output) {
                    if (!field.present || field.value != std::array<uint8_t, 3>{{0xaa, 0xaa, 0xaa}})
                        return 3;
                }
                ++rejected;
                continue;
            }
            for (unsigned i = 0; i < 4; ++i) {
                const bool present = mask & (8 >> i);
                if (output[i].present != present) return 4;
                const std::array<uint8_t, 3> expected{{uint8_t(0x10 + i), uint8_t(0x40 + i), uint8_t(0x80 + i)}};
                if (present && output[i].value != expected) return 5;
            }
            ++valid;
        }
    }
    if (valid != 8 || rejected != 232) return 6;
    std::cout << "{\"passed\":240,\"valid_presence_combinations\":" << valid
              << ",\"malformed_inputs_rejected\":" << rejected
              << ",\"baseline_fixed_vi_offset_returns_null\":true,"
                 "\"native_tlvf_used\":true,\"measurement_semantics_qualified\":false}\n";
}
