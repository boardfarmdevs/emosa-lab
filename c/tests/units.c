/* SPDX-License-Identifier: Apache-2.0 */
/* Unit checks of the C implementation's own machinery (not the conformance vectors, which
 * c/tests/vectors.c replays): canonical JSON, the secret store, the journal, the
 * operation engine, the schema validator, the file helpers, the stream framing, the channel
 * policy's record, the packet endpoint's refusals. Expected values come from the reference
 * (Python's json, hmac and datetime).
 *
 *   emosa-units SCHEMAS_DIR SCRATCH_DIR
 *
 * Leaves SCRATCH_DIR/journal-c (a journal the reference's tests read back). */
#include <cjson/cJSON.h>
#include <sqlite3.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "canon.h"
#include "channel_store.h"
#include "engine.h"
#include "ethernet.h"
#include "journal.h"
#include "jsonrpc.h"
#include "jschema.h"
#include "scope_telemetry.h"
#include "mqtt.h"
#include "vault.h"

static int checks, failures;

#define CHECK(cond, ...)                                                                                           \
    do {                                                                                                           \
        checks++;                                                                                                  \
        if (!(cond)) {                                                                                             \
            failures++;                                                                                            \
            fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__);                                                   \
            fprintf(stderr, __VA_ARGS__);                                                                          \
            fputc('\n', stderr);                                                                                   \
        }                                                                                                          \
    } while (0)

static void canon(void)
{
    static const struct {
        const char *input, *compact, *spaced;
    } cases[] = {
        {"{\"b\":1,\"a\":[true,null,\"x\\\"y\\\\z\\n\"],\"c\":{\"\\u00e9\":\"\\u00fc\\u20ac\\ud834\\udd1e\",\"d\":1.5}}",
         "{\"a\":[true,null,\"x\\\"y\\\\z\\n\"],\"b\":1,\"c\":{\"d\":1.5,\"\\u00e9\":\"\\u00fc\\u20ac\\ud834\\udd1e\"}}",
         "{\"a\": [true, null, \"x\\\"y\\\\z\\n\"], \"b\": 1, \"c\": {\"d\": 1.5, \"\\u00e9\": \"\\u00fc\\u20ac\\ud834\\udd1e\"}}"},
        {"[\"82:00:00:00:01:00\",0,-3,1e20,0.1,\"\\u0001\\u007f\"]",
         "[\"82:00:00:00:01:00\",0,-3,1e+20,0.1,\"\\u0001\\u007f\"]",
         "[\"82:00:00:00:01:00\", 0, -3, 1e+20, 0.1, \"\\u0001\\u007f\"]"},
        {"{\"ssid\":\"home\",\"enabled\":true,\"additional\":[[\"backhaul\",\"bh\",null]]}",
         "{\"additional\":[[\"backhaul\",\"bh\",null]],\"enabled\":true,\"ssid\":\"home\"}",
         "{\"additional\": [[\"backhaul\", \"bh\", null]], \"enabled\": true, \"ssid\": \"home\"}"},
    };
    for (size_t i = 0; i < sizeof(cases) / sizeof(*cases); i++) {
        cJSON *v = cJSON_Parse(cases[i].input);
        char *a = em_json_dumps(v, EM_JSON_COMPACT, true), *b = em_json_dumps(v, EM_JSON_DEFAULT, true);
        CHECK(a && !strcmp(a, cases[i].compact), "compact %zu: %s", i, a);
        CHECK(b && !strcmp(b, cases[i].spaced), "spaced %zu: %s", i, b);
        free(a);
        free(b);
        cJSON_Delete(v);
    }
    /* the start instance: sha256(json.dumps(sorted UUIDs))[:16] */
    cJSON *radios = cJSON_Parse("[\"a-uuid\",\"b-uuid\"]");
    char *text = em_json_dumps(radios, EM_JSON_DEFAULT, false), hex[65];
    em_sha256_hex(text, strlen(text), hex);
    CHECK(!strncmp(hex, "e4c0c6572af2fb4f", 16), "start instance %s", hex);
    free(text);
    cJSON_Delete(radios);
    char out[40];
    CHECK(em_utc_add("2026-09-29T12:34:56.789Z", 30, out) && !strcmp(out, "2026-09-29T12:35:26.789000+00:00"),
          "deadline %s", out);
    CHECK(em_utc_add("2026-09-29T12:34:56.000Z", 4, out) && !strcmp(out, "2026-09-29T12:35:00+00:00"),
          "deadline %s", out);
    double d;
    CHECK(em_utc_diff("2026-09-29T12:34:56.789Z", "2026-09-29T12:35:26.789000+00:00", &d) && d > 29.999 && d < 30.001,
          "diff %f", d);
}

static void vault(const char *scratch)
{
    char dir[600], key[700];
    snprintf(dir, sizeof(dir), "%s/secrets", scratch);
    mkdir(dir, 0700);
    snprintf(key, sizeof(key), "%s/.fingerprint-key", dir);
    FILE *f = fopen(key, "wb");
    for (int i = 0; i < 32; i++)
        fputc(i, f);
    fclose(f);
    chmod(key, 0600);
    em_vault v;
    CHECK(em_vault_open(&v, dir) == EM_OK, "vault open");
    char fp[65];
    em_vault_fingerprint_text(&v, "secret-pass", fp);
    CHECK(!strcmp(fp, "84ed201cb170309858f8577bb1c56d69f0831433f99150154798726621060576"), "fingerprint %s", fp);
    cJSON *value = cJSON_Parse("{\"target\":{\"x\":1},\"pod_id\":\"p\"}");
    em_vault_fingerprint(&v, value, fp);
    CHECK(!strcmp(fp, "fe620b40e60daa123aeb427da5514d0fda3ff75b912498d7cd982ce8b6080c78"), "fingerprint %s", fp);
    cJSON_Delete(value);
    const char *ref = "wsc-0123456789abcdef0123456789abcdef";
    CHECK(em_vault_persist_received(&v, ref, "correct horse") == EM_OK, "persist");
    CHECK(em_vault_persist_received(&v, ref, "correct horse") != EM_OK, "never overwritten");
    CHECK(em_vault_persist_received(&v, "../x", "correct horse") != EM_OK, "reference pattern");
    em_reason why;
    char *got = em_vault_resolve(&v, ref, &why);
    CHECK(got && !strcmp(got, "correct horse"), "resolve");
    free(got);
    CHECK(!em_vault_resolve(&v, "absent", &why) && why == EM_MISSING_PREREQUISITE, "absent reference");
    /* forget stays in the vault: a name with a path in it is not removed */
    char outside[700];
    snprintf(outside, sizeof(outside), "%s/outside", scratch);
    FILE *o = fopen(outside, "w");
    if (o)
        fclose(o);
    em_vault_forget(&v, "../outside");
    CHECK(access(outside, F_OK) == 0, "forget refuses a path out of the vault");
    em_vault_forget(&v, ref);
    CHECK(!em_vault_resolve(&v, ref, &why), "forget removes a secret");
}

/* -- the MQTT subscriber's parser (mqtt.c), through its fuzzing entry --------------------- */

static size_t mqtt_delivered;
static char mqtt_topic[64];

static void mqtt_deliver(void *ctx, const char *topic, const uint8_t *payload, size_t len, bool retained)
{
    (void)ctx;
    (void)payload;
    (void)len;
    (void)retained;
    mqtt_delivered++;
    snprintf(mqtt_topic, sizeof(mqtt_topic), "%s", topic);
}

/* the broker's bytes in one phase of a fresh connection: what em_mqtt_input says */
static bool mqtt_feed(int awaiting, const uint8_t *bytes, size_t n)
{
    em_mqtt *m = em_mqtt_open("127.0.0.1", 1, "emosa/stats/MVXPOD023F87E628DD", "units", mqtt_deliver, NULL);
    bool ok = em_mqtt_input(m, awaiting, bytes, n, 1.0);
    em_mqtt_close(m);
    return ok;
}

static void mqtt_parser(void)
{
    static const uint8_t connack[] = {0x20, 2, 0, 0}, refused_connack[] = {0x20, 2, 0, 5};
    static const uint8_t suback[] = {0x90, 3, 0, 1, 0}, refused_suback[] = {0x90, 3, 0, 1, 0x80};
    static const uint8_t publish0[] = {0x30, 7, 0, 3, 'a', '/', 'b', 'x', 'y'};
    static const uint8_t publish1[] = {0x32, 9, 0, 3, 'a', '/', 'b', 0, 7, 'x', 'y'};
    static const uint8_t nul_topic[] = {0x30, 7, 0, 3, 'a', 0, 'b', 'x', 'y'};
    static const uint8_t short_publish[] = {0x30, 1, 0};
    static const uint8_t long_topic[] = {0x30, 4, 0, 9, 'a', 'b'};
    static const uint8_t pingresp[] = {0xD0, 0}, other[] = {0x40, 2, 0, 1};
    static const uint8_t partial[] = {0x30}, five_bytes[] = {0x30, 0x80, 0x80, 0x80, 0x80, 0x01};
    static const uint8_t too_long[] = {0x30, 0xFF, 0xFF, 0xFF, 0x7F};
    /* the CONNACK needs the SUBSCRIBE sent, which this unconnected client cannot */
    CHECK(!mqtt_feed(0, connack, sizeof(connack)), "a CONNACK without a socket");
    CHECK(!mqtt_feed(0, refused_connack, sizeof(refused_connack)), "a refused CONNACK");
    CHECK(!mqtt_feed(1, connack, sizeof(connack)), "a CONNACK out of turn");
    CHECK(mqtt_feed(1, suback, sizeof(suback)), "a SUBACK subscribes");
    CHECK(!mqtt_feed(1, refused_suback, sizeof(refused_suback)), "a refused SUBACK");
    CHECK(!mqtt_feed(2, suback, sizeof(suback)), "a SUBACK out of turn");
    mqtt_delivered = 0;
    CHECK(mqtt_feed(2, publish0, sizeof(publish0)) && mqtt_delivered == 1 && !strcmp(mqtt_topic, "a/b"),
          "a QoS 0 PUBLISH delivered");
    CHECK(mqtt_feed(2, publish1, sizeof(publish1)) && mqtt_delivered == 2, "a QoS 1 PUBLISH delivered");
    CHECK(mqtt_feed(1, publish0, sizeof(publish0)) && mqtt_delivered == 2, "not delivered before the SUBACK");
    CHECK(!mqtt_feed(2, nul_topic, sizeof(nul_topic)) && mqtt_delivered == 2, "a topic with a NUL refused");
    CHECK(!mqtt_feed(2, short_publish, sizeof(short_publish)), "a PUBLISH without its topic length");
    CHECK(!mqtt_feed(2, long_topic, sizeof(long_topic)), "a topic longer than its packet");
    CHECK(mqtt_feed(2, pingresp, sizeof(pingresp)) && mqtt_feed(2, other, sizeof(other)), "others ignored");
    CHECK(mqtt_feed(2, partial, sizeof(partial)), "an incomplete packet waits");
    CHECK(!mqtt_feed(2, five_bytes, sizeof(five_bytes)), "a remaining length over four bytes");
    CHECK(!mqtt_feed(2, too_long, sizeof(too_long)), "a packet over the limit");
}

/* -- another secret backend: the store's policy over a platform's storage --------------- */

typedef struct {
    char refs[4][100];
    char values[4][64];
    int n, creates, removes;
} memory_secrets;

static uint8_t *memory_read(const em_vault *v, const char *ref, size_t *len)
{
    memory_secrets *m = v->ctx;
    for (int i = 0; i < m->n; i++)
        if (!strcmp(m->refs[i], ref)) {
            *len = strlen(m->values[i]);
            return (uint8_t *)strdup(m->values[i]);
        }
    return NULL;
}

static bool memory_create(const em_vault *v, const char *ref, const uint8_t *data, size_t len)
{
    memory_secrets *m = v->ctx;
    m->creates++;
    for (int i = 0; i < m->n; i++)
        if (!strcmp(m->refs[i], ref))
            return false;
    if (m->n == 4 || len >= sizeof(m->values[0]))
        return false;
    snprintf(m->refs[m->n], sizeof(m->refs[0]), "%s", ref);
    memcpy(m->values[m->n], data, len);
    m->values[m->n++][len] = 0;
    return true;
}

static void memory_remove(const em_vault *v, const char *ref)
{
    memory_secrets *m = v->ctx;
    m->removes++;
    for (int i = 0; i < m->n; i++)
        if (!strcmp(m->refs[i], ref)) {
            m->n--;
            memmove(m->refs[i], m->refs[m->n], sizeof(m->refs[0]));
            memmove(m->values[i], m->values[m->n], sizeof(m->values[0]));
            return;
        }
}

static bool memory_key(const em_vault *v, uint8_t out[32])
{
    (void)v;
    for (int i = 0; i < 32; i++)
        out[i] = (uint8_t)i;
    return true;
}

static void secret_backend(void)
{
    memory_secrets m = {.n = 1, .refs = {"backhaul"}, .values = {"short"}};
    em_vault v;
    em_secret_backend backend = {"memory", memory_read, memory_create, memory_remove, memory_key};
    CHECK(em_vault_open_backend(&v, backend, &m) == EM_OK, "another backend opens");
    char fp[65];
    em_vault_fingerprint_text(&v, "secret-pass", fp); /* the key above is the files test's */
    CHECK(!strcmp(fp, "84ed201cb170309858f8577bb1c56d69f0831433f99150154798726621060576"), "fingerprint %s", fp);
    em_reason why;
    CHECK(!em_vault_resolve(&v, "backhaul", &why) && why == EM_INVALID_INPUT, "the policy: a short passphrase");
    CHECK(!em_vault_resolve(&v, "../backhaul", &why) && why == EM_INVALID_INPUT && !m.creates,
          "the policy: a reference with a path never reaches the backend");
    const char *ref = "wsc-0123456789abcdef0123456789abcdef-1";
    CHECK(em_vault_persist_received(&v, ref, "correct horse") == EM_OK, "persist through the backend");
    CHECK(em_vault_persist_received(&v, ref, "correct horse") != EM_OK, "never overwritten");
    CHECK(em_vault_persist_received(&v, ref, "bad\x01value") != EM_OK && m.creates == 2,
          "the policy: an unprintable value never reaches the backend");
    char *got = em_vault_resolve(&v, ref, &why);
    CHECK(got && !strcmp(got, "correct horse"), "resolve through the backend");
    free(got);
    em_vault_forget(&v, "../x");
    CHECK(m.removes == 0, "the policy: forget refuses a path");
    em_vault_forget(&v, ref);
    CHECK(m.removes == 1 && !em_vault_resolve(&v, ref, &why) && why == EM_MISSING_PREREQUISITE,
          "forget through the backend");
}

/* -- a scope whose pod has one SSID ----------------------------------------------------- */

typedef struct {
    char configured[33], observed[33];
    bool lose_reply;
    int generation;
} fake_pod;

static double fake_clock_value;
static double fake_clock(void) { return fake_clock_value; }

static cJSON *fake_target(void *ctx, const cJSON *intent, em_reason *why)
{
    (void)ctx;
    const char *ssid = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(intent, "ssid"));
    if (!ssid || !*ssid) {
        *why = EM_INVALID_INPUT;
        return NULL;
    }
    cJSON *t = cJSON_CreateObject();
    cJSON_AddStringToObject(t, "ssid", ssid);
    return t;
}

static bool fake_snapshot(void *ctx, em_snapshot *out)
{
    fake_pod *p = ctx;
    memset(out, 0, sizeof(*out));
    out->config = cJSON_CreateObject();
    cJSON_AddStringToObject(out->config, "ssid", p->configured);
    cJSON *values = cJSON_CreateObject();
    cJSON_AddStringToObject(values, "ssid", p->observed);
    out->observed = em_observation("pod-1", "bss-1", values, "fake", p->generation, true, "fake:state", 1);
    out->ready = true;
    out->generation = p->generation;
    strcpy(out->schema_fingerprint, "0000000000000000000000000000000000000000000000000000000000000000");
    return true;
}

static cJSON *fake_plan(void *ctx, const cJSON *intent, em_reason *why)
{
    (void)ctx;
    (void)intent;
    (void)why;
    cJSON *p = cJSON_CreateObject();
    cJSON_AddStringToObject(p, "action", "update");
    return p;
}

static void fake_submit(void *ctx, const cJSON *intent, const cJSON *attempt, em_submit_out *out)
{
    fake_pod *p = ctx;
    (void)attempt;
    if (p->lose_reply) {
        strcpy(out->status, "unknown");
        out->reason = EM_OUTCOME_UNKNOWN;
        return;
    }
    snprintf(p->configured, sizeof(p->configured), "%s",
             cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(intent, "ssid")));
    strcpy(out->status, "committed");
    out->evidence = cJSON_CreateObject();
    cJSON_AddStringToObject(out->evidence, "attribution", "reply");
    cJSON_AddTrueToObject(out->evidence, "transaction_validated");
}

static cJSON *intent_for(const char *ssid)
{
    cJSON *i = cJSON_CreateObject();
    cJSON_AddStringToObject(i, "pod_id", "pod-1");
    cJSON_AddStringToObject(i, "radio_id", "radio-1");
    cJSON_AddStringToObject(i, "bss_id", "bss-1");
    cJSON_AddStringToObject(i, "ssid", ssid);
    cJSON_AddStringToObject(i, "secret_ref", "primary");
    cJSON_AddTrueToObject(i, "enabled");
    cJSON_AddStringToObject(i, "security_mode", "wpa2-psk");
    return i;
}

static void engine(const char *scratch, const char *schemas)
{
    char dir[600], vdir[600];
    snprintf(dir, sizeof(dir), "%s/journal-c", scratch);
    snprintf(vdir, sizeof(vdir), "%s/secrets", scratch);
    em_journal_schemas s = {em_schema_load(schemas, "operation"), em_schema_load(schemas, "event"),
                            em_schema_load(schemas, "wsc-receipt")};
    CHECK(s.operation && s.event && s.receipt, "schemas loaded from %s", schemas);
    em_reason why;
    em_journal *j = em_journal_open(dir, &s, &why);
    CHECK(j != NULL, "journal open %s", em_reason_name(why));
    CHECK(!em_journal_open(dir, &s, &why) && why == EM_BUSY, "one writer at a time");
    em_vault v;
    em_vault_open(&v, vdir);
    fake_pod pod = {"old", "old", false, 1};
    em_backend backend = {"fake", fake_target, fake_snapshot, fake_plan, fake_submit};
    em_engine e;
    fake_clock_value = 100;
    em_engine_init(&e, j, &v, "pod-1", backend, &pod, fake_clock);

    cJSON *intent = intent_for("home");
    cJSON *op = em_engine_request(&e, intent, "unit", "key-1", "run-1", 30, "semantic", NULL, &why);
    CHECK(op && !strcmp(em_state_of(op), "REQUESTED"), "requested");
    cJSON *again = em_engine_request(&e, intent, "unit", "key-1", "run-1", 30, "semantic", NULL, &why);
    CHECK(again && !strcmp(cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(again, "operation_id")),
                           cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(op, "operation_id"))),
          "the same key returns the same operation");
    cJSON *other = intent_for("elsewhere");
    CHECK(!em_engine_request(&e, other, "unit", "key-1", "run-1", 30, "semantic", NULL, &why) &&
              why == EM_INVALID_INPUT,
          "a key reused for another intent");
    cJSON *busy = em_engine_request(&e, other, "unit", "key-2", "run-1", 30, "semantic", NULL, &why);
    CHECK(busy && !strcmp(em_state_of(busy), "REJECTED") &&
              !strcmp(cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(busy, "reason")), "BUSY"),
          "one active operation per pod");
    const char *id = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(op, "operation_id"));
    cJSON *done = em_engine_execute(&e, id);
    CHECK(done && !strcmp(em_state_of(done), "CONFIG_COMMITTED"), "committed: %s", em_state_of(done));
    strcpy(pod.observed, "home");
    fake_clock_value = 101;
    em_engine_reconcile(&e);
    cJSON *seen = em_journal_get(j, id);
    CHECK(seen && !strcmp(em_state_of(seen), "OBSERVED_APPLIED"), "applied: %s", em_state_of(seen));
    CHECK(cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(seen, "timings"), "OBSERVED_APPLIED"),
          "timing recorded");
    /* a lost reply, then the deadline */
    pod.lose_reply = true;
    cJSON *third = intent_for("third");
    cJSON *op3 = em_engine_request(&e, third, "unit", "key-3", "run-1", 5, "semantic", NULL, &why);
    const char *id3 = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(op3, "operation_id"));
    cJSON *r3 = em_engine_execute(&e, id3);
    CHECK(r3 && !strcmp(em_state_of(r3), "INDETERMINATE"), "indeterminate: %s", em_state_of(r3));
    fake_clock_value = 110;
    em_engine_reconcile(&e);
    cJSON *t3 = em_journal_get(j, id3);
    CHECK(t3 && !strcmp(em_state_of(t3), "TIMED_OUT"), "timed out: %s", em_state_of(t3));
    /* recovery: a SUBMITTED record becomes INDETERMINATE */
    cJSON *fourth = intent_for("fourth");
    cJSON *op4 = em_engine_request(&e, fourth, "unit", "key-4", "run-1", 5, "semantic", NULL, &why);
    cJSON_ReplaceItemInObjectCaseSensitive(op4, "state", cJSON_CreateString("VALIDATED"));
    cJSON_AddItemToArray(cJSON_GetObjectItemCaseSensitive(op4, "attempts"), cJSON_Parse("{\"attempt_id\":\"a\",\"transaction_id\":\"t\",\"session_generation\":1,\"prepared_at\":\"2026-09-29T00:00:00.000Z\"}"));
    em_transition(op4, "SUBMITTED", NULL);
    em_engine_save(&e, op4, NULL);
    em_engine_recover(&e);
    cJSON *r4 = em_journal_get(j, cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(op4, "operation_id")));
    CHECK(r4 && !strcmp(em_state_of(r4), "INDETERMINATE"), "recovered: %s", em_state_of(r4));
    cJSON *events = em_journal_events(j, "run-1", 0, 500);
    CHECK(cJSON_GetArraySize(events) >= 8, "events %d", cJSON_GetArraySize(events));
    /* guards */
    cJSON *probe = cJSON_Duplicate(r4, true);
    CHECK(!em_transition(probe, "REQUESTED", NULL), "no way back");
    CHECK(!em_transition(probe, "TIMED_OUT", NULL), "timed out only after the deadline");
    cJSON *evidence = cJSON_Parse("{\"fresh\":true}");
    CHECK(!em_transition(probe, "OBSERVED_APPLIED", evidence), "applied needs the predicate");
    cJSON_Delete(evidence);
    cJSON_Delete(probe);
    cJSON *owned = cJSON_Parse("{\"reason\":\"x\"}");
    em_journal_conflict(j, "pod-1", owned);
    cJSON *o = em_journal_ownership(j, "pod-1");
    CHECK(o != NULL, "ownership recorded");
    cJSON_Delete(o);
    em_journal_release(j, "pod-1");
    cJSON_Delete(owned);
    for (cJSON **x = (cJSON *[]){intent, op, again, other, busy, done, seen, third, op3, r3, t3, fourth, op4, r4, events, NULL}; *x; x++)
        cJSON_Delete(*x);
    em_engine_free(&e);
    em_journal_close(j);
    em_schema_free(s.operation);
    em_schema_free(s.event);
    em_schema_free(s.receipt);
}

static void files(const char *scratch)
{
    char path[512], tmp[520], big[512];
    snprintf(path, sizeof(path), "%s/file.json", scratch);
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    snprintf(big, sizeof(big), "%s/big", scratch);
    CHECK(em_write_file(path, "{\"a\": 1}\n", true), "a durable write");
    CHECK(access(tmp, F_OK) != 0, "no temporary file left");
    size_t len = 0;
    char *text = em_read_file(path, 64, &len);
    CHECK(text && len == 9 && !strcmp(text, "{\"a\": 1}\n"), "read back");
    free(text);
    CHECK(em_write_file(path, "second", false), "a write over it");
    text = em_read_file(path, 64, NULL);
    CHECK(text && !strcmp(text, "second"), "the new content, whole");
    free(text);
    CHECK(!em_read_file(path, 3, NULL), "longer than the limit: refused");
    CHECK(!em_read_file(scratch, 64, NULL), "a directory: NULL");
    snprintf(tmp, sizeof(tmp), "%s/missing/file.json", scratch);
    CHECK(!em_write_file(tmp, "x", true), "an unwritable path: false");
    CHECK(!em_read_file(tmp, 64, NULL), "a missing file: NULL");
    /* larger than one read: the buffer grows */
    char *blob = em_malloc(20001);
    memset(blob, 'x', 20000);
    blob[20000] = 0;
    CHECK(em_write_file(big, blob, false), "a large write");
    text = em_read_file(big, 1 << 20, &len);
    CHECK(text && len == 20000 && !memcmp(text, blob, 20000), "a large read");
    free(text);
    free(blob);
}

/* the messages of stream, scanned as it arrives in pieces of `piece` bytes (0: whole),
 * joined by '|', as ovsdb.c and fleetd.c frame them */
static void scanned(const char *stream, size_t piece, char *out, size_t size)
{
    em_json_scanner sc = {0};
    char buf[256];
    size_t len = 0, total = strlen(stream), fed = 0;
    out[0] = 0;
    while (fed < total) {
        size_t n = piece && total - fed > piece ? piece : total - fed;
        memcpy(buf + len, stream + fed, n);
        len += n;
        fed += n;
        size_t start = 0;
        while (start < len) {
            size_t at, end;
            bool complete = em_json_scan(&sc, buf + start, len - start, &at, &end);
            start += at;
            if (!complete)
                break;
            snprintf(out + strlen(out), size - strlen(out), "%s%.*s", out[0] ? "|" : "", (int)(end - at), buf + start);
            start += end - at;
        }
        memmove(buf, buf + start, len - start);
        len -= start;
    }
}

static void framing(void)
{
    /* brackets and quotes inside strings, escapes, whitespace between messages */
    const char *stream = " {\"id\":1,\"s\":\"a}\\\"[\"}\n\t[1,[2,{}]]\r\n{\"e\":\"\\\\\"}  {\"open\":";
    const char *expected = "{\"id\":1,\"s\":\"a}\\\"[\"}|[1,[2,{}]]|{\"e\":\"\\\\\"}";
    char whole[256], piecewise[256];
    scanned(stream, 0, whole, sizeof(whole));
    CHECK(!strcmp(whole, expected), "whole: %s", whole);
    for (size_t piece = 1; piece <= 7; piece++) {
        scanned(stream, piece, piecewise, sizeof(piecewise));
        CHECK(!strcmp(piecewise, expected), "in pieces of %zu: %s", piece, piecewise);
    }
    /* whitespace alone is dropped as it comes; a stray closing bracket ends a message the
     * parser then refuses */
    em_json_scanner sc = {0};
    size_t at = 0, end = 0;
    CHECK(!em_json_scan(&sc, " \n ", 3, &at, &end) && at == 3, "whitespace dropped: %zu", at);
    CHECK(em_json_next(" x]", 3, &at, &end) && at == 1 && end == 3, "stray bracket: %zu %zu", at, end);
}

static void channel_store(const char *scratch)
{
    char path[512];
    snprintf(path, sizeof(path), "%s/channel-policy.sqlite", scratch);
    em_channel_store *s = em_channel_store_open(path);
    CHECK(s != NULL, "a new store");
    cJSON *none = em_channel_store_read(s);
    CHECK(none == NULL, "nothing accepted yet");
    cJSON *first = cJSON_Parse("{\"radios\":[{\"ruid\":\"02:00:00:00:01:00\",\"preferences\":[]}],\"generation\":1}");
    cJSON *second = cJSON_Parse("{\"radios\":[],\"generation\":2}");
    CHECK(em_channel_store_save(s, first), "the first policy kept");
    cJSON *back = em_channel_store_read(s);
    CHECK(back && cJSON_Compare(back, first, true), "read back");
    cJSON_Delete(back);
    CHECK(em_channel_store_save(s, second), "a later policy replaces it");
    /* over the 8 KiB budget: refused, the record kept as it was */
    cJSON *big = cJSON_CreateObject();
    char *filler = em_malloc(8200);
    memset(filler, 'x', 8199);
    filler[8199] = 0;
    cJSON_AddStringToObject(big, "filler", filler);
    free(filler);
    CHECK(!em_channel_store_save(s, big), "a record over its budget refused");
    cJSON_Delete(big);
    em_channel_store_close(s);
    s = em_channel_store_open(path);
    back = s ? em_channel_store_read(s) : NULL;
    CHECK(back && cJSON_Compare(back, second, true), "the later policy, after reopening");
    cJSON_Delete(back);
    em_channel_store_close(s);
    /* a record that is not JSON reads as none; a store that cannot be made is NULL */
    sqlite3 *db = NULL;
    if (sqlite3_open(path, &db) == SQLITE_OK)
        sqlite3_exec(db, "UPDATE channel_policy SET value='{' WHERE id=1", NULL, NULL, NULL);
    sqlite3_close(db);
    s = em_channel_store_open(path);
    back = s ? em_channel_store_read(s) : NULL;
    CHECK(s && !back, "an unparsable record: none");
    cJSON_Delete(back);
    em_channel_store_close(s);
    snprintf(path, sizeof(path), "%s/missing/channel-policy.sqlite", scratch);
    CHECK(em_channel_store_open(path) == NULL, "an unusable path: NULL");
    em_channel_store_close(NULL);
    cJSON_Delete(first);
    cJSON_Delete(second);
}

static void ethernet(void)
{
    /* what is refused before any packet socket (the box opens real ones) */
    const uint8_t local[6] = {0x02, 0x72, 0xf9, 0x7f, 0x07, 0x85}, group[6] = {0x01, 0x80, 0xc2, 0x00, 0x00, 0x13};
    em_ethernet e;
    CHECK(em_ethernet_open(&e, "no-such-if0", local) == EM_INVALID_INPUT && e.fd == -1, "no such interface");
    CHECK(em_ethernet_open(&e, "lo", group) == EM_INVALID_INPUT, "a multicast local address");
    em_reason r = em_ethernet_open(&e, "lo", local);
    CHECK(r == EM_NOT_READY || r == EM_INVALID_INPUT, "lo: no packet socket unprivileged, not Ethernet with it: %d", r);
    em_ethernet_close(&e);
    CHECK(e.fd == -1, "closed");
    uint8_t frame[64] = {0};
    memcpy(frame, group, 6);
    memcpy(frame + 6, local, 6);
    frame[12] = 0x89;
    frame[13] = 0x3a;
    CHECK(em_ethernet_send(&e, frame, 21) == EM_INVALID_INPUT, "shorter than a CMDU header");
    frame[14] = 1;
    CHECK(em_ethernet_send(&e, frame, sizeof(frame)) == EM_INVALID_INPUT, "a message version other than 0");
    frame[14] = 0;
    frame[11] ^= 1;
    CHECK(em_ethernet_send(&e, frame, sizeof(frame)) == EM_INVALID_INPUT, "another source address");
}

static void telemetry_limits(void)
{
    /* the reference refuses a topic over 128 and a broker over 253 characters; a value
     * cut to fit its buffer would pass the same checks, so it must be refused first */
    char topic[201], broker[301];
    memset(topic, 't', 200);
    topic[200] = 0;
    memset(broker, 'b', 300);
    broker[300] = 0;
    em_telemetry_intent intent;
    em_reason why;
    cJSON *c = cJSON_Parse("{\"broker\": \"10.101.0.40\", \"port\": 8883}");
    CHECK(em_telemetry_intent_from("pod-1", "SERIAL", c, &intent, &why), "a valid intent");
    cJSON_AddStringToObject(c, "topic", topic);
    CHECK(!em_telemetry_intent_from("pod-1", "SERIAL", c, &intent, &why), "a 200-character topic refused");
    cJSON_Delete(c);
    c = cJSON_CreateObject();
    cJSON_AddStringToObject(c, "broker", broker);
    CHECK(!em_telemetry_intent_from("pod-1", "SERIAL", c, &intent, &why), "a 300-character broker refused");
    cJSON_Delete(c);
}

static void schema(const char *schemas)
{
    em_schema *config = em_schema_load(schemas, "agent-config");
    CHECK(config != NULL, "agent-config schema");
    cJSON *ok = cJSON_Parse("{\"pod_id\":\"MVXPOD0\",\"serial\":\"MVXPOD0\",\"ovsdb\":\"ptcp:6652:127.0.0.1\","
                            "\"interface\":\"em2\",\"al_mac\":\"02:72:f9:7f:07:85\","
                            "\"controller_al\":\"00:60:2f:da:68:d4\",\"state_dir\":\"/var/lib/emosa/x\"}");
    char where[256];
    bool valid = em_schema_valid(config, ok, where, sizeof(where));
    CHECK(valid, "a minimal configuration: %s", where);
    cJSON_ReplaceItemInObjectCaseSensitive(ok, "al_mac", cJSON_CreateString("not-a-mac"));
    CHECK(!em_schema_valid(config, ok, where, sizeof(where)) && strstr(where, "/al_mac"), "bad MAC: %s", where);
    cJSON_Delete(ok);
    em_schema_free(config);
}

int main(int argc, char **argv)
{
    em_init();
    if (argc != 3) {
        fprintf(stderr, "usage: emosa-units SCHEMAS_DIR SCRATCH_DIR\n");
        return 2;
    }
    mkdir(argv[2], 0700);
    canon();
    vault(argv[2]);
    secret_backend();
    mqtt_parser();
    engine(argv[2], argv[1]);
    schema(argv[1]);
    files(argv[2]);
    telemetry_limits();
    framing();
    channel_store(argv[2]);
    ethernet();
    printf("%d checks, %d failures\n", checks, failures);
    return failures != 0;
}
