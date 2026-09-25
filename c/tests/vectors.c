/* Replays the conformance vectors (spec/conformance) against the C lab prototype.
 * Usage: emosa-vectors <spec/conformance directory> */
#include <cjson/cJSON.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../src/autoconf.h"
#include "../src/cmdu.h"
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

int main(int argc, char **argv)
{
    const char *dir = argc > 1 ? argv[1] : "spec/conformance";
    cmdu_vectors(dir);
    onboarding_vectors(dir);
    printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
