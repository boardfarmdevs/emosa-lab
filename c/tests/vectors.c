/* SPDX-License-Identifier: Apache-2.0 */
/* Replays the conformance vectors (spec/conformance) against the C implementation.
 * Usage: emosa-vectors <spec/conformance directory> */
#include <cjson/cJSON.h>
#include <dirent.h>
#include <math.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "../src/autoconf.h"
#include "../src/bhsteer.h"
#include "../src/cmdu.h"
#include "../src/control.h"
#include "fixture.h"
#include "../src/fleet.h"
#include "../src/gtp.h"
#include "../src/proc.h"
#include "../src/operation.h"
#include "../src/ovs.h"
#include "../src/early.h"
#include "../src/engine.h"
#include "../src/journal.h"
#include "../src/lifecycle.h"
#include "../src/jschema.h"
#include "../src/ovsdb.h"
#include "../src/vault.h"
#include "../src/reporting.h"
#include "../src/scope_steering.h"
#include "../src/scope_telemetry.h"
#include "../src/scope_watch.h"
#include "../src/southbound.h"
#include "../src/stats.h"
#include "../src/view.h"
#include "../src/wsc.h"

static int failures, checks;

static void fail(const char *set, const char *name, const char *what)
{
    failures++;
    fprintf(stderr, "FAIL %s %s: %s\n", set, name, what);
}

static cJSON *load(const char *dir, const char *file)
{
    char path[1024];
    snprintf(path, sizeof(path), "%s/%s", dir, file);
    FILE *f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "cannot open %s\n", path);
        exit(2);
    }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *text = malloc((size_t)n + 1);
    if (!text || fread(text, 1, (size_t)n, f) != (size_t)n)
        exit(2);
    text[n] = 0;
    fclose(f);
    cJSON *json = cJSON_Parse(text);
    free(text);
    if (!json) {
        fprintf(stderr, "invalid JSON in %s\n", path);
        exit(2);
    }
    return json;
}

static const char *str(const cJSON *o, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, key);
    return cJSON_IsString(v) ? v->valuestring : NULL;
}

static double num(const cJSON *o, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, key);
    return cJSON_IsNumber(v) ? v->valuedouble : -1;
}

/* A JSON TLV list ({"type": "0x..", "value": hex}) to em_tlv values. */
static em_tlv *tlv_list(const cJSON *list, size_t *n)
{
    *n = (size_t)cJSON_GetArraySize(list);
    em_tlv *tlvs = calloc(*n ? *n : 1, sizeof(em_tlv));
    size_t i = 0;
    const cJSON *item;
    cJSON_ArrayForEach(item, list)
    {
        em_buf value = {0};
        em_unhex(str(item, "value"), &value);
        tlvs[i].kind = (uint8_t)strtol(str(item, "type"), NULL, 16);
        tlvs[i].len = (uint16_t)value.len;
        tlvs[i].value = value.data ? value.data : calloc(1, 1);
        i++;
    }
    return tlvs;
}

static void free_list(em_tlv *tlvs, size_t n)
{
    for (size_t i = 0; i < n; i++)
        free(tlvs[i].value);
    free(tlvs);
}

/* The JSON form of a message's TLVs, compared as JSON. */
static cJSON *tlvs_json(const em_message *m)
{
    cJSON *list = cJSON_CreateArray();
    for (size_t i = 0; i < m->ntlvs; i++) {
        char type[8];
        snprintf(type, sizeof(type), "0x%02x", m->tlvs[i].kind);
        char *value = em_hex(m->tlvs[i].value, m->tlvs[i].len);
        cJSON *t = cJSON_CreateObject();
        cJSON_AddStringToObject(t, "type", type);
        cJSON_AddStringToObject(t, "value", value);
        free(value);
        cJSON_AddItemToArray(list, t);
    }
    return list;
}

static double fixed_clock(void *ctx)
{
    (void)ctx;
    return 0.0;
}

static void cmdu_vectors(const char *dir)
{
    cJSON *doc = load(dir, "cmdu.json");
    const cJSON *c;
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "encode"))
    {
        const cJSON *in = cJSON_GetObjectItemCaseSensitive(c, "input");
        uint8_t dst[6], src[6];
        em_parse_mac(str(in, "destination"), dst);
        em_parse_mac(str(in, "source"), src);
        size_t n;
        em_tlv *tlvs = tlv_list(cJSON_GetObjectItemCaseSensitive(in, "tlvs"), &n);
        em_frames frames;
        em_reason r = em_fragment(dst, src, (uint16_t)strtol(str(in, "message_type"), NULL, 16),
                                  (uint16_t)num(in, "mid"),
                                  tlvs, n, cJSON_IsTrue(cJSON_GetObjectItem(in, "relay")),
                                  (unsigned)num(in, "mtu"), &frames);
        const cJSON *expected = cJSON_GetObjectItemCaseSensitive(c, "expected_frames");
        checks++;
        if (r != EM_OK || (int)frames.count != cJSON_GetArraySize(expected)) {
            fail("cmdu", str(c, "name"), "frame count");
        } else {
            for (size_t i = 0; i < frames.count; i++) {
                char *hex = em_hex(frames.frames[i].data, frames.frames[i].len);
                if (strcmp(hex, cJSON_GetArrayItem(expected, (int)i)->valuestring))
                    fail("cmdu", str(c, "name"), "frame bytes");
                free(hex);
            }
        }
        em_frames_free(&frames);
        free_list(tlvs, n);
    }
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "decode"))
    {
        em_reassembler *r = em_reassembler_new(fixed_clock, NULL, 5.0, 64, 2097152, 65536, 64);
        em_message *m = NULL;
        em_reason reason = EM_OK;
        const cJSON *frame;
        cJSON_ArrayForEach(frame, cJSON_GetObjectItemCaseSensitive(c, "frames"))
        {
            em_buf bytes = {0};
            em_unhex(frame->valuestring, &bytes);
            em_message_free(m);
            m = NULL;
            reason = em_reassembler_feed(r, bytes.data, bytes.len, "conformance", &m);
            em_buf_free(&bytes);
            if (reason != EM_OK)
                break;
        }
        const cJSON *expected = cJSON_GetObjectItemCaseSensitive(c, "expected");
        const char *error = str(expected, "error");
        checks++;
        if (error) {
            if (reason == EM_OK || strcmp(error, em_reason_name(reason)))
                fail("cmdu", str(c, "name"), "expected a rejection");
        } else {
            const cJSON *want = cJSON_GetObjectItemCaseSensitive(expected, "message");
            if (reason != EM_OK) {
                fail("cmdu", str(c, "name"), em_reason_name(reason));
            } else if (cJSON_IsNull(want)) {
                if (m)
                    fail("cmdu", str(c, "name"), "complete too early");
            } else if (!m) {
                fail("cmdu", str(c, "name"), "incomplete");
            } else {
                char type[8];
                snprintf(type, sizeof(type), "0x%04x", m->message_type);
                char *d = em_mac_str(m->destination), *s = em_mac_str(m->source);
                cJSON *got = tlvs_json(m);
                if (strcmp(d, str(want, "destination")) || strcmp(s, str(want, "source")) ||
                    strcmp(type, str(want, "message_type")) || m->mid != num(want, "mid") ||
                    m->relay != cJSON_IsTrue(cJSON_GetObjectItem(want, "relay")) ||
                    !cJSON_Compare(got, cJSON_GetObjectItemCaseSensitive(want, "tlvs"), 1))
                    fail("cmdu", str(c, "name"), "message differs");
                cJSON_Delete(got);
                free(d);
                free(s);
            }
        }
        em_message_free(m);
        em_reassembler_free(r);
    }
    cJSON_Delete(doc);
}

/* -- onboarding.json ------------------------------------------------------------- */

static bool frames_equal(const em_frames *f, const cJSON *expected)
{
    if ((int)f->count != cJSON_GetArraySize(expected))
        return false;
    for (size_t i = 0; i < f->count; i++) {
        char *hex = em_hex(f->frames[i].data, f->frames[i].len);
        bool same = !strcmp(hex, cJSON_GetArrayItem(expected, (int)i)->valuestring);
        free(hex);
        if (!same)
            return false;
    }
    return true;
}

/* Feed hex frames to a fresh reassembler; the complete message or NULL. */
static em_message *assemble(const cJSON *frames)
{
    em_reassembler *r = em_reassembler_new(fixed_clock, NULL, 5.0, 64, 2097152, 65536, 64);
    em_message *m = NULL;
    const cJSON *frame;
    cJSON_ArrayForEach(frame, frames)
    {
        em_buf bytes = {0};
        em_unhex(frame->valuestring, &bytes);
        em_message_free(m);
        m = NULL;
        em_reason reason = em_reassembler_feed(r, bytes.data, bytes.len, "conformance", &m);
        em_buf_free(&bytes);
        if (reason != EM_OK)
            break;
    }
    em_reassembler_free(r);
    return m;
}

static void onboarding_vectors(const char *dir)
{
    cJSON *doc = load(dir, "onboarding.json");
    const cJSON *agent = cJSON_GetObjectItemCaseSensitive(doc, "agent");
    em_test_agent fixture;
    em_test_agent_load(agent, &fixture);
    const em_binding binding = fixture.binding;
    const em_radio_caps radio = fixture.radio;
    const em_m1_device d = fixture.device;
    const em_wsc_entropy entropy = fixture.entropy;
    const cJSON *search = cJSON_GetObjectItemCaseSensitive(agent, "search");

    const cJSON *c;
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "cases"))
    {
        const char *name = str(c, "message_set");
        em_message_set set = !strcmp(name, "r1") ? EM_SET_R1 : EM_SET_61;
        uint16_t mid = (uint16_t)num(agent, "first_mid");
        em_frames frames;
        checks++;
        if (em_search_frames(&binding, (int)num(search, "band"), (int)num(search, "profile"),
                             radio.profile2, set, mid, &frames) != EM_OK ||
            !frames_equal(&frames, cJSON_GetObjectItemCaseSensitive(c, "search_frames")))
            fail("onboarding", name, "search frames");
        em_frames_free(&frames);

        checks++;
        em_message *response = assemble(cJSON_GetObjectItemCaseSensitive(c, "response_frames"));
        em_advertisement adv;
        const cJSON *want = cJSON_GetObjectItemCaseSensitive(c, "expected_admission");
        bool admitted = response && em_binding_accepts(&binding, response) &&
                        response->mid == mid &&
                        em_parse_response(response, set, &adv) == EM_OK &&
                        adv.band == (int)num(search, "band") &&
                        (set == EM_SET_R1 || adv.profile == 1 || adv.profile > 3);
        const cJSON *flags = cJSON_GetObjectItemCaseSensitive(want, "controller_flags");
        if (admitted != cJSON_IsTrue(cJSON_GetObjectItem(want, "admitted")) ||
            (admitted && (adv.band != num(want, "band") || adv.profile != num(want, "profile") ||
                          adv.controller_flags != (cJSON_IsNull(flags) ? -1 : flags->valueint) ||
                          adv.security_capability_present !=
                              cJSON_IsTrue(cJSON_GetObjectItem(want, "security_capability_present")))))
            fail("onboarding", name, "admission");
        em_message_free(response);

        checks++;
        em_m1 m1;
        if (em_m1_create(&d, &entropy, &m1) != EM_OK) {
            fail("onboarding", name, "M1 not created");
            continue;
        }
        if (em_m1_frames(&binding, &radio, &m1, set, (uint16_t)(mid + 1), &frames) != EM_OK ||
            !frames_equal(&frames, cJSON_GetObjectItemCaseSensitive(c, "m1_frames"))) {
            fail("onboarding", name, "M1 frames");
            if (getenv("EMOSA_VECTORS_DEBUG") && frames.count) {
                char *hex = em_hex(frames.frames[0].data, frames.frames[0].len);
                fprintf(stderr, "got  %s\nwant %s\n", hex,
                        cJSON_GetArrayItem(cJSON_GetObjectItemCaseSensitive(c, "m1_frames"), 0)->valuestring);
                free(hex);
            }
        }
        em_frames_free(&frames);

        checks++;
        em_message *m2 = assemble(cJSON_GetObjectItemCaseSensitive(c, "m2_frames"));
        em_m2_result result;
        const cJSON *expected = cJSON_GetObjectItemCaseSensitive(c, "expected_m2");
        const cJSON *index = cJSON_GetObjectItemCaseSensitive(expected, "bss_index");
        em_reason r = m2 ? em_receive_m2(&binding, &radio, &m1, m2, false, false, &result)
                         : EM_INVALID_INPUT;
        if (r != EM_OK)
            fail("onboarding", name, em_reason_name(r));
        else if (result.count != 1 || strcmp(result.bss[0].ssid, str(expected, "ssid")) ||
                 strcmp(result.bss[0].passphrase, str(expected, "passphrase")) ||
                 result.bss[0].bss_index != (cJSON_IsNull(index) ? -1 : index->valueint))
            fail("onboarding", name, "M2 settings");
        em_message_free(m2);

        checks++;
        em_message *tampered = assemble(cJSON_GetObjectItemCaseSensitive(c, "tampered_m2_frames"));
        const char *error = str(cJSON_GetObjectItemCaseSensitive(c, "expected_tampered"), "error");
        r = tampered ? em_receive_m2(&binding, &radio, &m1, tampered, false, false, &result)
                     : EM_INVALID_INPUT;
        if (!error || r == EM_OK || strcmp(error, em_reason_name(r)))
            fail("onboarding", name, "tampered M2 accepted or wrong reason");
        em_message_free(tampered);
        em_m1_free(&m1);
    }
    /* the session's admission of a Response: dropped, or its issues (non_dpp_admission) */
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "admission"))
    {
        const char *name = str(c, "name");
        em_message_set set = !strcmp(str(c, "message_set"), "r1") ? EM_SET_R1 : EM_SET_61;
        const cJSON *want = cJSON_GetObjectItemCaseSensitive(c, "expected");
        em_message *response = assemble(cJSON_GetObjectItemCaseSensitive(c, "response_frames"));
        em_advertisement adv;
        bool dropped = !response || em_parse_response(response, set, &adv) != EM_OK || !em_response_usable(&adv, set);
        checks++;
        if (dropped != cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(want, "dropped"))) {
            fail("admission", name, dropped ? "dropped" : "not dropped");
        } else if (!dropped) {
            const char *issues[8];
            size_t n = em_admission_issues(&adv, set, issues);
            const cJSON *expected = cJSON_GetObjectItemCaseSensitive(want, "issues");
            bool same = (int)n == cJSON_GetArraySize(expected);
            for (size_t i = 0; same && i < n; i++)
                same = !strcmp(issues[i], cJSON_GetArrayItem(expected, (int)i)->valuestring);
            if (!same)
                fail("admission", name, "issues differ");
        }
        em_message_free(response);
    }
    em_test_agent_free(&fixture);
    cJSON_Delete(doc);
}

/* -- translation-northbound.json ------------------------------------------------- */

static cJSON *list_json(const em_tlv *tlvs, size_t n)
{
    em_message m = {0};
    m.ntlvs = n;
    m.tlvs = (em_tlv *)tlvs;
    return tlvs_json(&m);
}

static void northbound_vectors(const char *dir)
{
    cJSON *doc = load(dir, "translation-northbound.json");
    const cJSON *c;
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "cases"))
    {
        const char *name = str(c, "name");
        const cJSON *agent = cJSON_GetObjectItemCaseSensitive(c, "agent");
        const cJSON *expected = cJSON_GetObjectItemCaseSensitive(c, "expected");
        em_device_view *view = malloc(sizeof(*view));
        checks++;
        if (em_device_view_from_rows(cJSON_GetObjectItemCaseSensitive(c, "ovsdb_tables"), view) != EM_OK) {
            fail("northbound", name, "device view");
            free(view);
            continue;
        }
        cJSON *got = em_device_view_json(view);
        if (!cJSON_Compare(got, cJSON_GetObjectItemCaseSensitive(expected, "device_view"), 1))
            fail("northbound", name, "device view differs");
        cJSON_Delete(got);
        uint8_t ruid[6], agent_al[6], controller_al[6];
        em_parse_mac(str(agent, "radio"), ruid);
        em_parse_mac(str(agent, "al_mac"), agent_al);
        em_parse_mac(str(agent, "controller_al"), controller_al);
        const em_radio_view *radio = em_view_radio(view, ruid);
        int channel = radio && radio->nbss && radio->has_channel ? radio->channel : 6;
        checks++;
        if (!radio || channel != num(agent, "channel")) {
            fail("northbound", name, "radio or channel");
            free(view);
            continue;
        }
        em_tlv_list list;
        checks++;
        if (em_capability_tlvs(radio, channel, (uint8_t)num(agent, "max_bss"), (int8_t)num(agent, "max_eirp"), &list) != EM_OK)
            fail("northbound", name, "capability TLVs not built");
        got = list_json(list.tlvs, list.count);
        if (!cJSON_Compare(got, cJSON_GetObjectItemCaseSensitive(expected, "capability_tlvs"), 1))
            fail("northbound", name, "capability TLVs differ");
        cJSON_Delete(got);
        em_tlv_list_free(&list);
        em_tlv inv;
        checks++;
        if (em_inventory_tlv(view, radio, str(agent, "chipset"), &inv) != EM_OK) {
            fail("northbound", name, "inventory TLV not built");
        } else {
            got = list_json(&inv, 1);
            if (!cJSON_Compare(cJSON_GetArrayItem(got, 0), cJSON_GetObjectItemCaseSensitive(expected, "inventory_tlv"), 1))
                fail("northbound", name, "inventory TLV differs");
            cJSON_Delete(got);
            free(inv.value);
        }
        em_station_age ages[64];
        size_t nages = 0;
        const cJSON *age;
        cJSON_ArrayForEach(age, cJSON_GetObjectItemCaseSensitive(agent, "station_ages"))
        {
            em_parse_mac(age->string, ages[nages].mac);
            ages[nages++].seconds = (long)age->valuedouble;
        }
        const cJSON *sets = cJSON_GetObjectItemCaseSensitive(expected, "topology_tlvs");
        const cJSON *set;
        cJSON_ArrayForEach(set, sets)
        {
            checks++;
            bool r1 = !strcmp(set->string, "r1");
            if (em_topology_tlvs(agent_al, controller_al, radio, channel, ages, nages, r1, NULL, &list) != EM_OK) {
                fail("northbound", name, "topology TLVs not built");
                continue;
            }
            got = list_json(list.tlvs, list.count);
            if (!cJSON_Compare(got, set, 1))
                fail("northbound", name, r1 ? "topology r1 differs" : "topology 6.1 differs");
            cJSON_Delete(got);
            em_tlv_list_free(&list);
        }
        free(view);
    }
    /* the stations' ages as of each Topology Response */
    const cJSON *aged = cJSON_GetObjectItemCaseSensitive(doc, "aged_at_response");
    em_device_view *view = malloc(sizeof(*view));
    checks++;
    if (!aged || em_device_view_from_rows(cJSON_GetObjectItemCaseSensitive(aged, "ovsdb_tables"), view) != EM_OK) {
        fail("northbound", "aged_at_response", "device view");
    } else {
        uint8_t ruid[6], agent_al[6], controller_al[6];
        const cJSON *first = cJSON_GetArrayItem(cJSON_GetObjectItemCaseSensitive(doc, "cases"), 0);
        const cJSON *agent = cJSON_GetObjectItemCaseSensitive(first, "agent");
        em_parse_mac(str(agent, "radio"), ruid);
        em_parse_mac(str(agent, "al_mac"), agent_al);
        em_parse_mac(str(agent, "controller_al"), controller_al);
        const em_radio_view *radio = em_view_radio(view, ruid);
        const cJSON *response;
        cJSON_ArrayForEach(response, cJSON_GetObjectItemCaseSensitive(aged, "responses"))
        {
            double t = num(response, "now");
            em_station_age ages[64];
            size_t nages = 0;
            const cJSON *at;
            cJSON_ArrayForEach(at, cJSON_GetObjectItemCaseSensitive(aged, "associated_at"))
            {
                em_parse_mac(at->string, ages[nages].mac);
                ages[nages++].seconds = em_age_at(t, at->valuedouble);
            }
            char label[64];
            snprintf(label, sizeof(label), "aged_at_response %.1f", t);
            em_tlv_list list;
            checks++;
            if (!radio || em_topology_tlvs(agent_al, controller_al, radio, 6, ages, nages, false, NULL, &list) != EM_OK) {
                fail("northbound", label, "topology TLVs not built");
                continue;
            }
            cJSON *got = list_json(list.tlvs, list.count);
            if (!cJSON_Compare(got, cJSON_GetObjectItemCaseSensitive(response, "topology_tlvs"), 1))
                fail("northbound", label, "topology 6.1 differs");
            cJSON_Delete(got);
            em_tlv_list_free(&list);
        }
    }
    free(view);
    cJSON_Delete(doc);
}

/* -- control.json ----------------------------------------------------------------- */

static const char *record_mandate(void *ctx, const em_steering_request *r, uint16_t mid)
{
    cJSON *handed = ctx, *m = cJSON_CreateObject(), *stations = cJSON_CreateArray(),
          *targets = cJSON_CreateArray();
    char *text = em_mac_str(r->source_bssid);
    cJSON_AddBoolToObject(m, "abridged", r->abridged);
    cJSON_AddBoolToObject(m, "disassoc_imminent", r->disassoc_imminent);
    cJSON_AddNumberToObject(m, "disassoc_timer", r->disassoc_timer);
    cJSON_AddBoolToObject(m, "mandate", r->mandate);
    cJSON_AddNumberToObject(m, "mid", mid);
    cJSON_AddStringToObject(m, "source_bssid", text);
    free(text);
    for (size_t i = 0; i < r->nstations; i++) {
        text = em_mac_str(r->stations[i]);
        cJSON_AddItemToArray(stations, cJSON_CreateString(text));
        free(text);
    }
    for (size_t i = 0; i < r->ntargets; i++) {
        cJSON *t = cJSON_CreateArray();
        text = em_mac_str(r->targets[i].bssid);
        cJSON_AddItemToArray(t, cJSON_CreateString(text));
        free(text);
        cJSON_AddItemToArray(t, cJSON_CreateNumber(r->targets[i].op_class));
        cJSON_AddItemToArray(t, cJSON_CreateNumber(r->targets[i].channel));
        cJSON_AddItemToArray(targets, t);
    }
    cJSON_AddItemToObject(m, "stations", stations);
    cJSON_AddItemToObject(m, "targets", targets);
    cJSON_AddNumberToObject(m, "window", r->window);
    cJSON_AddItemToArray(handed, m);
    return NULL;
}

static char scan_timestamp[40];

static void fixed_utc(char out[40]) { memcpy(out, scan_timestamp, sizeof(scan_timestamp)); }

static double probe_wall;

static double fixed_wall(void) { return probe_wall; }

/* The pod's statistics holding a case's probe requests (spec §3.9), or NULL. */
static em_pod_stats *case_probes(const cJSON *c)
{
    const cJSON *probes = cJSON_GetObjectItemCaseSensitive(c, "probes"), *p;
    if (!probes)
        return NULL;
    em_pod_stats *s = calloc(1, sizeof(*s));
    cJSON_ArrayForEach(p, probes)
    {
        em_probe_stats *x = &s->probes[s->nprobes++];
        snprintf(x->mac, sizeof(x->mac), "%s", p->string);
        snprintf(x->band, sizeof(x->band), "%s", str(p, "band"));
        snprintf(x->ifname, sizeof(x->ifname), "%s", str(p, "ifname"));
        x->snr_db = (unsigned)num(p, "snr_db");
        x->measured_at = num(p, "measured_at");
    }
    return s;
}

/* The accepted channel policy's record, as the store would keep it. */
static bool keep_record(void *ctx, const cJSON *record)
{
    cJSON **kept = ctx;
    cJSON_Delete(*kept);
    *kept = cJSON_Duplicate(record, true);
    return true;
}

static void control_vectors(const char *dir)
{
    cJSON *doc = load(dir, "control.json");
    const cJSON *agent = cJSON_GetObjectItemCaseSensitive(doc, "agent");
    snprintf(scan_timestamp, sizeof(scan_timestamp), "%s", str(agent, "scan_timestamp"));
    probe_wall = num(agent, "probe_wall");
    em_device_view *view = malloc(sizeof(*view));
    if (em_device_view_from_rows(cJSON_GetObjectItemCaseSensitive(doc, "ovsdb_tables"), view) != EM_OK) {
        fail("control", "rows", "device view");
        free(view);
        cJSON_Delete(doc);
        return;
    }
    uint8_t ruid[6];
    em_parse_mac(str(agent, "radio"), ruid);
    const cJSON *c;
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "cases"))
    {
        const char *name = str(c, "name");
        cJSON *handed = cJSON_CreateArray();
        em_control control = {0};
        em_parse_mac(str(agent, "al_mac"), control.binding.local_al);
        em_parse_mac(str(agent, "controller_al"), control.binding.controller_al);
        memcpy(control.binding.sources[0], control.binding.controller_al, 6);
        control.binding.nsources = 1;
        control.radio = em_view_radio(view, ruid);
        control.tx_power_dbm = control.radio->tx_power;
        control.max_eirp_dbm = 30;
        control.previous_mid = (uint16_t)(num(agent, "first_mid") - 1);
        control.utc = fixed_utc;
        control.executor = record_mandate;
        control.executor_ctx = handed;
        em_pod_stats *probes = case_probes(c);
        control.stats = probes;
        control.wall = fixed_wall;
        cJSON *kept = NULL;
        control.keep_channel_policy = keep_record;
        control.keep_ctx = &kept;
        const cJSON *step;
        cJSON_ArrayForEach(step, cJSON_GetObjectItemCaseSensitive(c, "steps"))
        {
            checks++;
            cJSON *frames = cJSON_CreateArray();
            cJSON_AddItemToArray(frames, cJSON_CreateString(str(step, "request")));
            em_message *m = assemble(frames);
            cJSON_Delete(frames);
            em_frames out = {0};
            em_reason error = EM_OK;
            const char *result = m ? em_control_handle(&control, m, &out, &error) : NULL;
            const cJSON *expected = cJSON_GetObjectItemCaseSensitive(step, "expected");
            const char *want = str(expected, "result");
            if (!result && m)
                result = em_reason_name(error); /* refused: its reason, as the reference's */
            if (!result || !want || strcmp(result, want))
                fail("control", name, result ? result : "no message");
            else if (!frames_equal(&out, cJSON_GetObjectItemCaseSensitive(expected, "frames")))
                fail("control", name, "frames differ");
            em_frames_free(&out);
            em_message_free(m);
        }
        checks++;
        if (!cJSON_Compare(handed, cJSON_GetObjectItemCaseSensitive(c, "handed_to_pod"), 1))
            fail("control", name, "mandates handed to the pod differ");
        checks++;
        const cJSON *want = cJSON_GetObjectItemCaseSensitive(c, "channel_policy");
        if (cJSON_IsNull(want) ? kept != NULL : !kept || !cJSON_Compare(kept, want, 1))
            fail("control", name, "the kept channel policy differs");
        cJSON_Delete(kept);
        cJSON_Delete(handed);
        free(probes);
    }
    free(view);
    cJSON_Delete(doc);
}

/* -- translation-southbound.json, steering.json ------------------------------------- */

/* An OVSDB session over fixed rows that records every transaction (the generator's). */
static cJSON *record(void *ctx, const cJSON *operations)
{
    cJSON *sent = ctx, *results = cJSON_CreateArray();
    cJSON_AddItemToArray(sent, cJSON_Duplicate(operations, 1));
    const cJSON *o;
    cJSON_ArrayForEach(o, operations)
    {
        const char *kind = str(o, "op");
        cJSON *r = cJSON_CreateObject();
        if (!strcmp(kind, "select")) {
            cJSON_AddItemToObject(r, "rows", cJSON_CreateArray());
        } else if (!strcmp(kind, "insert")) {
            cJSON *u = cJSON_CreateArray();
            cJSON_AddItemToArray(u, cJSON_CreateString("uuid"));
            cJSON_AddItemToArray(u, cJSON_CreateString("00000000-0000-4000-8000-0000000000ff"));
            cJSON_AddItemToObject(r, "uuid", u);
        } else if (strcmp(kind, "wait")) {
            cJSON_AddNumberToObject(r, "count", 1);
        }
        cJSON_AddItemToArray(results, r);
    }
    return results;
}

static const char *passphrase(void *ctx, const char *ref)
{
    return str(ctx, ref);
}

static void southbound_vectors(const char *dir)
{
    cJSON *doc = load(dir, "translation-southbound.json");
    const cJSON *c;
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "cases"))
    {
        const char *name = str(c, "name");
        char path[1200];
        snprintf(path, sizeof(path), "%s/../../src/emosa/profiles/%s.json", dir, str(c, "profile"));
        em_profile profile;
        checks++;
        if (em_profile_load(path, &profile) != EM_OK) {
            fail("southbound", name, "profile");
            continue;
        }
        const cJSON *in = cJSON_GetObjectItemCaseSensitive(c, "intent"), *extra;
        em_ap_intent intent = {.ssid = str(in, "ssid"), .secret_ref = str(in, "secret_ref")};
        const cJSON *additional = cJSON_GetObjectItemCaseSensitive(in, "additional");
        intent.has_additional = additional != NULL;
        cJSON_ArrayForEach(extra, additional)
        {
            intent.additional[intent.nadditional].role = str(extra, "role");
            intent.additional[intent.nadditional].ssid = str(extra, "ssid");
            intent.additional[intent.nadditional].secret_ref = str(extra, "secret_ref");
            intent.nadditional++;
        }
        cJSON *sent = cJSON_CreateArray();
        em_ovs_session session = {cJSON_GetObjectItemCaseSensitive(c, "ovsdb_tables"), 1, record, sent};
        em_submit_result result;
        em_reason r = em_ap_submit(&profile, "MVXPOD023F87E628DD",
                                   cJSON_IsTrue(cJSON_GetObjectItem(c, "multi_bss")), &session,
                                   &intent, passphrase,
                                   cJSON_GetObjectItemCaseSensitive(c, "passphrases"), &result);
        const cJSON *expected = cJSON_GetObjectItemCaseSensitive(c, "expected");
        if (r != EM_OK)
            fail("southbound", name, em_reason_name(r));
        else if (strcmp(result.status, str(expected, "status")))
            fail("southbound", name, "status");
        else if (!cJSON_Compare(sent, cJSON_GetObjectItemCaseSensitive(expected, "transactions"), 1))
            fail("southbound", name, "transactions differ");
        if (getenv("EMOSA_VECTORS_DEBUG")) {
            char *text = cJSON_PrintUnformatted(sent);
            fprintf(stderr, "%s got %s\n", name, text);
            free(text);
        }
        cJSON_Delete(sent);
        em_profile_free(&profile);
    }
    cJSON_Delete(doc);

    doc = load(dir, "steering.json");
    const cJSON *in = cJSON_GetObjectItemCaseSensitive(doc, "intent");
    em_steering_intent intent = {0};
    snprintf(intent.station, sizeof(intent.station), "%s", str(in, "station"));
    snprintf(intent.source_bssid, sizeof(intent.source_bssid), "%s", str(in, "source_bssid"));
    snprintf(intent.target_bssid, sizeof(intent.target_bssid), "%s", str(in, "target_bssid"));
    intent.op_class = (int)num(in, "op_class");
    intent.channel = (int)num(in, "channel");
    intent.window = (int)num(in, "window");
    intent.disassoc_imminent = cJSON_IsTrue(cJSON_GetObjectItem(in, "disassoc_imminent"));
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "cases"))
    {
        const char *name = str(c, "name");
        cJSON *sent = cJSON_CreateArray();
        em_ovs_session session = {cJSON_GetObjectItemCaseSensitive(c, "ovsdb_tables"), 1, record, sent};
        em_submit_result result;
        em_steering_rows created;
        checks++;
        em_reason r = em_steering_open("MVXPOD023F87E628DD", &session, &intent, &result, &created);
        if (r == EM_OK)
            r = em_steering_kick("MVXPOD023F87E628DD", &session, intent.station, created.client);
        if (r == EM_OK)
            r = em_steering_close(&session, &intent, &created);
        const cJSON *expected = cJSON_GetObjectItemCaseSensitive(c, "expected");
        if (r != EM_OK || strcmp(result.status, str(expected, "status")) || cJSON_GetArraySize(sent) != 3)
            fail("steering", name, r != EM_OK ? em_reason_name(r) : "status or transaction count");
        else if (!cJSON_Compare(cJSON_GetArrayItem(sent, 0), cJSON_GetObjectItemCaseSensitive(expected, "open"), 1) ||
                 !cJSON_Compare(cJSON_GetArrayItem(sent, 1), cJSON_GetObjectItemCaseSensitive(expected, "kick"), 1) ||
                 !cJSON_Compare(cJSON_GetArrayItem(sent, 2), cJSON_GetObjectItemCaseSensitive(expected, "close"), 1))
            fail("steering", name, "transactions differ");
        cJSON_Delete(sent);
    }
    cJSON_Delete(doc);
}

static const char *ovs_str_first_serial(const cJSON *tables)
{
    const cJSON *nodes = cJSON_GetObjectItemCaseSensitive(tables, "AWLAN_Node");
    return nodes && nodes->child ? ovs_str(nodes->child, "serial_number") : "";
}

/* -- uplink.json -------------------------------------------------------------------- */

static void uplink_vectors(const char *dir)
{
    cJSON *doc = load(dir, "uplink.json");
    const cJSON *c;
    uint8_t agent_al[6], controller_al[6];
    em_parse_mac("02:72:f9:7f:07:85", agent_al);
    em_parse_mac("02:00:00:e0:00:01", controller_al);
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "states"))
    {
        const char *name = str(c, "name"), *station = str(c, "station");
        const cJSON *tables = cJSON_GetObjectItemCaseSensitive(c, "ovsdb_tables");
        const cJSON *expected = cJSON_GetObjectItemCaseSensitive(c, "expected");
        em_uplink_state state;
        em_uplink_state_of(tables, station, &state);
        cJSON *got = em_uplink_state_json(&state);
        checks++;
        if (!cJSON_Compare(got, cJSON_GetObjectItemCaseSensitive(expected, "uplink"), 1))
            fail("uplink", name, "uplink state differs");
        cJSON_Delete(got);
        em_device_view *view = malloc(sizeof(*view));
        em_backhaul link;
        const cJSON *want = cJSON_GetObjectItemCaseSensitive(expected, "backhaul");
        bool has = strcmp(state.kind, "multi-ap") == 0 &&
                   em_device_view_from_rows(tables, view) == EM_OK &&
                   em_view_backhaul(view, station, &link);
        checks++;
        if (has != !cJSON_IsNull(want)) {
            fail("uplink", name, "backhaul presence");
        } else if (has) {
            got = em_backhaul_json(&link);
            if (!cJSON_Compare(got, cJSON_GetObjectItemCaseSensitive(want, "view"), 1))
                fail("uplink", name, "backhaul view differs");
            cJSON_Delete(got);
            const em_radio_view *radio = em_view_radio(view, link.ruid);
            em_station_age ages[64];
            size_t nages = 0;
            for (size_t i = 0; i < radio->nbss; i++)
                for (size_t k = 0; k < radio->bss[i].nstations; k++) {
                    memcpy(ages[nages].mac, radio->bss[i].stations[k], 6);
                    ages[nages++].seconds = 0;
                }
            em_tlv_list list;
            checks++;
            if (em_topology_tlvs(agent_al, controller_al, radio, radio->channel, ages, nages, false,
                                 &link, &list) != EM_OK) {
                fail("uplink", name, "topology TLVs not built");
            } else {
                got = list_json(list.tlvs, list.count);
                if (!cJSON_Compare(got, cJSON_GetObjectItemCaseSensitive(want, "topology_tlvs"), 1))
                    fail("uplink", name, "topology TLVs differ");
                cJSON_Delete(got);
                em_tlv_list_free(&list);
            }
            uint8_t cap[13];
            memcpy(cap, link.ruid, 6);
            cap[6] = 0x80;
            memcpy(cap + 7, link.station.mac, 6);
            em_tlv t = {0xCB, 13, cap};
            got = list_json(&t, 1);
            checks++;
            if (!cJSON_Compare(got, cJSON_GetObjectItemCaseSensitive(want, "backhaul_sta_capability_tlvs"), 1))
                fail("uplink", name, "backhaul STA capability differs");
            cJSON_Delete(got);
        }
        free(view);
    }
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "switch"))
    {
        const char *name = str(c, "name");
        const cJSON *in = cJSON_GetObjectItemCaseSensitive(c, "intent");
        const cJSON *expected = cJSON_GetObjectItemCaseSensitive(c, "expected");
        const cJSON *tables = cJSON_GetObjectItemCaseSensitive(c, "ovsdb_tables");
        em_uplink_intent intent = {str(in, "station"), str(in, "ssid"), str(in, "secret_ref"),
                                   str(in, "bssid")};
        cJSON *sent = cJSON_CreateArray();
        em_ovs_session session = {tables, 1, record, sent};
        em_submit_result result;
        const char *refusal;
        const char *serial = ovs_str_first_serial(tables);
        em_reason r = em_uplink_submit(serial, &session, &intent, passphrase,
                                       cJSON_GetObjectItemCaseSensitive(c, "passphrases"), &result,
                                       &refusal);
        const char *status = r == EM_OK ? result.status : em_reason_name(r);
        checks++;
        if (strcmp(status, str(expected, "status")) ||
            (str(expected, "refusal") && (!refusal || strcmp(refusal, str(expected, "refusal")))))
            fail("uplink", name, refusal ? refusal : status);
        else if (!cJSON_Compare(sent, cJSON_GetObjectItemCaseSensitive(expected, "transactions"), 1))
            fail("uplink", name, "transactions differ");
        cJSON_Delete(sent);
    }
    cJSON_Delete(doc);
}

/* -- al-mac.json, operation-transitions.json, fleet.json ---------------------------- */

static int cmp_str(const void *a, const void *b)
{
    return strcmp(*(const char *const *)a, *(const char *const *)b);
}

static void fleet_vectors(const char *dir)
{
    cJSON *doc = load(dir, "al-mac.json");
    const cJSON *c;
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "cases"))
    {
        const char *taken[64];
        size_t n = 0;
        const cJSON *t;
        cJSON_ArrayForEach(t, cJSON_GetObjectItemCaseSensitive(c, "taken")) taken[n++] = t->valuestring;
        char al[18];
        checks++;
        if (!em_derive_al(str(c, "serial"), taken, n, al) || strcmp(al, str(c, "expected")))
            fail("al-mac", str(c, "serial"), al);
    }
    cJSON_Delete(doc);

    doc = load(dir, "operation-transitions.json");
    cJSON *active = cJSON_CreateArray(), *transitions = cJSON_CreateObject();
    const char *names[EM_OP_COUNT];
    size_t nactive = 0;
    for (int st = 0; st < EM_OP_COUNT; st++)
        if (em_op_active((em_op_state)st))
            names[nactive++] = em_op_state_name((em_op_state)st);
    qsort(names, nactive, sizeof(*names), cmp_str);
    for (size_t i = 0; i < nactive; i++)
        cJSON_AddItemToArray(active, cJSON_CreateString(names[i]));
    for (int from = 0; from < EM_OP_COUNT; from++) {
        size_t n = 0;
        for (int to = 0; to < EM_OP_COUNT; to++)
            if (em_op_may_transition((em_op_state)from, (em_op_state)to))
                names[n++] = em_op_state_name((em_op_state)to);
        if (!n)
            continue;
        qsort(names, n, sizeof(*names), cmp_str);
        cJSON *list = cJSON_CreateArray();
        for (size_t i = 0; i < n; i++)
            cJSON_AddItemToArray(list, cJSON_CreateString(names[i]));
        cJSON_AddItemToObject(transitions, em_op_state_name((em_op_state)from), list);
    }
    checks++;
    if (!cJSON_Compare(active, cJSON_GetObjectItemCaseSensitive(doc, "active"), 1) ||
        !cJSON_Compare(transitions, cJSON_GetObjectItemCaseSensitive(doc, "transitions"), 1))
        fail("operation-transitions", "table", "differs");
    cJSON_Delete(active);
    cJSON_Delete(transitions);
    cJSON_Delete(doc);

    doc = load(dir, "fleet.json");
    const cJSON *config = cJSON_GetObjectItemCaseSensitive(doc, "fleet_config");
    em_registry *registry = calloc(1, sizeof(*registry));
    const cJSON *ports = cJSON_GetObjectItemCaseSensitive(config, "ports");
    registry->port_low = cJSON_GetArrayItem(ports, 0)->valueint;
    registry->port_high = cJSON_GetArrayItem(ports, 1)->valueint;
    snprintf(registry->reserved_al, sizeof(registry->reserved_al), "%s", str(config, "controller_al"));
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "cases"))
    {
        const cJSON *node = cJSON_GetObjectItemCaseSensitive(c, "awlan_node");
        const cJSON *expected = cJSON_GetObjectItemCaseSensitive(c, "expected");
        const char *serial = str(node, "serial_number");
        em_fleet_entry *e = em_registry_assign(registry, serial, str(node, "id"), str(node, "model"),
                                               str(node, "firmware_version"), 0);
        checks++;
        if (!e) {
            fail("fleet", serial, "no agent");
            continue;
        }
        cJSON *entry = cJSON_CreateObject();
        cJSON_AddStringToObject(entry, "al_mac", e->al_mac);
        cJSON_AddNumberToObject(entry, "handovers", e->handovers);
        cJSON_AddStringToObject(entry, "interface", e->interface);
        cJSON_AddStringToObject(entry, "pod_id", e->pod_id);
        cJSON_AddNumberToObject(entry, "port", e->port);
        cJSON *agent = em_agent_config(e, config), *update = em_manager_update(e, config);
        cJSON *rows = cJSON_CreateArray();
        cJSON_AddItemToArray(rows, cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(update, "row"), 1));
        if (!cJSON_Compare(entry, cJSON_GetObjectItemCaseSensitive(expected, "registry_entry"), 1))
            fail("fleet", serial, "registry entry differs");
        else if (!cJSON_Compare(agent, cJSON_GetObjectItemCaseSensitive(expected, "agent_config"), 1))
            fail("fleet", serial, "agent configuration differs");
        else if (!cJSON_Compare(rows, cJSON_GetObjectItemCaseSensitive(expected, "manager_addr_update"), 1))
            fail("fleet", serial, "manager_addr update differs");
        cJSON_Delete(entry);
        cJSON_Delete(agent);
        cJSON_Delete(update);
        cJSON_Delete(rows);
    }
    em_registry_free(registry);
    free(registry);
    cJSON_Delete(doc);
}

/* -- gtp.json ---------------------------------------------------------------------------- */

static void remove_tree(const char *path); /* (fleet-sessions.json) */

typedef struct {
    const cJSON *calls; /* [[args], check, output] in the order the reference made them */
    int next;
    char problem[256];
} ip_replay;

static char *replay_ip(void *ctx, const char *const *args, size_t n, bool check)
{
    ip_replay *r = ctx;
    const cJSON *call = cJSON_GetArrayItem(r->calls, r->next++);
    const cJSON *want = cJSON_GetArrayItem(call, 0);
    bool same = call && (size_t)cJSON_GetArraySize(want) == n &&
                cJSON_IsTrue(cJSON_GetArrayItem(call, 1)) == check;
    for (size_t i = 0; same && i < n; i++)
        same = !strcmp(cJSON_GetStringValue(cJSON_GetArrayItem(want, (int)i)), args[i]);
    if (!same && !r->problem[0]) {
        size_t used = (size_t)snprintf(r->problem, sizeof(r->problem), "call %d: ip", r->next - 1);
        for (size_t i = 0; i < n && used < sizeof(r->problem); i++)
            used += (size_t)snprintf(r->problem + used, sizeof(r->problem) - used, " %s", args[i]);
    }
    const char *output = cJSON_GetStringValue(cJSON_GetArrayItem(call, 2));
    return strdup(output ? output : "");
}

static void gtp_vectors(const char *dir)
{
    cJSON *doc = load(dir, "gtp.json");
    const cJSON *c;
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "checks"))
    {
        char why[256] = "";
        bool ok = em_gtp_check(cJSON_GetObjectItemCaseSensitive(c, "config"), why, sizeof(why));
        const char *error = str(c, "error");
        checks++;
        if (ok != !error || (error && strcmp(why, error)))
            fail("gtp", "check", ok ? "accepted" : why);
    }
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "tunnel_names"))
    {
        char name[16];
        checks++;
        if (!em_gtp_tunnel_name(cJSON_GetArrayItem(c, 0)->valuestring, name) ||
            strcmp(name, cJSON_GetArrayItem(c, 1)->valuestring))
            fail("gtp", "tunnel name", cJSON_GetArrayItem(c, 0)->valuestring);
    }
    char *conf = em_gtp_dnsmasq(cJSON_GetObjectItemCaseSensitive(doc, "config"));
    checks++;
    if (!conf || strcmp(conf, str(doc, "dnsmasq")))
        fail("gtp", "dnsmasq", "configuration differs");
    free(conf);
    const cJSON *session;
    cJSON_ArrayForEach(session, cJSON_GetObjectItemCaseSensitive(doc, "sessions"))
    {
        const char *name = str(session, "name");
        char root[] = "gtp-session-XXXXXX", full[1024], path[1200];
        if (!mkdtemp(root) || !realpath(root, full)) {
            fail("gtp", name, "no scratch directory");
            continue;
        }
        cJSON *config = cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(doc, "config"), 1);
        cJSON_ReplaceItemInObjectCaseSensitive(config, "state_dir", cJSON_CreateString(full));
        snprintf(path, sizeof(path), "%s/leases", full);
        em_write_file(path, str(session, "leases"), false);
        ip_replay replay = {0};
        em_gtp g;
        char why[512] = "";
        if (!em_gtp_init(&g, config, (em_ip){replay_ip, &replay}, why, sizeof(why))) {
            fail("gtp", name, why);
            cJSON_Delete(config);
            remove_tree(full);
            continue;
        }
        const cJSON *step;
        int index = 0;
        cJSON_ArrayForEach(step, cJSON_GetObjectItemCaseSensitive(session, "steps"))
        {
            char where[160];
            snprintf(where, sizeof(where), "%s step %d", name, index++);
            const cJSON *command = cJSON_GetObjectItemCaseSensitive(step, "command");
            const char *verb = cJSON_GetArrayItem(command, 0)->valuestring;
            replay = (ip_replay){.calls = cJSON_GetObjectItemCaseSensitive(step, "calls")};
            why[0] = 0;
            cJSON *result = NULL;
            if (!strcmp(verb, "setup")) {
                snprintf(path, sizeof(path), "%s/gtp.json", full);
                result = em_gtp_setup(&g, path, "/opt/emosa-adapter/bin/emosa-gtp-c", why, sizeof(why));
            } else if (!strcmp(verb, "lease")) {
                result = em_gtp_lease(&g, cJSON_GetArrayItem(command, 1)->valuestring,
                                      cJSON_GetArrayItem(command, 2)->valuestring,
                                      cJSON_GetArrayItem(command, 3)->valuestring, why, sizeof(why));
            } else if (!strcmp(verb, "reconcile")) {
                result = em_gtp_reconcile(&g, why, sizeof(why));
            } else {
                result = em_gtp_list(&g, why, sizeof(why));
            }
            const char *error = str(step, "error");
            checks++;
            if (replay.problem[0])
                fail("gtp", where, replay.problem);
            else if (replay.next != cJSON_GetArraySize(replay.calls))
                fail("gtp", where, "fewer iproute2 calls");
            else if (error ? (result || strcmp(why, error)) : !result)
                fail("gtp", where, result ? "accepted" : why);
            else if (result && !cJSON_Compare(result, cJSON_GetObjectItemCaseSensitive(step, "result"), 1))
                fail("gtp", where, "the tunnels differ");
            cJSON_Delete(result);
        }
        cJSON_Delete(config);
        remove_tree(full);
    }
    cJSON_Delete(doc);
}

/* -- fleet-sessions.json --------------------------------------------------------------- */

/* text with every `from` replaced by `to` (caller frees) */
static char *subst(const char *text, const char *from, const char *to)
{
    size_t nf = strlen(from), nt = strlen(to), n = 0;
    for (const char *p = strstr(text, from); p; p = strstr(p + nf, from))
        n++;
    char *out = malloc(strlen(text) + n * nt + 1), *w = out;
    const char *r = text;
    for (const char *p = strstr(r, from); p; p = strstr(r, from)) {
        memcpy(w, r, (size_t)(p - r));
        w += p - r;
        memcpy(w, to, nt);
        w += nt;
        r = p + nf;
    }
    strcpy(w, r);
    return out;
}

/* a JSON value with the placeholder replaced in every string */
static cJSON *subst_json(const cJSON *value, const char *from, const char *to)
{
    char *text = cJSON_PrintUnformatted(value), *done = subst(text, from, to);
    cJSON *out = cJSON_Parse(done);
    free(text);
    free(done);
    return out;
}

static void remove_tree(const char *path)
{
    DIR *d = opendir(path);
    struct dirent *e;
    while (d && (e = readdir(d))) {
        if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, ".."))
            continue;
        char child[2048];
        snprintf(child, sizeof(child), "%s/%s", path, e->d_name);
        struct stat st;
        if (!lstat(child, &st) && S_ISDIR(st.st_mode))
            remove_tree(child);
        else
            unlink(child);
    }
    if (d)
        closedir(d);
    rmdir(path);
}

static char *slurp(const char *path)
{
    return em_read_file(path, 16 * 1024 * 1024, NULL);
}

static void record_stop(void *ctx, const char *pod_id)
{
    cJSON_AddItemToArray((cJSON *)ctx, cJSON_CreateString(pod_id));
}

static void fleet_session_vectors(const char *dir)
{
    cJSON *doc = load(dir, "fleet-sessions.json");
    const char *placeholder = str(doc, "root"), *stamp = str(doc, "stamp");
    const cJSON *session;
    cJSON_ArrayForEach(session, cJSON_GetObjectItemCaseSensitive(doc, "sessions"))
    {
        const char *name = str(session, "name");
        char root[] = "fleet-session-XXXXXX";
        if (!mkdtemp(root)) {
            fail("fleet-sessions", name, "no scratch directory");
            continue;
        }
        char full[1024];
        if (!realpath(root, full)) {
            fail("fleet-sessions", name, "no scratch directory");
            continue;
        }
        em_fleet fleet = {0};
        bool open = false;
        const cJSON *step;
        int index = 0;
        cJSON_ArrayForEach(step, cJSON_GetObjectItemCaseSensitive(session, "steps"))
        {
            char where[160];
            snprintf(where, sizeof(where), "%s step %d", name, index++);
            const cJSON *expected = cJSON_GetObjectItemCaseSensitive(step, "expected");
            if (cJSON_GetObjectItemCaseSensitive(step, "fleet_config")) {
                if (open)
                    em_fleet_close(&fleet);
                cJSON *config = subst_json(cJSON_GetObjectItemCaseSensitive(step, "fleet_config"), placeholder, full);
                em_mkdirs(str(config, "state_root"), 0700);
                em_mkdirs(str(config, "config_dir"), 0755);
                char why[600];
                open = em_fleet_open(&fleet, config, why, sizeof(why));
                checks++;
                if (!open) {
                    fail("fleet-sessions", where, why);
                    em_fleet_close(&fleet);
                    break;
                }
            } else if (str(step, "mkdir")) {
                char path[2048];
                snprintf(path, sizeof(path), "%s/%s", full, str(step, "mkdir"));
                em_mkdirs(path, 0755);
            } else if (cJSON_GetObjectItemCaseSensitive(step, "arrival")) {
                em_fleet_handover h;
                em_fleet_outcome outcome = em_fleet_identify(
                    &fleet, cJSON_GetObjectItemCaseSensitive(step, "arrival"), num(step, "now"), &h);
                static const char *const names[] = {"handover", "refused", "invalid", "failed"};
                cJSON *start = cJSON_CreateNull(), *update = cJSON_CreateNull();
                if (outcome == EM_FLEET_HANDOVER) {
                    cJSON_Delete(start);
                    start = cJSON_CreateArray();
                    cJSON_AddItemToArray(start, cJSON_CreateString(h.entry.pod_id));
                    cJSON_AddItemToArray(start, cJSON_CreateBool(h.changed));
                    cJSON_Delete(update);
                    update = cJSON_Duplicate(h.update, 1);
                }
                checks++;
                if (strcmp(names[outcome], str(expected, "outcome")))
                    fail("fleet-sessions", where, names[outcome]);
                else if (!cJSON_Compare(start, cJSON_GetObjectItemCaseSensitive(expected, "start"), 1))
                    fail("fleet-sessions", where, "the agent's start differs");
                else if (!cJSON_Compare(update, cJSON_GetObjectItemCaseSensitive(expected, "update"), 1))
                    fail("fleet-sessions", where, "the handover differs");
                cJSON_Delete(start);
                cJSON_Delete(update);
                em_fleet_handover_clear(&h);
            } else if (str(step, "forget_elsewhere")) {
                /* another fleet process on the same files, while this one serves */
                em_fleet other;
                char why[600];
                cJSON *config = cJSON_Duplicate(fleet.config, 1);
                cJSON *stopped = cJSON_CreateArray();
                if (em_fleet_open(&other, config, why, sizeof(why)))
                    cJSON_Delete(em_fleet_forget(&other, str(step, "forget_elsewhere"), stamp, record_stop, stopped));
                em_fleet_close(&other);
                cJSON_Delete(stopped);
            } else if (str(step, "forget")) {
                cJSON *stopped = cJSON_CreateArray();
                cJSON *entry = em_fleet_forget(&fleet, str(step, "forget"), stamp, record_stop, stopped);
                cJSON *want = subst_json(cJSON_GetObjectItemCaseSensitive(expected, "entry"), placeholder, full);
                if (!entry)
                    entry = cJSON_CreateNull();
                checks++;
                if (!cJSON_Compare(entry, want, 1))
                    fail("fleet-sessions", where, "the released entry differs");
                else if (!cJSON_Compare(stopped, cJSON_GetObjectItemCaseSensitive(expected, "stopped"), 1))
                    fail("fleet-sessions", where, "the agents stopped differ");
                cJSON_Delete(entry);
                cJSON_Delete(want);
                cJSON_Delete(stopped);
            }
        }
        if (open) {
            const cJSON *files = cJSON_GetObjectItemCaseSensitive(session, "files"), *config;
            char *want = subst(str(files, "registry"), placeholder, full), *got = slurp(fleet.registry_path);
            checks++;
            if (!got || strcmp(got, want))
                fail("fleet-sessions", name, "the registry file differs");
            free(want);
            free(got);
            cJSON_ArrayForEach(config, cJSON_GetObjectItemCaseSensitive(files, "configs"))
            {
                char path[2048];
                snprintf(path, sizeof(path), "%s/%s.json", fleet.config_dir, config->string);
                want = subst(config->valuestring, placeholder, full);
                got = slurp(path);
                checks++;
                if (!got || strcmp(got, want))
                    fail("fleet-sessions", name, config->string);
                free(want);
                free(got);
            }
            const cJSON *archive;
            cJSON_ArrayForEach(archive, cJSON_GetObjectItemCaseSensitive(files, "archives"))
            {
                char path[2048];
                struct stat st;
                snprintf(path, sizeof(path), "%s/%s", fleet.state_root, archive->valuestring);
                checks++;
                if (stat(path, &st) || !S_ISDIR(st.st_mode))
                    fail("fleet-sessions", name, archive->valuestring);
            }
            em_fleet_close(&fleet);
        }
        remove_tree(full);
    }
    cJSON_Delete(doc);
}

/* -- telemetry.json ----------------------------------------------------------------- */

static double stats_clock(void *ctx) { return *(double *)ctx; }

static void telemetry_vectors(const char *dir)
{
    cJSON *doc = load(dir, "telemetry.json");
    double clock = num(doc, "clock");
    em_pod_stats *stats = malloc(sizeof(*stats));
    em_pod_stats_init(stats, str(doc, "topic"), (unsigned)num(doc, "reporting_interval"), stats_clock, &clock);
    const cJSON *step;
    int index = 0;
    cJSON_ArrayForEach(step, cJSON_GetObjectItemCaseSensitive(doc, "steps"))
    {
        char name[24];
        snprintf(name, sizeof(name), "step %d", index++);
        em_buf payload = {0};
        em_unhex(str(step, "payload"), &payload);
        bool accepted = em_pod_stats_receive(stats, str(step, "topic"), payload.data, payload.len,
                                             cJSON_IsTrue(cJSON_GetObjectItem(step, "retained")));
        em_buf_free(&payload);
        const cJSON *expected = cJSON_GetObjectItemCaseSensitive(step, "expected");
        cJSON *status = em_pod_stats_status(stats);
        cJSON_DeleteItemFromObjectCaseSensitive(status, "last_report_at");
        checks++;
        if (accepted != cJSON_IsTrue(cJSON_GetObjectItem(expected, "accepted")))
            fail("telemetry", name, "accepted differs");
        else if (!cJSON_Compare(status, cJSON_GetObjectItemCaseSensitive(expected, "status"), 1)) {
            fail("telemetry", name, "status differs");
            if (getenv("EMOSA_VECTORS_DEBUG")) {
                char *text = cJSON_PrintUnformatted(status);
                fprintf(stderr, "%s\n", text);
                free(text);
            }
        }
        cJSON_Delete(status);
    }
    free(stats);
    cJSON_Delete(doc);
}

/* -- metrics.json: the Multi-AP Policy kept, AP metrics from the pod's statistics -------- */

static double metrics_wall_value;
static double metrics_wall(void) { return metrics_wall_value; }
static uint16_t metrics_mid(void *ctx) { return ++*(uint16_t *)ctx; }

static cJSON *counts_of(const cJSON *status)
{
    return cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(status, "counts"), 1);
}

static void metrics_vectors(const char *dir)
{
    cJSON *doc = load(dir, "metrics.json");
    const cJSON *agent = cJSON_GetObjectItemCaseSensitive(doc, "agent");
    metrics_wall_value = num(agent, "wall");
    double stats_now = metrics_wall_value;
    em_device_view *view = malloc(sizeof(*view));
    if (em_device_view_from_rows(cJSON_GetObjectItemCaseSensitive(doc, "ovsdb_tables"), view) != EM_OK) {
        fail("metrics", "rows", "device view");
        free(view);
        cJSON_Delete(doc);
        return;
    }
    uint8_t ruid[6], controller[6], al[6], esp[3];
    em_parse_mac(str(agent, "radio"), ruid);
    em_parse_mac(str(agent, "controller_al"), controller);
    em_parse_mac(str(agent, "al_mac"), al);
    em_buf esp_hex = {0};
    em_unhex(str(agent, "esp_be"), &esp_hex);
    memcpy(esp, esp_hex.data, 3);
    em_buf_free(&esp_hex);
    const em_radio_view *radio = em_view_radio(view, ruid);
    const cJSON *c;
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "cases"))
    {
        const char *name = str(c, "name");
        char path[] = "/tmp/emosa-vectors-XXXXXX", db[64];
        if (!mkdtemp(path)) {
            fail("metrics", name, "temporary directory");
            continue;
        }
        snprintf(db, sizeof(db), "%s/policy.sqlite", path);
        em_policy_store *store = em_policy_store_open(db, "conformance");
        em_reporting r = {0};
        em_reporting_start(&r, store, controller, al, ruid);
        em_pod_stats *stats = malloc(sizeof(*stats));
        em_pod_stats_init(stats, str(agent, "topic"), (unsigned)num(agent, "reporting_interval"), stats_clock,
                          &stats_now);
        const cJSON *p;
        cJSON_ArrayForEach(p, cJSON_GetObjectItemCaseSensitive(c, "publishes"))
        {
            em_buf payload = {0};
            em_unhex(p->valuestring, &payload);
            em_pod_stats_receive(stats, str(agent, "topic"), payload.data, payload.len, false);
            em_buf_free(&payload);
        }
        em_metric_source source = {radio, stats, esp, num(agent, "freshness"), metrics_wall};
        uint16_t mid = (uint16_t)(num(agent, "first_mid") - 1);
        const cJSON *step;
        int index = 0;
        cJSON_ArrayForEach(step, cJSON_GetObjectItemCaseSensitive(c, "steps"))
        {
            char label[96];
            snprintf(label, sizeof(label), "%s step %d", name, index++);
            checks++;
            double at = num(step, "at");
            em_frames out = {0};
            em_reason error = EM_OK;
            const char *result = NULL;
            if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(step, "tick"))) {
                em_reporting_tick(&r, at, true, &source, metrics_mid, &mid, &out);
            } else {
                cJSON *frames = cJSON_CreateArray();
                cJSON_AddItemToArray(frames, cJSON_CreateString(str(step, "request")));
                em_message *m = assemble(frames);
                cJSON_Delete(frames);
                if (m && m->message_type == 0x8003)
                    result = em_reporting_policy(&r, m, at, true, &source, metrics_mid, &mid, &out, &error);
                else if (m && m->message_type == 0x800B)
                    result = em_reporting_query(&r, m, true, &source, &out, &error);
                if (!result && m && error != EM_OK)
                    result = em_reason_name(error); /* refused: its reason, as the reference's */
                em_message_free(m);
            }
            const cJSON *expected = cJSON_GetObjectItemCaseSensitive(step, "expected");
            const char *want = str(expected, "result");
            if ((want == NULL) != (result == NULL) || (want && strcmp(want, result)))
                fail("metrics", label, result ? result : em_reason_name(error));
            else if (!frames_equal(&out, cJSON_GetObjectItemCaseSensitive(expected, "frames")))
                fail("metrics", label, "frames differ");
            em_frames_free(&out);
        }
        const cJSON *counts = cJSON_GetObjectItemCaseSensitive(c, "expected_counts");
        cJSON *policy_status = em_reporting_status(&r), *reporter_status = em_reporting_metrics_status(&r, &source);
        cJSON *pc = counts_of(policy_status), *rc = counts_of(reporter_status);
        checks++;
        if (!cJSON_Compare(pc, cJSON_GetObjectItemCaseSensitive(counts, "policy"), 1) ||
            !cJSON_Compare(rc, cJSON_GetObjectItemCaseSensitive(counts, "reporter"), 1))
            fail("metrics", name, "counts differ");
        checks++;
        cJSON *kept = r.value ? cJSON_Duplicate(r.value, 1) : cJSON_CreateNull();
        cJSON_DeleteItemFromObjectCaseSensitive(kept, "boot_id");
        if (!cJSON_Compare(kept, cJSON_GetObjectItemCaseSensitive(c, "expected_policy"), 1)) {
            fail("metrics", name, "kept policy differs");
            if (getenv("EMOSA_VECTORS_DEBUG")) {
                char *text = cJSON_PrintUnformatted(kept);
                fprintf(stderr, "%s\n", text);
                free(text);
            }
        }
        cJSON_Delete(kept);
        cJSON_Delete(pc);
        cJSON_Delete(rc);
        cJSON_Delete(policy_status);
        cJSON_Delete(reporter_status);
        cJSON_Delete(r.value);
        em_policy_store_close(store);
        free(stats);
        char cmd[128];
        snprintf(cmd, sizeof(cmd), "rm -rf %s", path);
        if (system(cmd)) {
        }
    }
    free(view);
    cJSON_Delete(doc);
}

/* -- backhaul-steering.json ---------------------------------------------------------------- */

typedef struct {
    const cJSON *step;
    cJSON *handed;
} bh_script;

static const char *bh_executor(void *ctx, const char *bssid)
{
    bh_script *s = ctx;
    cJSON_AddItemToArray(s->handed, cJSON_CreateString(bssid));
    const cJSON *reply = cJSON_GetObjectItemCaseSensitive(s->step, "executor");
    return cJSON_IsString(reply) ? reply->valuestring : NULL;
}

static int bh_outcome(void *ctx, const char *bssid, const char **why)
{
    bh_script *s = ctx;
    (void)bssid;
    const cJSON *o = cJSON_GetObjectItemCaseSensitive(s->step, "outcome");
    if (cJSON_IsTrue(o))
        return 1;
    if (cJSON_IsString(o)) {
        *why = o->valuestring;
        return -1;
    }
    return 0;
}

static void backhaul_steering_vectors(const char *dir)
{
    cJSON *doc = load(dir, "backhaul-steering.json");
    const cJSON *agent = cJSON_GetObjectItemCaseSensitive(doc, "agent");
    uint8_t controller[6], al[6], stations[4][6];
    em_parse_mac(str(agent, "controller_al"), controller);
    em_parse_mac(str(agent, "al_mac"), al);
    size_t nstations = 0;
    const cJSON *m;
    cJSON_ArrayForEach(m, cJSON_GetObjectItemCaseSensitive(doc, "backhaul_stations"))
    em_parse_mac(m->valuestring, stations[nstations++]);
    const cJSON *c;
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "cases"))
    {
        const char *name = str(c, "name");
        em_bh_shared shared = {0};
        em_bh_coordinator sessions[4] = {0};
        bool started[4] = {0};
        int last = 0;
        bh_script script = {NULL, cJSON_CreateArray()};
        const cJSON *step;
        int index = 0;
        cJSON_ArrayForEach(step, cJSON_GetObjectItemCaseSensitive(c, "steps"))
        {
            char label[96];
            snprintf(label, sizeof(label), "%s step %d", name, index++);
            int k = (int)num(step, "session");
            last = k > last ? k : last;
            if (!started[k]) {
                em_bh_start(&sessions[k], &shared, controller, al, bh_executor, bh_outcome, &script);
                started[k] = true;
            }
            script.step = step;
            double at = num(step, "at");
            em_frames out = {0};
            em_reason error = EM_OK;
            const char *result = NULL;
            checks++;
            if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(step, "close"))) {
                em_bh_close(&sessions[k]);
            } else if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(step, "tick"))) {
                /* a tick's result is the event it records, if any: the counter that grew */
                unsigned before[16] = {0};
                size_t n = sessions[k].counts.n;
                for (size_t i = 0; i < n; i++)
                    before[i] = sessions[k].counts.items[i].count;
                em_bh_tick(&sessions[k], at, cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(step, "source_available")), &out);
                for (size_t i = 0; i < sessions[k].counts.n && !result; i++)
                    if (i >= n || sessions[k].counts.items[i].count > before[i])
                        result = sessions[k].counts.items[i].name;
            } else {
                cJSON *frames = cJSON_CreateArray();
                cJSON_AddItemToArray(frames, cJSON_CreateString(str(step, "request")));
                em_message *msg = assemble(frames);
                cJSON_Delete(frames);
                result = msg ? em_bh_handle(&sessions[k], msg, at, (const uint8_t(*)[6])stations, nstations, &out, &error)
                             : NULL;
                em_message_free(msg);
            }
            const cJSON *expected = cJSON_GetObjectItemCaseSensitive(step, "expected");
            const char *want = str(expected, "result");
            if ((want == NULL) != (result == NULL) || (want && strcmp(want, result)))
                fail("backhaul-steering", label, result ? result : em_reason_name(error));
            else if (!frames_equal(&out, cJSON_GetObjectItemCaseSensitive(expected, "frames")))
                fail("backhaul-steering", label, "frames differ");
            em_frames_free(&out);
        }
        checks++;
        if (!cJSON_Compare(script.handed, cJSON_GetObjectItemCaseSensitive(c, "handed_to_uplink"), 1))
            fail("backhaul-steering", name, "moves handed to the uplink scope differ");
        checks++;
        cJSON *status = em_bh_status(&sessions[last]);
        if (!cJSON_Compare(status, cJSON_GetObjectItemCaseSensitive(c, "expected_status"), 1)) {
            fail("backhaul-steering", name, "status differs");
            if (getenv("EMOSA_VECTORS_DEBUG")) {
                char *text = cJSON_PrintUnformatted(status);
                fprintf(stderr, "%s\n", text);
                free(text);
            }
        }
        cJSON_Delete(status);
        cJSON_Delete(script.handed);
        for (int i = 0; i < 4; i++)
            cJSON_Delete(sessions[i].last);
    }
    cJSON_Delete(doc);
}

/* -- scope-writes.json: the scopes' guarded OVSDB writes ------------------------------------ */

static void scope_writes_vectors(const char *dir)
{
    cJSON *doc = load(dir, "scope-writes.json");
    const char *serial = str(doc, "serial");
    const cJSON *c;
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "cases"))
    {
        const char *name = str(c, "name"), *scope = str(c, "scope");
        em_ovsdb *ovs = em_ovsdb_fixed(cJSON_GetObjectItemCaseSensitive(c, "ovsdb_tables"), 1, "conformance");
        const cJSON *intent = cJSON_GetObjectItemCaseSensitive(c, "intent");
        cJSON *sent = cJSON_CreateArray(), *attempt = cJSON_CreateObject();
        cJSON_AddStringToObject(attempt, "attempt_id", "a");
        cJSON_AddStringToObject(attempt, "transaction_id", "t");
        cJSON_AddNumberToObject(attempt, "session_generation", 1);
        em_backend backend;
        void *ctx = NULL;
        em_telemetry_scope telemetry = {0};
        em_watch_scope watch = {0};
        em_steering_scope steering = {0};
        if (!strcmp(scope, "telemetry")) {
            em_reason why;
            em_telemetry_intent_from(str(intent, "pod_id"), serial, intent, &telemetry.intent, &why);
            telemetry.ovs = ovs;
            telemetry.serial = serial;
            telemetry.transact = record;
            telemetry.transact_ctx = sent;
            backend = em_telemetry_backend();
            ctx = &telemetry;
        } else if (!strcmp(scope, "probe-watch")) {
            watch = (em_watch_scope){.ovs = ovs, .serial = serial, .pod_id = str(doc, "pod_id"),
                                     .transact = record, .transact_ctx = sent};
            backend = em_watch_backend();
            ctx = &watch;
        } else {
            steering = (em_steering_scope){.ovs = ovs, .serial = serial, .pod_id = str(doc, "pod_id"),
                                           .transact = record, .transact_ctx = sent};
            backend = em_steering_backend();
            ctx = &steering;
        }
        const cJSON *expected = cJSON_GetObjectItemCaseSensitive(c, "expected");
        em_reason why = EM_OK;
        cJSON *plan = backend.plan(ctx, intent, &why);
        checks++;
        if (!plan) {
            const char *want = str(expected, "refusal");
            if (!want || strcmp(want, em_reason_name(why)))
                fail("scope-writes", name, em_reason_name(why));
        } else {
            em_submit_out out = {0};
            backend.submit(ctx, intent, attempt, &out);
            const char *want = str(expected, "status");
            if (!want || strcmp(want, out.status))
                fail("scope-writes", name, out.status);
            cJSON_Delete(out.evidence);
        }
        checks++;
        if (!cJSON_Compare(sent, cJSON_GetObjectItemCaseSensitive(expected, "transactions"), 1)) {
            fail("scope-writes", name, "transactions differ");
            if (getenv("EMOSA_VECTORS_DEBUG")) {
                char *text = cJSON_PrintUnformatted(sent);
                fprintf(stderr, "%s got %s\n", name, text);
                free(text);
            }
        }
        cJSON_Delete(plan);
        cJSON_Delete(sent);
        cJSON_Delete(attempt);
        em_snapshot_clear(&telemetry.last);
        em_snapshot_clear(&watch.last);
        em_snapshot_clear(&steering.last);
        em_ovsdb_close(ovs);
    }
    cJSON_Delete(doc);
}

/* -- engine.json: the operation lifecycle over a scripted pod -------------------------------- */

typedef struct {
    char config[256], observed[256];
    bool fresh, ready;
    char instance[17]; /* the start of the pod's OpenSync, "" for none (spec §5) */
    char plan_error[32], result[16];
    jmp_buf crash;
} scripted_pod;

static double engine_clock_value;
static double engine_clock(void) { return engine_clock_value; }

static bool scripted_snapshot(void *ctx, em_snapshot *out)
{
    scripted_pod *p = ctx;
    memset(out, 0, sizeof(*out));
    out->config = cJSON_CreateObject();
    cJSON_AddStringToObject(out->config, "watched", p->config);
    cJSON *values = cJSON_CreateObject();
    cJSON_AddStringToObject(values, "watched", p->observed);
    out->observed = em_observation("pod-1", "probe-watch", values, "scripted", 1, p->fresh, "scripted", 1);
    out->ready = p->ready;
    out->generation = 1;
    strcpy(out->schema_fingerprint, "conformance");
    if (p->instance[0]) {
        snprintf(out->instance, sizeof(out->instance), "%s", p->instance);
        out->has_instance = true;
    }
    return true;
}

static cJSON *scripted_plan(void *ctx, const cJSON *intent, em_reason *why)
{
    scripted_pod *p = ctx;
    (void)intent;
    if (p->plan_error[0]) {
        *why = em_reason_parse(p->plan_error);
        return NULL;
    }
    cJSON *plan = cJSON_CreateObject();
    cJSON_AddStringToObject(plan, "action", "scripted");
    return plan;
}

static void scripted_submit(void *ctx, const cJSON *intent, const cJSON *attempt, em_submit_out *out)
{
    scripted_pod *p = ctx;
    (void)intent;
    (void)attempt;
    memset(out, 0, sizeof(*out));
    if (!strcmp(p->result, "crash"))
        longjmp(p->crash, 1); /* the process stops after the journal's SUBMITTED record */
    if (!strcmp(p->result, "committed")) {
        strcpy(out->status, "committed");
        out->evidence = cJSON_CreateObject();
        cJSON_AddStringToObject(out->evidence, "attribution", "reply");
        cJSON_AddTrueToObject(out->evidence, "transaction_validated");
        if (p->instance[0])
            cJSON_AddStringToObject(out->evidence, "instance", p->instance);
    } else if (!strcmp(p->result, "conflict")) {
        strcpy(out->status, "conflict");
        out->reason = EM_PRECONDITION_FAILED;
    } else if (!strcmp(p->result, "rejected")) {
        strcpy(out->status, "rejected");
        out->reason = EM_NOT_READY;
    } else { /* unknown, or lost: no reply */
        strcpy(out->status, "unknown");
        out->reason = EM_OUTCOME_UNKNOWN;
    }
}

static cJSON *watch_intent_of(const cJSON *stations)
{
    cJSON *i = cJSON_CreateObject();
    cJSON_AddStringToObject(i, "pod_id", "pod-1");
    cJSON_AddStringToObject(i, "if_name", "home-ap-24");
    cJSON_AddStringToObject(i, "band", "2.4G");
    cJSON_AddItemToObject(i, "stations", cJSON_Duplicate(stations, 1));
    return i;
}

/* Execute, stopping where the scripted pod crashes the process (its own frame, so the
 * caller's locals stay clear of the longjmp). */
static void execute_or_crash(em_engine *engine, scripted_pod *pod, const char *id)
{
    if (!setjmp(pod->crash))
        cJSON_Delete(em_engine_execute(engine, id));
}

static cJSON *null_or(const cJSON *v) { return v ? cJSON_Duplicate(v, 1) : cJSON_CreateNull(); }

static void engine_vectors(const char *dir)
{
    cJSON *doc = load(dir, "engine.json");
    char schemas_dir[600];
    snprintf(schemas_dir, sizeof(schemas_dir), "%s/../../schemas", dir);
    em_journal_schemas schemas = {em_schema_load(schemas_dir, "operation"), em_schema_load(schemas_dir, "event"),
                                  em_schema_load(schemas_dir, "wsc-receipt")};
    const cJSON *c;
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "cases"))
    {
        const char *name = str(c, "name");
        char path[] = "/tmp/emosa-engine-XXXXXX", sub[64];
        if (!mkdtemp(path)) {
            fail("engine", name, "temporary directory");
            continue;
        }
        snprintf(sub, sizeof(sub), "%s/secrets", path);
        em_vault vault;
        em_vault_open(&vault, sub);
        snprintf(sub, sizeof(sub), "%s/journal", path);
        em_reason why;
        em_journal *journal = em_journal_open(sub, &schemas, &why);
        static scripted_pod pod;
        memset(&pod, 0, sizeof(pod));
        pod.fresh = pod.ready = true;
        strcpy(pod.result, "committed");
        em_backend backend = {"scripted", em_watch_backend().target, scripted_snapshot, scripted_plan, scripted_submit};
        static em_engine engine;
        engine_clock_value = 0;
        em_engine_init(&engine, journal, &vault, "pod-1", backend, &pod, engine_clock);
        cJSON *ids = cJSON_CreateArray();
        long seen = 0;
        const cJSON *step;
        int index = 0;
        cJSON_ArrayForEach(step, cJSON_GetObjectItemCaseSensitive(c, "steps"))
        {
            char label[96];
            snprintf(label, sizeof(label), "%s step %d", name, index++);
            const cJSON *s = cJSON_GetObjectItemCaseSensitive(step, "step");
            const char *kind = cJSON_GetArrayItem(s, 0)->valuestring;
            const char *error = NULL;
            if (!strcmp(kind, "request")) {
                cJSON *intent = watch_intent_of(cJSON_GetArrayItem(s, 1));
                cJSON *op = em_engine_request(&engine, intent, "conformance", cJSON_GetArrayItem(s, 2)->valuestring,
                                              "run-1", cJSON_GetArrayItem(s, 3)->valuedouble, "semantic", NULL, &why);
                if (!op) {
                    error = em_reason_name(why);
                } else {
                    const char *id = str(op, "operation_id");
                    bool known = false;
                    const cJSON *x;
                    cJSON_ArrayForEach(x, ids) known = known || !strcmp(x->valuestring, id);
                    if (!known)
                        cJSON_AddItemToArray(ids, cJSON_CreateString(id));
                }
                cJSON_Delete(op);
                cJSON_Delete(intent);
            } else if (!strcmp(kind, "execute")) {
                const char *id = cJSON_GetArrayItem(ids, cJSON_GetArrayItem(s, 1)->valueint)->valuestring;
                execute_or_crash(&engine, &pod, id);
            } else if (!strcmp(kind, "pod")) {
                snprintf(pod.config, sizeof(pod.config), "%s", cJSON_GetArrayItem(s, 1)->valuestring);
                snprintf(pod.observed, sizeof(pod.observed), "%s", cJSON_GetArrayItem(s, 2)->valuestring);
                pod.fresh = cJSON_IsTrue(cJSON_GetArrayItem(s, 3));
                pod.ready = cJSON_IsTrue(cJSON_GetArrayItem(s, 4));
                const cJSON *instance = cJSON_GetArrayItem(s, 5);
                snprintf(pod.instance, sizeof(pod.instance), "%s",
                         cJSON_IsString(instance) ? instance->valuestring : "");
            } else if (!strcmp(kind, "script")) {
                const cJSON *e = cJSON_GetArrayItem(s, 1);
                snprintf(pod.plan_error, sizeof(pod.plan_error), "%s", cJSON_IsString(e) ? e->valuestring : "");
                snprintf(pod.result, sizeof(pod.result), "%s", cJSON_GetArrayItem(s, 2)->valuestring);
            } else if (!strcmp(kind, "advance")) {
                engine_clock_value += cJSON_GetArrayItem(s, 1)->valuedouble;
            } else if (!strcmp(kind, "reconcile")) {
                em_engine_reconcile(&engine);
            } else if (!strcmp(kind, "recover")) { /* a new process on the same journal */
                em_engine_free(&engine);
                em_engine_init(&engine, journal, &vault, "pod-1", backend, &pod, engine_clock);
                em_engine_recover(&engine);
            } else if (!strcmp(kind, "cancel")) {
                cJSON_Delete(em_engine_cancel(&engine, cJSON_GetArrayItem(ids, cJSON_GetArrayItem(s, 1)->valueint)->valuestring));
            }
            cJSON *got = cJSON_CreateObject(), *ops = cJSON_CreateArray();
            cJSON_AddItemToObject(got, "error", error ? cJSON_CreateString(error) : cJSON_CreateNull());
            const cJSON *x;
            cJSON_ArrayForEach(x, ids)
            {
                cJSON *op = em_journal_get(journal, x->valuestring), *o = cJSON_CreateObject();
                const cJSON *evidence = cJSON_GetObjectItemCaseSensitive(op, "application_evidence");
                cJSON_AddItemToObject(o, "state", null_or(cJSON_GetObjectItemCaseSensitive(op, "state")));
                cJSON_AddItemToObject(o, "reason", null_or(cJSON_GetObjectItemCaseSensitive(op, "reason")));
                cJSON_AddItemToObject(o, "deadline_elapsed", null_or(cJSON_GetObjectItemCaseSensitive(op, "deadline_elapsed")));
                cJSON_AddItemToObject(o, "original_outcome", null_or(cJSON_GetObjectItemCaseSensitive(op, "original_outcome")));
                cJSON_AddItemToObject(o, "late_resolution", null_or(cJSON_GetObjectItemCaseSensitive(op, "late_resolution")));
                cJSON_AddItemToObject(o, "changed", null_or(cJSON_GetObjectItemCaseSensitive(op, "changed")));
                cJSON_AddItemToObject(o, "blocked_for_resubmission",
                                      null_or(cJSON_GetObjectItemCaseSensitive(op, "blocked_for_resubmission")));
                cJSON_AddBoolToObject(o, "applied_evidence", evidence && !cJSON_IsNull(evidence));
                cJSON_AddItemToObject(o, "attribution", null_or(cJSON_GetObjectItemCaseSensitive(evidence, "attribution")));
                cJSON_AddItemToObject(o, "commit_attribution",
                                      null_or(cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(op, "commit_evidence"), "attribution")));
                cJSON_AddItemToArray(ops, o);
                cJSON_Delete(op);
            }
            cJSON_AddItemToObject(got, "operations", ops);
            cJSON *owned = em_journal_ownership(journal, "pod-1");
            cJSON_AddBoolToObject(got, "ownership", owned != NULL);
            cJSON_Delete(owned);
            cJSON *events = em_journal_events(journal, "run-1", seen, 500), *phases = cJSON_CreateArray(), *e;
            cJSON_ArrayForEach(e, events)
            {
                cJSON_AddItemToArray(phases, cJSON_CreateString(str(e, "phase")));
                seen = (long)num(e, "sequence");
            }
            cJSON_Delete(events);
            cJSON_AddItemToObject(got, "events", phases);
            checks++;
            if (!cJSON_Compare(got, cJSON_GetObjectItemCaseSensitive(step, "expected"), 1)) {
                fail("engine", label, "lifecycle differs");
                if (getenv("EMOSA_VECTORS_DEBUG")) {
                    char *text = cJSON_PrintUnformatted(got);
                    fprintf(stderr, "%s got %s\n", label, text);
                    free(text);
                }
            }
            cJSON_Delete(got);
        }
        em_engine_free(&engine);
        em_journal_close(journal);
        cJSON_Delete(ids);
        char cmd[128];
        snprintf(cmd, sizeof(cmd), "rm -rf %s", path);
        if (system(cmd)) {
        }
    }
    em_schema_free(schemas.operation);
    em_schema_free(schemas.event);
    em_schema_free(schemas.receipt);
    cJSON_Delete(doc);
}

/* -- early-report.json: the Early AP Capability Report's delivery ---------------------------- */

static void early_report_vectors(const char *dir)
{
    cJSON *doc = load(dir, "early-report.json");
    const cJSON *agent = cJSON_GetObjectItemCaseSensitive(doc, "agent");
    em_device_view *view = malloc(sizeof(*view));
    uint8_t ruid[6], controller[6], al[6];
    em_parse_mac(str(agent, "radio"), ruid);
    em_parse_mac(str(agent, "controller_al"), controller);
    em_parse_mac(str(agent, "al_mac"), al);
    em_tlv_list caps = {0};
    if (em_device_view_from_rows(cJSON_GetObjectItemCaseSensitive(doc, "ovsdb_tables"), view) != EM_OK ||
        em_capability_tlvs(em_view_radio(view, ruid), 6, 5, 30, &caps) != EM_OK) {
        fail("early-report", "rows", "capabilities");
        free(view);
        cJSON_Delete(doc);
        return;
    }
    const cJSON *c;
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "cases"))
    {
        const char *name = str(c, "name");
        em_early early = {0};
        uint16_t mid = (uint16_t)(num(agent, "first_mid") - 1), sent_mids[8];
        size_t nsent = 0;
        bool changed = false; /* the pod's State since the report was built */
        const cJSON *step;
        int index = 0;
        cJSON_ArrayForEach(step, cJSON_GetObjectItemCaseSensitive(c, "steps"))
        {
            char label[96];
            snprintf(label, sizeof(label), "%s step %d", name, index++);
            const cJSON *s = cJSON_GetObjectItemCaseSensitive(step, "step");
            const char *kind = cJSON_GetArrayItem(s, 0)->valuestring;
            double at = cJSON_GetArrayItem(s, 1)->valuedouble;
            em_frames out = {0};
            const char *result = NULL;
            bool transmit = false;
            if (!strcmp(kind, "notify")) {
                em_early_start(&early, at);
                changed = false;
                transmit = true;
            } else if (!strcmp(kind, "tick")) {
                transmit = em_early_tick(&early, at, !changed);
            } else if (!strcmp(kind, "revision")) {
                changed = true;
            } else {
                const cJSON *which = cJSON_GetArrayItem(s, 2);
                uint16_t acked = cJSON_IsNumber(which) ? sent_mids[which->valueint] : 999;
                result = em_early_ack(&early, acked, cJSON_IsTrue(cJSON_GetArrayItem(s, 3)), at);
            }
            if (transmit) {
                ++mid;
                em_fragment(controller, al, 0x8043, mid, caps.tlvs, caps.count, false, EM_MAX_CMDU, &out);
                em_early_sent(&early, mid, at);
                sent_mids[nsent++ & 7] = mid;
            }
            checks++;
            const cJSON *expected = cJSON_GetObjectItemCaseSensitive(step, "expected");
            const char *want = str(expected, "result");
            if ((want == NULL) != (result == NULL) || (want && strcmp(want, result)))
                fail("early-report", label, result ? result : "none");
            else if (!frames_equal(&out, cJSON_GetObjectItemCaseSensitive(expected, "frames")))
                fail("early-report", label, "frames differ");
            em_frames_free(&out);
        }
        checks++;
        cJSON *counts = em_early_counts(&early);
        if (!cJSON_Compare(counts, cJSON_GetObjectItemCaseSensitive(c, "expected_counts"), 1))
            fail("early-report", name, "counts differ");
        cJSON_Delete(counts);
    }
    em_tlv_list_free(&caps);
    free(view);
    cJSON_Delete(doc);
}

/* -- steering-queue.json: client steering, one window at a time with a short queue ------------ */

static double steering_clock_value;
static double steering_clock(void) { return steering_clock_value; }

/* the case's pod: with owm, a client row a transaction inserts appears in the rows as owm
 * steering it (the fixed view's own copy, changed here as the Recorder changes its rows);
 * the lost transactions are carried out without a reply */
static struct {
    cJSON *sent;
    em_ovsdb *ovs;
    bool owm;
    const cJSON *lost;
} steering_pod;

static cJSON *steering_transact(void *ctx, const cJSON *operations)
{
    (void)ctx;
    int index = cJSON_GetArraySize(steering_pod.sent);
    cJSON *results = record(steering_pod.sent, operations);
    const cJSON *o;
    cJSON_ArrayForEach(o, operations)
    {
        if (!steering_pod.owm || strcmp(str(o, "op"), "insert") || strcmp(str(o, "table"), "Band_Steering_Clients"))
            continue;
        cJSON *tables = (cJSON *)em_ovsdb_tables(steering_pod.ovs);
        cJSON *table = cJSON_GetObjectItemCaseSensitive(tables, "Band_Steering_Clients");
        if (!table)
            table = cJSON_AddObjectToObject(tables, "Band_Steering_Clients");
        cJSON *row = cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(o, "row"), 1);
        cJSON_DeleteItemFromObjectCaseSensitive(row, "cs_state");
        cJSON_AddStringToObject(row, "cs_state", "steering");
        cJSON_DeleteItemFromObjectCaseSensitive(table, "00000000-0000-4000-8000-0000000000ff");
        cJSON_AddItemToObject(table, "00000000-0000-4000-8000-0000000000ff", row);
    }
    cJSON_ArrayForEach(o, steering_pod.lost)
    {
        if (o->valueint == index) {
            cJSON_Delete(results);
            return NULL;
        }
    }
    return results;
}

static void strip_operation_ids(cJSON *status)
{
    cJSON_DeleteItemFromObjectCaseSensitive(cJSON_GetObjectItemCaseSensitive(status, "active"), "operation_id");
    cJSON *e;
    cJSON_ArrayForEach(e, cJSON_GetObjectItemCaseSensitive(status, "history"))
    cJSON_DeleteItemFromObjectCaseSensitive(e, "operation_id");
}

static void steering_queue_vectors(const char *dir)
{
    cJSON *doc = load(dir, "steering-queue.json");
    char schemas_dir[600];
    snprintf(schemas_dir, sizeof(schemas_dir), "%s/../../schemas", dir);
    em_journal_schemas schemas = {em_schema_load(schemas_dir, "operation"), em_schema_load(schemas_dir, "event"),
                                  em_schema_load(schemas_dir, "wsc-receipt")};
    uint8_t source[6], target[6];
    em_parse_mac(str(doc, "source_bssid"), source);
    em_parse_mac(str(doc, "target"), target);
    const cJSON *c;
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "cases"))
    {
        const char *name = str(c, "name");
        char path[] = "/tmp/emosa-steering-XXXXXX", sub[64];
        if (!mkdtemp(path)) {
            fail("steering-queue", name, "temporary directory");
            continue;
        }
        snprintf(sub, sizeof(sub), "%s/secrets", path);
        em_vault vault;
        em_vault_open(&vault, sub);
        em_ovsdb *ovs = em_ovsdb_fixed(cJSON_GetObjectItemCaseSensitive(doc, "ovsdb_tables"), 1, "conformance");
        cJSON *sent = cJSON_CreateArray();
        steering_pod.sent = sent;
        steering_pod.ovs = ovs;
        steering_pod.owm = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(c, "owm"));
        steering_pod.lost = cJSON_GetObjectItemCaseSensitive(c, "lost");
        steering_clock_value = 0;
        static em_steering_scope steering;
        memset(&steering, 0, sizeof(steering));
        steering.ovs = ovs;
        steering.serial = str(doc, "serial");
        steering.pod_id = "pod-1";
        steering.run_id = "run-1";
        steering.transact = steering_transact;
        steering.monotonic = steering_clock;
        if (em_steering_scope_open(&steering, path, &schemas, &vault) != EM_OK) {
            fail("steering-queue", name, "journal");
            continue;
        }
        int already = 0, index = 0;
        const cJSON *step;
        cJSON_ArrayForEach(step, cJSON_GetObjectItemCaseSensitive(c, "steps"))
        {
            char label[96];
            snprintf(label, sizeof(label), "%s step %d", name, index++);
            const cJSON *s = cJSON_GetObjectItemCaseSensitive(step, "step");
            steering_clock_value = cJSON_GetArrayItem(s, 1)->valuedouble;
            const char *result = NULL;
            if (!strcmp(cJSON_GetArrayItem(s, 0)->valuestring, "start")) {
                em_steering_request r = {0};
                memcpy(r.source_bssid, source, 6);
                r.mandate = true;
                r.disassoc_imminent = cJSON_IsTrue(cJSON_GetArrayItem(s, 5));
                r.window = (uint16_t)cJSON_GetArrayItem(s, 4)->valueint;
                r.nstations = r.ntargets = 1;
                em_parse_mac(cJSON_GetArrayItem(s, 2)->valuestring, r.stations[0]);
                memcpy(r.targets[0].bssid, target, 6);
                r.targets[0].op_class = 81;
                r.targets[0].channel = cJSON_GetArraySize(s) > 6 ? (uint8_t)cJSON_GetArrayItem(s, 6)->valueint : 6;
                result = em_steering_start(&steering, &r, (uint16_t)cJSON_GetArrayItem(s, 3)->valueint);
            } else if (!strcmp(cJSON_GetArrayItem(s, 0)->valuestring, "restart")) {
                em_steering_scope_close(&steering);
                em_steering_scope fresh = {.ovs = ovs, .serial = steering.serial, .pod_id = "pod-1",
                                           .run_id = "run-1", .transact = steering_transact,
                                           .monotonic = steering_clock};
                steering = fresh;
                if (em_steering_scope_open(&steering, path, &schemas, &vault) != EM_OK) {
                    fail("steering-queue", label, "journal");
                    break;
                }
            } else {
                em_steering_tick(&steering);
            }
            const cJSON *expected = cJSON_GetObjectItemCaseSensitive(step, "expected");
            const char *want = str(expected, "result");
            checks++;
            if ((want == NULL) != (result == NULL) || (want && strcmp(want, result)))
                fail("steering-queue", label, result ? result : "none");
            cJSON *these = cJSON_CreateArray();
            for (int i = already; i < cJSON_GetArraySize(sent); i++)
                cJSON_AddItemToArray(these, cJSON_Duplicate(cJSON_GetArrayItem(sent, i), 1));
            already = cJSON_GetArraySize(sent);
            checks++;
            if (!cJSON_Compare(these, cJSON_GetObjectItemCaseSensitive(expected, "transactions"), 1))
                fail("steering-queue", label, "transactions differ");
            cJSON_Delete(these);
            checks++;
            cJSON *status = em_steering_status(&steering);
            strip_operation_ids(status);
            if (!cJSON_Compare(status, cJSON_GetObjectItemCaseSensitive(expected, "status"), 1)) {
                fail("steering-queue", label, "status differs");
                if (getenv("EMOSA_VECTORS_DEBUG")) {
                    char *text = cJSON_PrintUnformatted(status);
                    fprintf(stderr, "%s got %s\n", label, text);
                    free(text);
                }
            }
            cJSON_Delete(status);
        }
        em_steering_scope_close(&steering);
        em_ovsdb_close(ovs);
        cJSON_Delete(sent);
        char cmd[128];
        snprintf(cmd, sizeof(cmd), "rm -rf %s", path);
        if (system(cmd)) {
        }
    }
    em_schema_free(schemas.operation);
    em_schema_free(schemas.event);
    em_schema_free(schemas.receipt);
    cJSON_Delete(doc);
}

/* -- probe-watch.json: which stations the pod watches ----------------------------------------- */

static double watch_clock_value;
static double watch_clock(void) { return watch_clock_value; }

static void probe_watch_vectors(const char *dir)
{
    cJSON *doc = load(dir, "probe-watch.json");
    char schemas_dir[600];
    snprintf(schemas_dir, sizeof(schemas_dir), "%s/../../schemas", dir);
    em_journal_schemas schemas = {em_schema_load(schemas_dir, "operation"), em_schema_load(schemas_dir, "event"),
                                  em_schema_load(schemas_dir, "wsc-receipt")};
    const cJSON *c;
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "cases"))
    {
        const char *name = str(c, "name");
        char path[] = "/tmp/emosa-watch-XXXXXX", sub[64];
        if (!mkdtemp(path)) {
            fail("probe-watch", name, "temporary directory");
            continue;
        }
        snprintf(sub, sizeof(sub), "%s/secrets", path);
        em_vault vault;
        em_vault_open(&vault, sub);
        em_ovsdb *ovs = em_ovsdb_fixed(cJSON_GetObjectItemCaseSensitive(c, "ovsdb_tables"), 1, "conformance");
        cJSON *sent = cJSON_CreateArray();
        watch_clock_value = 0;
        static em_watch_scope watch;
        memset(&watch, 0, sizeof(watch));
        watch.ovs = ovs;
        watch.serial = str(doc, "serial");
        watch.pod_id = "pod-1";
        watch.run_id = "run-1";
        watch.if_name = "home-ap-24";
        watch.band = "2.4G";
        watch.transact = record;
        watch.transact_ctx = sent;
        watch.monotonic = watch_clock;
        if (em_watch_open(&watch, path, &schemas, &vault) != EM_OK) {
            fail("probe-watch", name, "journal");
            continue;
        }
        int already = 0, index = 0;
        const cJSON *step;
        cJSON_ArrayForEach(step, cJSON_GetObjectItemCaseSensitive(c, "steps"))
        {
            char label[96];
            snprintf(label, sizeof(label), "%s step %d", name, index++);
            const cJSON *s = cJSON_GetObjectItemCaseSensitive(step, "step");
            watch_clock_value = cJSON_GetArrayItem(s, 1)->valuedouble;
            if (!strcmp(cJSON_GetArrayItem(s, 0)->valuestring, "ask")) {
                const cJSON *m;
                uint8_t stations[64][6];
                size_t n = 0;
                cJSON_ArrayForEach(m, cJSON_GetArrayItem(s, 2)) em_parse_mac(m->valuestring, stations[n++]);
                em_watch_ask(&watch, (const uint8_t(*)[6])stations, n);
            } else {
                em_watch_tick(&watch);
            }
            const cJSON *expected = cJSON_GetObjectItemCaseSensitive(step, "expected");
            cJSON *these = cJSON_CreateArray();
            for (int i = already; i < cJSON_GetArraySize(sent); i++)
                cJSON_AddItemToArray(these, cJSON_Duplicate(cJSON_GetArrayItem(sent, i), 1));
            already = cJSON_GetArraySize(sent);
            checks++;
            if (!cJSON_Compare(these, cJSON_GetObjectItemCaseSensitive(expected, "transactions"), 1)) {
                fail("probe-watch", label, "transactions differ");
                if (getenv("EMOSA_VECTORS_DEBUG")) {
                    char *text = cJSON_PrintUnformatted(these);
                    fprintf(stderr, "%s got %s\n", label, text);
                    free(text);
                }
            }
            cJSON_Delete(these);
            checks++;
            cJSON *status = em_watch_status(&watch);
            cJSON_DeleteItemFromObjectCaseSensitive(cJSON_GetObjectItemCaseSensitive(status, "operation"), "operation_id");
            if (!cJSON_Compare(status, cJSON_GetObjectItemCaseSensitive(expected, "status"), 1)) {
                fail("probe-watch", label, "status differs");
                if (getenv("EMOSA_VECTORS_DEBUG")) {
                    char *text = cJSON_PrintUnformatted(status);
                    fprintf(stderr, "%s got %s\n", label, text);
                    free(text);
                }
            }
            cJSON_Delete(status);
        }
        em_watch_close(&watch);
        em_ovsdb_close(ovs);
        cJSON_Delete(sent);
        char cmd[128];
        snprintf(cmd, sizeof(cmd), "rm -rf %s", path);
        if (system(cmd)) {
        }
    }
    em_schema_free(schemas.operation);
    em_schema_free(schemas.event);
    em_schema_free(schemas.receipt);
    cJSON_Delete(doc);
}

static void session_timing_vectors(const char *dir)
{
    cJSON *doc = load(dir, "session-timing.json");
    const cJSON *c;
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "attempts"))
    {
        const char *name = str(c, "name");
        em_attempts at = {0};
        const cJSON *step;
        int index = 0;
        cJSON_ArrayForEach(step, cJSON_GetObjectItemCaseSensitive(c, "steps"))
        {
            char label[96];
            snprintf(label, sizeof(label), "%s step %d", name, index++);
            const cJSON *s = cJSON_GetObjectItemCaseSensitive(step, "step");
            double t = cJSON_GetArrayItem(s, 1)->valuedouble;
            em_attempt_step got = {0};
            if (!strcmp(cJSON_GetArrayItem(s, 0)->valuestring, "renew")) {
                em_attempts_renew(&at);
            } else {
                const cJSON *generation = cJSON_GetArrayItem(s, 2);
                got = em_attempts_tick(&at, t, cJSON_IsNumber(generation),
                                       cJSON_IsNumber(generation) ? generation->valueint : 0);
            }
            const cJSON *e = cJSON_GetObjectItemCaseSensitive(step, "expected");
            const char *ended = str(e, "ended");
            char seen[160];
            snprintf(seen, sizeof(seen), "started %d searches %d ended %s discovering %d starts %u failures %u next %.3f",
                     got.start, got.search, got.ended ? got.ended : "null", at.state == EM_SESSION_DISCOVERING,
                     at.starts, at.failures, at.next_start);
            checks++;
            if (got.start != cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(e, "started")) ||
                (int)got.search != (int)num(e, "searches") || (ended == NULL) != (got.ended == NULL) ||
                (ended && strcmp(ended, got.ended)) ||
                (at.state == EM_SESSION_DISCOVERING) != cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(e, "discovering")) ||
                at.starts != (unsigned)num(e, "attempts_started") || at.failures != (unsigned)num(e, "failures") ||
                fabs(at.next_start - num(e, "next_start")) > 1e-9)
                fail("session-timing", label, seen);
        }
    }
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "renewals"))
    {
        const char *name = str(c, "name");
        em_renew rules;
        em_renew_init(&rules, 0);
        const cJSON *step;
        int index = 0;
        cJSON_ArrayForEach(step, cJSON_GetObjectItemCaseSensitive(c, "steps"))
        {
            char label[96];
            snprintf(label, sizeof(label), "%s step %d", name, index++);
            const cJSON *s = cJSON_GetObjectItemCaseSensitive(step, "step");
            double t = cJSON_GetArrayItem(s, 1)->valuedouble;
            const char *reasons[3];
            size_t n = 0;
            if (!strcmp(cJSON_GetArrayItem(s, 0)->valuestring, "contact"))
                em_renew_contact(&rules, t);
            else
                n = em_renew_check(&rules, t, cJSON_IsTrue(cJSON_GetArrayItem(s, 2)),
                                   cJSON_IsTrue(cJSON_GetArrayItem(s, 3)), reasons);
            const cJSON *want = cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(step, "expected"),
                                                                 "reasons");
            bool same = (size_t)cJSON_GetArraySize(want) == n;
            for (size_t i = 0; same && i < n; i++)
                same = !strcmp(cJSON_GetArrayItem(want, (int)i)->valuestring, reasons[i]);
            checks++;
            if (!same)
                fail("session-timing", label, n ? reasons[0] : "no reason");
        }
    }
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "reannouncements"))
    {
        const char *name = str(c, "name");
        em_reannounce rule = {0};
        const cJSON *step;
        int index = 0;
        cJSON_ArrayForEach(step, cJSON_GetObjectItemCaseSensitive(c, "steps"))
        {
            char label[96];
            snprintf(label, sizeof(label), "%s step %d", name, index++);
            const cJSON *s = cJSON_GetObjectItemCaseSensitive(step, "step");
            unsigned responses = (unsigned)cJSON_GetArrayItem(s, 1)->valueint;
            const cJSON *want = cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(step, "expected"), "due");
            checks++;
            if (!strcmp(cJSON_GetArrayItem(s, 0)->valuestring, "provisioned")) {
                em_reannounce_provisioned(&rule, responses);
                if (!cJSON_IsNull(want))
                    fail("session-timing", label, "provisioned has no answer");
            } else if (em_reannounce_due(&rule, responses) != cJSON_IsTrue(want)) {
                fail("session-timing", label, "due differs");
            }
        }
    }
    cJSON_Delete(doc);
}

/* -- journal-retention.json: what the journal keeps as operations are added ---------------- */

/* the numbers (the last 12 digits of operation_id) of the operations the journal holds */
static cJSON *kept_numbers(em_journal *j)
{
    cJSON *out = cJSON_CreateArray();
    const cJSON *op;
    cJSON_ArrayForEach(op, em_journal_operations_view(j))
    {
        const char *id = str(op, "operation_id");
        size_t n = id ? strlen(id) : 0;
        cJSON_AddItemToArray(out, cJSON_CreateNumber(n >= 12 ? strtod(id + n - 12, NULL) : -1));
    }
    return out;
}

static void journal_retention_vectors(const char *dir)
{
    cJSON *doc = load(dir, "journal-retention.json");
    char schemas_dir[600];
    snprintf(schemas_dir, sizeof(schemas_dir), "%s/../../schemas", dir);
    em_journal_schemas schemas = {em_schema_load(schemas_dir, "operation"), em_schema_load(schemas_dir, "event"),
                                  em_schema_load(schemas_dir, "wsc-receipt")};
    const cJSON *template = cJSON_GetObjectItemCaseSensitive(doc, "template"), *c;
    cJSON_ArrayForEach(c, cJSON_GetObjectItemCaseSensitive(doc, "cases"))
    {
        const char *name = str(c, "name");
        char path[] = "/tmp/emosa-retention-XXXXXX", journal_dir[64];
        if (!mkdtemp(path)) {
            fail("journal-retention", name, "temporary directory");
            continue;
        }
        snprintf(journal_dir, sizeof(journal_dir), "%s/journal", path);
        em_reason why;
        em_journal *j = em_journal_open(journal_dir, &schemas, &why);
        if (!j) {
            fail("journal-retention", name, "journal");
            continue;
        }
        const cJSON *step, *last = NULL;
        int index = 0;
        cJSON_ArrayForEach(step, cJSON_GetObjectItemCaseSensitive(c, "steps"))
        {
            char label[96], id[40], key[24];
            snprintf(label, sizeof(label), "%s step %d", name, index++);
            const cJSON *s = cJSON_GetObjectItemCaseSensitive(step, "step");
            int n = cJSON_GetArrayItem(s, 1)->valueint;
            /* the template, numbered n, for the step's pod, in its state */
            cJSON *op = cJSON_Duplicate(template, true);
            snprintf(id, sizeof(id), "00000000-0000-4000-8000-%012d", n);
            snprintf(key, sizeof(key), "key-%d", n);
            cJSON_ReplaceItemInObjectCaseSensitive(op, "operation_id", cJSON_CreateString(id));
            cJSON_ReplaceItemInObjectCaseSensitive(op, "idempotency_key", cJSON_CreateString(key));
            cJSON_ReplaceItemInObjectCaseSensitive(cJSON_GetObjectItemCaseSensitive(op, "intent"), "pod_id",
                                                   cJSON_CreateString(cJSON_GetArrayItem(s, 2)->valuestring));
            cJSON_ReplaceItemInObjectCaseSensitive(op, "state", cJSON_CreateString(cJSON_GetArrayItem(s, 3)->valuestring));
            em_reason r = strcmp(cJSON_GetArrayItem(s, 0)->valuestring, "add") ? em_journal_save(j, op, NULL)
                                                                                : em_journal_add(j, op, NULL);
            cJSON_Delete(op);
            checks++;
            if (r != EM_OK)
                fail("journal-retention", label, em_reason_name(r));
            const cJSON *expected = cJSON_GetObjectItemCaseSensitive(step, "kept");
            if (!expected)
                continue;
            last = expected;
            cJSON *kept = kept_numbers(j);
            checks++;
            if (!cJSON_Compare(kept, expected, true))
                fail("journal-retention", label, "kept operations differ");
            cJSON_Delete(kept);
        }
        /* what was removed is gone from the file too: the journal read again */
        em_journal_close(j);
        j = em_journal_open(journal_dir, &schemas, &why);
        checks++;
        if (!j) {
            fail("journal-retention", name, "journal again");
        } else {
            cJSON *kept = kept_numbers(j);
            if (last && !cJSON_Compare(kept, last, true))
                fail("journal-retention", name, "kept operations differ after reopening");
            cJSON_Delete(kept);
            em_journal_close(j);
        }
        char cmd[128];
        snprintf(cmd, sizeof(cmd), "rm -rf %s", path);
        if (system(cmd)) {
        }
    }
    em_schema_free(schemas.operation);
    em_schema_free(schemas.event);
    em_schema_free(schemas.receipt);
    cJSON_Delete(doc);
}

int main(int argc, char **argv)
{
    em_init();
    const char *dir = argc > 1 ? argv[1] : "spec/conformance";
    cmdu_vectors(dir);
    onboarding_vectors(dir);
    northbound_vectors(dir);
    control_vectors(dir);
    southbound_vectors(dir);
    uplink_vectors(dir);
    fleet_vectors(dir);
    fleet_session_vectors(dir);
    gtp_vectors(dir);
    telemetry_vectors(dir);
    metrics_vectors(dir);
    backhaul_steering_vectors(dir);
    scope_writes_vectors(dir);
    engine_vectors(dir);
    early_report_vectors(dir);
    steering_queue_vectors(dir);
    journal_retention_vectors(dir);
    probe_watch_vectors(dir);
    session_timing_vectors(dir);
    printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
