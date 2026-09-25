/* Replays the conformance vectors (spec/conformance) against the C lab prototype.
 * Usage: emosa-vectors <spec/conformance directory> */
#include <cjson/cJSON.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../src/cmdu.h"

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

int main(int argc, char **argv)
{
    const char *dir = argc > 1 ? argv[1] : "spec/conformance";
    cmdu_vectors(dir);
    printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
