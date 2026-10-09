/* SPDX-License-Identifier: Apache-2.0 */
/* Fuzz target: a controller's requests after onboarding, which any host on the LAN can
 * send: the control plane (control.c: channel, policy, steering, unassociated stations,
 * scans and the capability queries), the reporting policy and AP Metrics Query
 * (reporting.c) and Backhaul Steering (bhsteer.c), each as the agent dispatches them.
 * The pod is the control vectors' (spec/conformance/control.json, found through
 * EMOSA_VECTORS, else spec/conformance from the working directory). The input is a
 * sequence of frames, each after its length (two bytes, big endian), reassembled as the
 * agent does. */
#include "bhsteer.h"
#include "cmdu.h"
#include "control.h"
#include "reporting.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

static em_device_view view;
static const em_radio_view *radio;
static em_binding binding;
static em_policy_store *store;
static uint8_t ruid[6];
static const uint8_t esp[3] = {0x80, 0x00, 0x60};

static double fixed_clock(void *ctx)
{
    (void)ctx;
    return 1000.0;
}
static double fixed_wall(void) { return 1759500000.0; }
static void fixed_utc(char out[40]) { memcpy(out, "2026-10-03T12:00:00Z", 21); }
static const char *no_mandate(void *ctx, const em_steering_request *r, uint16_t mid)
{
    (void)ctx;
    (void)r;
    (void)mid;
    return "steering_mandate_accepted";
}
static bool keep_nothing(void *ctx, const cJSON *record)
{
    (void)ctx;
    (void)record;
    return true;
}
static uint16_t next_mid(void *ctx) { return ++*(uint16_t *)ctx; }
static const char *bh_move(void *ctx, const char *bssid, int operating_class, int channel)
{
    (void)ctx;
    (void)bssid;
    (void)operating_class;
    (void)channel;
    return NULL;
}
static int bh_outcome(void *ctx, const char *bssid, const char **why)
{
    (void)ctx;
    (void)bssid;
    *why = "fuzz";
    return -1;
}

static char *read_all(const char *path)
{
    FILE *f = fopen(path, "rb");
    if (!f)
        return NULL;
    em_buf b = {0};
    char chunk[65536];
    size_t n;
    while ((n = fread(chunk, 1, sizeof(chunk), f)) > 0)
        (void)em_buf_put(&b, chunk, n);
    fclose(f);
    (void)em_buf_u8(&b, 0);
    return (char *)b.data;
}

static void setup(void)
{
    em_init();
    const char *dir = getenv("EMOSA_VECTORS") ? getenv("EMOSA_VECTORS") : "spec/conformance";
    char path[1024], scratch[] = "/tmp/emosa-fuzz-control-XXXXXX";
    snprintf(path, sizeof(path), "%s/control.json", dir);
    char *text = read_all(path);
    cJSON *doc = text ? cJSON_Parse(text) : NULL;
    free(text);
    const cJSON *agent = cJSON_GetObjectItemCaseSensitive(doc, "agent");
    if (!doc || em_device_view_from_rows(cJSON_GetObjectItemCaseSensitive(doc, "ovsdb_tables"), &view) != EM_OK ||
        !em_parse_mac(cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(agent, "radio")), ruid) ||
        !em_parse_mac(cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(agent, "al_mac")), binding.local_al) ||
        !em_parse_mac(cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(agent, "controller_al")),
                      binding.controller_al) ||
        !mkdtemp(scratch)) {
        fprintf(stderr, "control fuzz target: %s unusable\n", path);
        abort();
    }
    memcpy(binding.sources[0], binding.controller_al, 6);
    binding.nsources = 1;
    radio = em_view_radio(&view, ruid);
    snprintf(path, sizeof(path), "%s/policy.sqlite", scratch);
    store = em_policy_store_open(path, "fuzz");
    cJSON_Delete(doc);
    if (!radio || !store)
        abort();
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    static bool ready;
    if (!ready) {
        setup();
        ready = true;
    }
    em_control control = {0};
    control.binding = binding;
    control.radio = radio;
    control.tx_power_dbm = radio->tx_power;
    control.max_eirp_dbm = 30;
    control.previous_mid = 0x1000;
    control.executor = no_mandate;
    control.keep_channel_policy = keep_nothing;
    control.utc = fixed_utc;
    control.wall = fixed_wall;
    em_reporting reporting = {0};
    em_reporting_start(&reporting, store, binding.controller_al, binding.local_al, ruid, 1000.0); /* the requests come at 1000 */
    em_bh_shared shared = {0};
    em_bh_coordinator bh = {0};
    em_bh_start(&bh, &shared, binding.controller_al, binding.local_al, bh_move, bh_outcome, NULL);
    em_metric_source source = {radio, NULL, esp, 30.0, fixed_wall};
    const uint8_t stations[1][6] = {{0x02, 0, 0, 0, 0x14, 0}};
    uint16_t mid = 0x2000;
    em_reassembler *r = em_reassembler_new(fixed_clock, NULL, 5.0, 8, 65536, 16384, 16);
    while (size >= 2) {
        size_t len = (size_t)data[0] << 8 | data[1];
        data += 2;
        size -= 2;
        if (len > size)
            len = size;
        em_message *m = NULL;
        if (em_reassembler_feed(r, data, len, "fuzz", &m) == EM_OK && m) {
            em_frames out = {0};
            em_reason error = EM_OK;
            (void)em_control_handle(&control, m, &out, &error);
            if (m->message_type == 0x8003)
                (void)em_reporting_policy(&reporting, m, 1000.0, true, &source, next_mid, &mid, &out, &error);
            else if (m->message_type == 0x800B)
                (void)em_reporting_query(&reporting, m, true, &source, &out, &error);
            else if (m->message_type == 0x8019)
                (void)em_bh_handle(&bh, m, 1000.0, stations, 1, &out, &error);
            em_bh_tick(&bh, 1001.0, true, NULL, &out);
            em_reporting_tick(&reporting, 1002.0, true, &source, next_mid, &mid, &out);
            em_frames_free(&out);
            em_message_free(m);
        }
        data += len;
        size -= len;
    }
    em_reassembler_free(r);
    cJSON_Delete(em_bh_status(&bh));
    cJSON_Delete(em_reporting_status(&reporting));
    em_bh_close(&bh);
    em_reporting_close(&reporting);
    /* what the agent frees when the next session reuses these (em_*_start), as each input
     * here is a session of its own */
    cJSON_Delete(bh.last);
    cJSON_Delete(reporting.value);
    return 0;
}
