/* The onboarding vectors' agent (spec/conformance/onboarding.json, "agent"): its
 * binding, radio, M1 device and fixed entropy, for the conformance harness and the
 * WSC fuzz target. */
#ifndef EMOSA_TEST_FIXTURE_H
#define EMOSA_TEST_FIXTURE_H

#include "../src/autoconf.h"

#include <cjson/cJSON.h>

typedef struct {
    em_binding binding;
    em_radio_caps radio;
    em_m1_device device;
    em_buf private_key;
    uint8_t nonce[16];
    em_wsc_entropy entropy; /* points into this fixture */
} em_test_agent;

void em_test_agent_load(const cJSON *agent, em_test_agent *out);
void em_test_agent_free(em_test_agent *a);

#endif
