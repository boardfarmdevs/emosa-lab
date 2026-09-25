/* Replays the conformance vectors (spec/conformance) against the C lab prototype.
 * Usage: emosa-vectors <spec/conformance directory> */
#include <cjson/cJSON.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../src/autoconf.h"
#include "../src/cmdu.h"
#include "../src/control.h"
#include "../src/fleet.h"
#include "../src/operation.h"
#include "../src/ovs.h"
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

static void hexfield(const cJSON *o, const char *key, em_buf *out)
{
    memset(out, 0, sizeof(*out));
    em_unhex(str(o, key), out);
}

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
    const cJSON *dev = cJSON_GetObjectItemCaseSensitive(agent, "m1_device");
    const cJSON *radio_json = cJSON_GetObjectItemCaseSensitive(agent, "radio");
    em_binding binding = {0};
    em_parse_mac(str(agent, "al_mac"), binding.local_al);
    em_parse_mac(str(agent, "controller_al"), binding.controller_al);
    memcpy(binding.sources[0], binding.controller_al, 6);
    binding.nsources = 1;
    em_radio_caps radio = {0};
    em_parse_mac(str(radio_json, "ruid"), radio.ruid);
    radio.max_bss = (uint8_t)num(radio_json, "max_bss");
    radio.advanced_flags = (uint8_t)num(radio_json, "advanced_flags");
    const cJSON *cls;
    cJSON_ArrayForEach(cls, cJSON_GetObjectItemCaseSensitive(radio_json, "operating_classes"))
    {
        em_basic_class *c = &radio.classes[radio.nclasses++];
        c->operating_class = (uint8_t)cJSON_GetArrayItem(cls, 0)->valueint;
        c->max_eirp_dbm = (int8_t)cJSON_GetArrayItem(cls, 1)->valueint;
        const cJSON *ch;
        cJSON_ArrayForEach(ch, cJSON_GetArrayItem(cls, 2))
            c->non_operable[c->nnon_operable++] = (uint8_t)ch->valueint;
    }
    em_buf p2;
    hexfield(agent, "profile2_ap_capability", &p2);
    memcpy(radio.profile2, p2.data, 4);
    em_buf_free(&p2);
    const cJSON *search = cJSON_GetObjectItemCaseSensitive(agent, "search");
    em_m1_device d = {0};
    em_buf b;
    hexfield(dev, "uuid", &b);
    memcpy(d.uuid, b.data, 16);
    em_buf_free(&b);
    em_parse_mac(str(dev, "al_mac"), d.al_mac);
    d.authentication_types = (uint16_t)num(dev, "authentication_types");
    d.encryption_types = (uint16_t)num(dev, "encryption_types");
    d.connection_types = (uint8_t)num(dev, "connection_types");
    d.configuration_methods = (uint16_t)num(dev, "configuration_methods");
    d.wps_state = (uint8_t)num(dev, "wps_state");
    hexfield(dev, "manufacturer", &d.manufacturer);
    hexfield(dev, "model_name", &d.model_name);
    hexfield(dev, "model_number", &d.model_number);
    hexfield(dev, "serial_number", &d.serial_number);
    hexfield(dev, "device_name", &d.device_name);
    hexfield(dev, "primary_device_type", &b);
    memcpy(d.primary_device_type, b.data, 8);
    em_buf_free(&b);
    d.rf_band = (uint8_t)num(dev, "rf_band");
    d.association_state = (uint16_t)num(dev, "association_state");
    d.device_password_id = (uint16_t)num(dev, "device_password_id");
    d.configuration_error = (uint16_t)num(dev, "configuration_error");
    d.os_version = (uint32_t)num(dev, "os_version");
    em_buf private_key;
    hexfield(agent, "enrollee_private", &private_key);
    uint8_t nonce[16];
    for (int i = 0; i < 16; i++)
        nonce[i] = (uint8_t)i;
    em_wsc_entropy entropy = {private_key.data, private_key.len, nonce};

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
    em_buf_free(&private_key);
    em_buf_free(&d.manufacturer);
    em_buf_free(&d.model_name);
    em_buf_free(&d.model_number);
    em_buf_free(&d.serial_number);
    em_buf_free(&d.device_name);
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

static void control_vectors(const char *dir)
{
    cJSON *doc = load(dir, "control.json");
    const cJSON *agent = cJSON_GetObjectItemCaseSensitive(doc, "agent");
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
        control.executor = record_mandate;
        control.executor_ctx = handed;
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
            if (!result || !want || strcmp(result, want))
                fail("control", name, result ? result : em_reason_name(error));
            else if (!frames_equal(&out, cJSON_GetObjectItemCaseSensitive(expected, "frames")))
                fail("control", name, "frames differ");
            em_frames_free(&out);
            em_message_free(m);
        }
        checks++;
        if (!cJSON_Compare(handed, cJSON_GetObjectItemCaseSensitive(c, "handed_to_pod"), 1))
            fail("control", name, "mandates handed to the pod differ");
        cJSON_Delete(handed);
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
    free(registry);
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
        char name[16];
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

int main(int argc, char **argv)
{
    const char *dir = argc > 1 ? argv[1] : "spec/conformance";
    cmdu_vectors(dir);
    onboarding_vectors(dir);
    northbound_vectors(dir);
    control_vectors(dir);
    southbound_vectors(dir);
    uplink_vectors(dir);
    fleet_vectors(dir);
    telemetry_vectors(dir);
    printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
