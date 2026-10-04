/* SPDX-License-Identifier: Apache-2.0 */
/* Fuzz target: a controller's WSC M2 (autoconf.c em_receive_m2, wsc.c), parsed as
 * far as its authenticator and, for the recorded M2s, beyond. The agent and its M1
 * are the onboarding vectors' (spec/conformance/onboarding.json, found through
 * EMOSA_VECTORS, else spec/conformance from the working directory). The input's
 * first byte selects multi_bss (bit 0) and shared_session (bit 1); the rest is a
 * sequence of 1905 frames, each after its length (two bytes, big endian). */
#include "../tests/fixture.h"

#include <stdio.h>
#include <stdlib.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

static double fixed_clock(void *ctx)
{
    (void)ctx;
    return 1000.0;
}

static em_test_agent agent;
static em_m1 m1;

static void setup(void)
{
    em_init();
    const char *dir = getenv("EMOSA_VECTORS") ? getenv("EMOSA_VECTORS") : "spec/conformance";
    char path[4096];
    snprintf(path, sizeof(path), "%s/onboarding.json", dir);
    FILE *f = fopen(path, "rb");
    if (!f) {
        perror(path);
        exit(2);
    }
    static char text[1 << 20];
    size_t n = fread(text, 1, sizeof(text) - 1, f);
    fclose(f);
    text[n] = 0;
    cJSON *doc = cJSON_Parse(text);
    if (!doc) {
        fprintf(stderr, "%s: invalid JSON\n", path);
        exit(2);
    }
    em_test_agent_load(cJSON_GetObjectItemCaseSensitive(doc, "agent"), &agent);
    cJSON_Delete(doc);
    if (em_m1_create(&agent.device, &agent.entropy, &m1) != EM_OK) {
        fprintf(stderr, "M1 not created\n");
        exit(2);
    }
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    static bool ready;
    if (!ready) {
        setup();
        ready = true;
    }
    if (size < 1)
        return 0;
    bool multi_bss = data[0] & 1, shared = data[0] & 2;
    data++;
    size--;
    em_reassembler *r = em_reassembler_new(fixed_clock, NULL, 5.0, 8, 65536, 16384, 16);
    while (size >= 2) {
        size_t len = (size_t)data[0] << 8 | data[1];
        data += 2;
        size -= 2;
        if (len > size)
            len = size;
        em_message *m = NULL;
        if (em_reassembler_feed(r, data, len, "fuzz", &m) == EM_OK && m) {
            em_m2_result result;
            em_receive_m2(&agent.binding, &agent.radio, &m1, m, multi_bss, shared, &result);
            em_message_free(m);
        }
        data += len;
        size -= len;
    }
    em_reassembler_free(r);
    return 0;
}
