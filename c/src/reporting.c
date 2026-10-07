/* SPDX-License-Identifier: Apache-2.0 */
/* The Multi-AP policy, the due-report schedule and the pod's AP metrics
 * (emosa.wire.reporting_policy, emosa.wire.pod_metrics). */
#include "reporting.h"

#include <math.h>
#include <sqlite3.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "canon.h"

#define NOISE_FLOOR_DBM (-96)

/* -- the store -------------------------------------------------------------------------- */

struct em_policy_store {
    sqlite3 *db;
    char boot_id[129];
};

em_policy_store *em_policy_store_open(const char *path, const char *boot_id)
{
    if (!boot_id || !*boot_id || strlen(boot_id) > 128)
        return NULL; /* "OS boot identity required for durable reporting schedule" */
    em_policy_store *s = em_calloc(1, sizeof(*s));
    EM_FORMAT_FIXED(s->boot_id, sizeof(s->boot_id), "%s", boot_id); /* 128 at most, checked above */
    if (sqlite3_open(path, &s->db) != SQLITE_OK ||
        sqlite3_exec(s->db,
                     "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;"
                     "CREATE TABLE IF NOT EXISTS reporting_policy (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);",
                     NULL, NULL, NULL) != SQLITE_OK) {
        em_policy_store_close(s);
        return NULL;
    }
    sqlite3_busy_timeout(s->db, 2000);
    return s;
}

void em_policy_store_close(em_policy_store *s)
{
    if (!s)
        return;
    if (s->db)
        sqlite3_close(s->db);
    free(s);
}

const char *em_policy_store_boot_id(const em_policy_store *s) { return s->boot_id; }

static cJSON *store_read(em_policy_store *s)
{
    sqlite3_stmt *st;
    cJSON *v = NULL;
    if (sqlite3_prepare_v2(s->db, "SELECT value FROM reporting_policy WHERE id=1", -1, &st, NULL) == SQLITE_OK) {
        if (sqlite3_step(st) == SQLITE_ROW)
            v = cJSON_Parse((const char *)sqlite3_column_text(st, 0));
        sqlite3_finalize(st);
    }
    return v;
}

static bool store_save(em_policy_store *s, const cJSON *value)
{
    char *text = em_json_dumps(value, EM_JSON_DEFAULT, true);
    if (!text || strlen(text) > 32768) {
        free(text);
        return false; /* "reporting policy record exceeds budget" */
    }
    sqlite3_stmt *st;
    bool ok = false;
    if (sqlite3_prepare_v2(s->db,
                           "INSERT INTO reporting_policy VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                           -1, &st, NULL) == SQLITE_OK) {
        ok = sqlite3_bind_text(st, 1, text, -1, SQLITE_TRANSIENT) == SQLITE_OK && sqlite3_step(st) == SQLITE_DONE;
        sqlite3_finalize(st);
    }
    free(text);
    return ok;
}

/* -- counters ----------------------------------------------------------------------------- */

typedef struct {
    size_t n;
    struct {
        char name[64];
        unsigned count;
    } items[16];
} counters16;

static const char *record(void *counters, const char *name)
{
    counters16 *c = counters;
    for (size_t i = 0; i < c->n; i++)
        if (!strcmp(c->items[i].name, name)) {
            c->items[i].count++;
            return name;
        }
    if (c->n < 16) {
        EM_FORMAT_FIXED(c->items[c->n].name, sizeof(c->items[c->n].name), "%s", name);
        c->items[c->n++].count = 1;
    }
    return name;
}

static cJSON *counts_json(const void *counters)
{
    const counters16 *c = counters;
    cJSON *o = cJSON_CreateObject();
    for (size_t i = 0; i < c->n; i++)
        cJSON_AddNumberToObject(o, c->items[i].name, c->items[i].count);
    return o;
}

/* -- the TLV vector and frames ------------------------------------------------------------ */

void em_tlv_vec_free(em_tlv_vec *v)
{
    for (size_t i = 0; i < v->count; i++)
        free(v->tlvs[i].value);
    free(v->tlvs);
    memset(v, 0, sizeof(*v));
}

static void push(em_tlv_vec *v, uint8_t kind, const uint8_t *value, size_t len)
{
    if (v->count == v->cap) {
        v->cap = v->cap ? v->cap * 2 : 16;
        v->tlvs = em_realloc(v->tlvs, v->cap * sizeof(*v->tlvs));
    }
    em_tlv *t = &v->tlvs[v->count++];
    t->kind = kind;
    t->len = (uint16_t)len;
    t->value = em_malloc(len ? len : 1);
    memcpy(t->value, value, len);
}

/* the message's frames appended to out */
static bool send(const uint8_t dst[6], const uint8_t src[6], uint16_t type, uint16_t mid, const em_tlv *tlvs,
                 size_t n, em_frames *out)
{
    em_frames f;
    if (em_fragment(dst, src, type, mid, tlvs, n, false, EM_MAX_CMDU, &f) != EM_OK)
        return false;
    out->frames = em_realloc(out->frames, (out->count + f.count) * sizeof(*out->frames));
    memcpy(out->frames + out->count, f.frames, f.count * sizeof(*f.frames));
    out->count += f.count;
    free(f.frames);
    return true;
}

/* -- the policy (reporting_policy.decode_policy) ------------------------------------------ */

static bool unicast(const uint8_t *m)
{
    static const uint8_t zero[6] = {0};
    return memcmp(m, zero, 6) && !(m[0] & 1);
}

static cJSON *hex_of(const uint8_t *m)
{
    char *h = em_hex(m, 6);
    cJSON *s = cJSON_CreateString(h);
    free(h);
    return s;
}

/* addresses(): a counted list of distinct unicast MACs; *used its length */
static bool addresses(const uint8_t *d, size_t len, cJSON **list, size_t *used)
{
    if (!len || len < 1 + 6 * (size_t)d[0])
        return false;
    *list = cJSON_CreateArray();
    for (size_t i = 0; i < d[0]; i++) {
        const uint8_t *m = d + 1 + 6 * i;
        if (!unicast(m)) {
            cJSON_Delete(*list);
            return false;
        }
        for (size_t k = 0; k < i; k++)
            if (!memcmp(m, d + 1 + 6 * k, 6)) {
                cJSON_Delete(*list);
                return false; /* "duplicate policy station identity" */
            }
        cJSON_AddItemToArray(*list, hex_of(m));
    }
    *used = 1 + 6 * (size_t)d[0];
    return true;
}

static cJSON *decode_policy(const em_message *m, const uint8_t ruid[6], em_reason *why)
{
    cJSON *r = cJSON_CreateObject();
    *why = EM_INVALID_INPUT;
    for (size_t i = 0; i < m->ntlvs; i++) {
        const em_tlv *t = &m->tlvs[i];
        const uint8_t *d = t->value;
        size_t len = t->len;
        if (t->kind == 0x8A) {
            if (cJSON_GetObjectItemCaseSensitive(r, "metrics") || len < 2 || len != 2 + (size_t)d[1] * 10)
                goto invalid;
            if (d[1] > 1) {
                *why = EM_UNSUPPORTED_OPERATION; /* "sole-radio policy required" */
                goto invalid;
            }
            cJSON *metrics = cJSON_AddObjectToObject(r, "metrics");
            cJSON_AddNumberToObject(metrics, "interval_seconds", d[0]);
            cJSON *radios = cJSON_AddArrayToObject(metrics, "radios");
            if (d[1]) {
                if (memcmp(d + 2, ruid, 6) || d[8] > 220)
                    goto invalid; /* "foreign radio or reserved RCPI threshold" */
                cJSON *x = cJSON_CreateObject();
                cJSON_AddItemToObject(x, "ruid", hex_of(d + 2));
                cJSON_AddNumberToObject(x, "rcpi_threshold", d[8]);
                cJSON_AddNumberToObject(x, "rcpi_hysteresis_db", d[9]);
                cJSON_AddNumberToObject(x, "utilization_threshold", d[10]);
                cJSON_AddBoolToObject(x, "include_traffic", d[11] & 0x80);
                cJSON_AddBoolToObject(x, "include_link", d[11] & 0x40);
                cJSON_AddBoolToObject(x, "include_wifi6_status", d[11] & 0x20);
                cJSON_AddItemToArray(radios, x);
            }
        } else if (t->kind == 0x89) {
            if (cJSON_GetObjectItemCaseSensitive(r, "steering"))
                goto invalid;
            cJSON *local, *btm;
            size_t a, b;
            if (!addresses(d, len, &local, &a))
                goto invalid;
            if (!addresses(d + a, len - a, &btm, &b)) {
                cJSON_Delete(local);
                goto invalid;
            }
            const uint8_t *rest = d + a + b;
            size_t n = len - a - b;
            if (!n || n != 1 + 9 * (size_t)rest[0] || rest[0] > 1) {
                cJSON_Delete(local);
                cJSON_Delete(btm);
                if (n && n == 1 + 9 * (size_t)rest[0])
                    *why = EM_UNSUPPORTED_OPERATION;
                goto invalid;
            }
            cJSON *steering = cJSON_AddObjectToObject(r, "steering");
            cJSON_AddItemToObject(steering, "local_disallowed", local);
            cJSON_AddItemToObject(steering, "btm_disallowed", btm);
            cJSON *radios = cJSON_AddArrayToObject(steering, "radios");
            if (rest[0]) {
                if (memcmp(rest + 1, ruid, 6) || rest[7] > 2 || rest[9] > 220)
                    goto invalid; /* "foreign radio or reserved steering field" */
                cJSON *x = cJSON_CreateObject();
                cJSON_AddItemToObject(x, "ruid", hex_of(rest + 1));
                cJSON_AddNumberToObject(x, "policy", rest[7]);
                cJSON_AddNumberToObject(x, "utilization_threshold", rest[8]);
                cJSON_AddNumberToObject(x, "rcpi_threshold", rest[9]);
                cJSON_AddItemToArray(radios, x);
            }
        } else if (t->kind == 0xDB) {
            cJSON *mscs, *scs;
            size_t a, b = 0;
            if (!addresses(d, len, &mscs, &a))
                goto invalid;
            if (!addresses(d + a, len - a, &scs, &b)) {
                cJSON_Delete(mscs);
                goto invalid;
            }
            if (len - a - b != 20) {
                cJSON_Delete(mscs);
                cJSON_Delete(scs);
                goto invalid; /* "malformed QoS management policy reserved field" */
            }
            cJSON *qos = cJSON_GetObjectItemCaseSensitive(r, "qos");
            if (!qos)
                qos = cJSON_AddArrayToObject(r, "qos");
            cJSON *x = cJSON_CreateObject();
            cJSON_AddItemToObject(x, "mscs_disallowed", mscs);
            cJSON_AddItemToObject(x, "scs_disallowed", scs);
            cJSON_AddItemToArray(qos, x);
        } else {
            cJSON *ignored = cJSON_GetObjectItemCaseSensitive(r, "not_applied");
            if (!ignored)
                ignored = cJSON_AddArrayToObject(r, "not_applied");
            cJSON *x = cJSON_CreateObject();
            cJSON_AddNumberToObject(x, "kind", t->kind);
            cJSON_AddNumberToObject(x, "length", (double)len);
            cJSON_AddItemToArray(ignored, x);
        }
    }
    *why = EM_OK;
    return r;
invalid:
    cJSON_Delete(r);
    return NULL;
}

/* -- the coordinator ------------------------------------------------------------------------ */

static void put(cJSON *o, const char *k, cJSON *v);
static double num(const cJSON *o, const char *k);
static const cJSON *metrics_of(const cJSON *policy);

/* The kept record at the session's start (spec §3.8): the policy and its receipts stay;
 * the schedule starts now and its accounting is this session's, never written. */
static void started(em_reporting *r, double now)
{
    if (!r->value)
        return;
    double interval = num(metrics_of(cJSON_GetObjectItemCaseSensitive(r->value, "policy")), "interval_seconds");
    put(r->value, "boot_id", cJSON_CreateString(r->store->boot_id));
    put(r->value, "next_due", interval ? cJSON_CreateNumber(now + interval) : cJSON_CreateNull());
    put(r->value, "periods_due_without_report", cJSON_CreateNumber(0));
    put(r->value, "last_unfulfilled_due", cJSON_CreateNull());
    put(r->value, "schedule_rebases", cJSON_CreateNumber(num(r->value, "schedule_rebases") + 1));
    cJSON_DeleteItemFromObjectCaseSensitive(r->value, "latest_report_attempt");
    cJSON_DeleteItemFromObjectCaseSensitive(r->value, "reports_transmitted");
}

void em_reporting_start(em_reporting *r, em_policy_store *store, const uint8_t controller[6],
                        const uint8_t local_al[6], const uint8_t ruid[6], double now)
{
    cJSON_Delete(r->value);
    memset(r, 0, sizeof(*r));
    r->store = store;
    memcpy(r->controller, controller, 6);
    memcpy(r->local_al, local_al, 6);
    memcpy(r->ruid, ruid, 6);
    r->value = store ? store_read(store) : NULL;
    started(r, now);
}

cJSON *em_policy_store_read(em_policy_store *s) { return store_read(s); }

bool em_policy_store_save(em_policy_store *s, const cJSON *value) { return store_save(s, value); }

void em_reporting_close(em_reporting *r)
{
    r->closed = true;
    r->nrecent = 0;
}

static cJSON *identity(const em_reporting *r)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddItemToObject(o, "controller", hex_of(r->controller));
    cJSON_AddItemToObject(o, "local_al", hex_of(r->local_al));
    cJSON_AddItemToObject(o, "ruid", hex_of(r->ruid));
    return o;
}

static bool same_identity(const em_reporting *r, const cJSON *value)
{
    cJSON *mine = identity(r);
    bool same = cJSON_Compare(mine, cJSON_GetObjectItemCaseSensitive(value, "identity"), true);
    cJSON_Delete(mine);
    return same;
}

static void put(cJSON *o, const char *k, cJSON *v)
{
    if (cJSON_GetObjectItemCaseSensitive(o, k))
        cJSON_ReplaceItemInObjectCaseSensitive(o, k, v);
    else
        cJSON_AddItemToObject(o, k, v);
}

/* the session's record, in memory only (ticks) */
static void keep(em_reporting *r, const cJSON *value)
{
    if (value != r->value) {
        cJSON_Delete(r->value);
        r->value = cJSON_Duplicate(value, true);
    }
}

/* a received policy: written, then kept (spec §3.8: written only then) */
static bool persist(em_reporting *r, cJSON *value)
{
    if (!store_save(r->store, value))
        return false;
    keep(r, value);
    return true;
}

static double num(const cJSON *o, const char *k)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, k);
    return cJSON_IsNumber(v) ? v->valuedouble : 0;
}

static const cJSON *metrics_of(const cJSON *policy) { return cJSON_GetObjectItemCaseSensitive(policy, "metrics"); }

/* the AP Metrics Response for the periodic report, or false (the reference's NOT_READY) */
static bool send_metrics(const em_reporting *r, uint16_t mid, const cJSON *policy, const uint8_t (*bssids)[6],
                         size_t nbssids, bool admitted, const em_metric_source *source, em_frames *out)
{
    if (!admitted || !source || !source->stats)
        return false; /* "the agent is not admitted or its state is unavailable" */
    em_tlv_vec tlvs = {0};
    if (!em_pod_metric_tlvs(source, policy, bssids, nbssids, &tlvs)) {
        em_tlv_vec_free(&tlvs);
        return false;
    }
    bool ok = send(r->controller, r->local_al, 0x800C, mid, tlvs.tlvs, tlvs.count, out);
    em_tlv_vec_free(&tlvs);
    return ok;
}

void em_reporting_tick(em_reporting *r, double now, bool admitted, const em_metric_source *source,
                       em_next_mid next_mid, void *mid_ctx, em_frames *out)
{
    size_t kept = 0;
    for (size_t i = 0; i < r->nrecent; i++)
        if (r->recent[i].until > now)
            r->recent[kept++] = r->recent[i];
    r->nrecent = kept;
    if (r->closed || !r->value || !source || !source->radio)
        return;
    if (!same_identity(r, r->value))
        return; /* an old intent cannot become another pod's or controller's policy */
    cJSON *value = cJSON_Duplicate(r->value, true);
    const cJSON *policy = cJSON_GetObjectItemCaseSensitive(value, "policy");
    double interval = num(metrics_of(policy), "interval_seconds");
    const cJSON *due_item = cJSON_GetObjectItemCaseSensitive(value, "next_due");
    if (!cJSON_IsNumber(due_item) || now < due_item->valuedouble || interval <= 0) {
        cJSON_Delete(value);
        return;
    }
    double due = due_item->valuedouble;
    double periods = floor((now - due) / interval) + 1;
    cJSON *prior_unfulfilled = cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(value, "last_unfulfilled_due"), true);
    double reserved = due + (periods - 1) * interval;
    put(value, "periods_due_without_report", cJSON_CreateNumber(num(value, "periods_due_without_report") + periods));
    put(value, "next_due", cJSON_CreateNumber(due + periods * interval));
    put(value, "last_unfulfilled_due", cJSON_CreateNumber(reserved));
    cJSON *attempt = cJSON_CreateObject();
    cJSON_AddNumberToObject(attempt, "due", reserved);
    cJSON_AddStringToObject(attempt, "status", "reserved_outcome_unknown");
    put(value, "latest_report_attempt", attempt);
    /* accounted before I/O: a failed send never repeats a period, no burst follows */
    keep(r, value);
    uint16_t mid = next_mid(mid_ctx);
    if (send_metrics(r, mid, policy, NULL, 0, admitted, source, out)) {
        record(&r->reporter_counts, "periodic_ap_metric_report_transmitted");
        put(value, "periods_due_without_report", cJSON_CreateNumber(num(value, "periods_due_without_report") - 1));
        put(value, "reports_transmitted", cJSON_CreateNumber(num(value, "reports_transmitted") + 1));
        put(value, "last_unfulfilled_due", periods > 1 ? cJSON_CreateNumber(due + (periods - 2) * interval)
                                                       : cJSON_Duplicate(prior_unfulfilled, true));
        attempt = cJSON_CreateObject();
        cJSON_AddNumberToObject(attempt, "due", reserved);
        cJSON_AddStringToObject(attempt, "status", "transmitted_controller_receipt_unverified");
        put(value, "latest_report_attempt", attempt);
        keep(r, value);
        record(&r->counts, "periodic_metric_report_transmitted");
    } else {
        attempt = cJSON_CreateObject();
        cJSON_AddNumberToObject(attempt, "due", reserved);
        cJSON_AddStringToObject(attempt, "status", "unavailable_or_send_incomplete");
        cJSON_AddStringToObject(attempt, "reason", "NOT_READY");
        put(value, "latest_report_attempt", attempt);
        keep(r, value);
        record(&r->counts, "metric_reporting_due_without_qualified_source");
    }
    cJSON_Delete(prior_unfulfilled);
    cJSON_Delete(value);
}

const char *em_reporting_policy(em_reporting *r, const em_message *m, double now, bool admitted,
                                const em_metric_source *source, em_next_mid next_mid, void *mid_ctx,
                                em_frames *out, em_reason *error)
{
    em_reporting_tick(r, now, admitted, source, next_mid, mid_ctx, out);
    *error = EM_OK;
    if (r->closed || !source || !source->radio) {
        *error = EM_NOT_READY; /* "reporting policy source unavailable" */
        return NULL;
    }
    if (memcmp(m->source, r->controller, 6) || memcmp(m->destination, r->local_al, 6) || m->relay) {
        *error = EM_INVALID_INPUT; /* "policy from an unbound controller or envelope" */
        return NULL;
    }
    cJSON *raw = cJSON_CreateArray();
    for (size_t i = 0; i < m->ntlvs; i++) {
        cJSON *x = cJSON_CreateObject();
        char *hex = em_hex(m->tlvs[i].value, m->tlvs[i].len);
        cJSON_AddNumberToObject(x, "kind", m->tlvs[i].kind);
        cJSON_AddStringToObject(x, "value", hex);
        free(hex);
        cJSON_AddItemToArray(raw, x);
    }
    char *encoded = em_json_dumps(raw, EM_JSON_DEFAULT, false), digest[65];
    if (!encoded || strlen(encoded) > 8192) {
        free(encoded);
        cJSON_Delete(raw);
        *error = EM_INVALID_INPUT; /* "policy request exceeds budget" */
        return NULL;
    }
    em_sha256_hex(encoded, strlen(encoded), digest);
    free(encoded);
    em_reason why;
    cJSON *update = decode_policy(m, r->ruid, &why);
    if (!update) {
        cJSON_Delete(raw);
        *error = why;
        return NULL;
    }
    int prior = -1;
    for (size_t i = 0; i < r->nrecent; i++)
        if (r->recent[i].mid == m->mid)
            prior = (int)i;
    if (prior >= 0 && strcmp(r->recent[prior].digest, digest)) {
        cJSON_Delete(update);
        cJSON_Delete(raw);
        *error = EM_INVALID_INPUT; /* "conflicting policy request MID" */
        return NULL;
    }
    if (prior < 0) {
        if (r->nrecent >= 64) {
            cJSON_Delete(update);
            cJSON_Delete(raw);
            *error = EM_NOT_READY; /* "policy request budget exhausted" */
            return NULL;
        }
        cJSON *value = r->value ? cJSON_Duplicate(r->value, true) : NULL;
        if (value && !same_identity(r, value)) {
            /* a record kept for another controller, agent or radio (a pod whose radio
             * changed) is never applied; the policy received now replaces it whole */
            cJSON_Delete(value);
            value = NULL;
            (void)record(&r->counts, "stored_policy_superseded");
        }
        if (!value) {
            value = cJSON_CreateObject();
            cJSON_AddItemToObject(value, "identity", identity(r));
            cJSON_AddItemToObject(value, "policy", cJSON_CreateObject());
            cJSON_AddNumberToObject(value, "receipt_count", 0);
            cJSON_AddNullToObject(value, "next_due");
            cJSON_AddNumberToObject(value, "periods_due_without_report", 0);
            cJSON_AddNullToObject(value, "last_unfulfilled_due");
            cJSON_AddStringToObject(value, "boot_id", r->store->boot_id);
            cJSON_AddNumberToObject(value, "schedule_rebases", 0);
        }
        cJSON *old = cJSON_GetObjectItemCaseSensitive(value, "policy");
        cJSON *policy = cJSON_Duplicate(old, true), *u;
        cJSON_ArrayForEach(u, update) put(policy, u->string, cJSON_Duplicate(u, true));
        cJSON *due = cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(value, "next_due"), true);
        const cJSON *new_metrics = metrics_of(policy), *old_metrics = metrics_of(old);
        bool changed = new_metrics ? !old_metrics || !cJSON_Compare(new_metrics, old_metrics, true) : old_metrics != NULL;
        if (changed) { /* identical re-deliveries never postpone the obligations */
            double interval = num(new_metrics, "interval_seconds");
            cJSON_Delete(due);
            due = interval ? cJSON_CreateNumber(now + interval) : cJSON_CreateNull();
        }
        put(value, "policy", policy);
        put(value, "latest_mid", cJSON_CreateNumber(m->mid));
        put(value, "latest_request", cJSON_Duplicate(raw, true));
        put(value, "receipt_count", cJSON_CreateNumber(num(value, "receipt_count") + 1));
        put(value, "next_due", due ? due : cJSON_CreateNull());
        bool saved = persist(r, value);
        cJSON_Delete(value);
        if (!saved) {
            cJSON_Delete(update);
            cJSON_Delete(raw);
            *error = EM_NOT_READY; /* "reporting policy persistence failed" */
            return NULL;
        }
        r->recent[r->nrecent].mid = m->mid;
        EM_FORMAT_FIXED(r->recent[r->nrecent].digest, 65, "%s", digest);
        r->recent[r->nrecent++].until = now + 5;
    }
    cJSON_Delete(update);
    cJSON_Delete(raw);
    send(r->controller, r->local_al, 0x8000, m->mid, NULL, 0, out);
    return record(&r->counts, "policy_receipt_ack_sent");
}

const char *em_reporting_query(em_reporting *r, const em_message *m, bool admitted, const em_metric_source *source,
                               em_frames *out, em_reason *error)
{
    *error = EM_OK;
    const em_tlv *query = NULL;
    size_t nquery = 0;
    for (size_t i = 0; i < m->ntlvs; i++)
        if (m->tlvs[i].kind == 0x93) {
            query = &m->tlvs[i];
            nquery++;
        }
    if (m->relay || nquery != 1 || query->len < 1 || query->len != 1 + 6 * (size_t)query->value[0]) {
        *error = EM_INVALID_INPUT; /* "one AP Metric Query TLV required" */
        return NULL;
    }
    size_t n = query->value[0];
    uint8_t(*bssids)[6] = em_malloc(n ? n * 6 : 6); /* an empty list queries no BSS (not every one) */
    for (size_t i = 0; i < n; i++)
        memcpy(bssids[i], query->value + 1 + 6 * i, 6);
    const cJSON *policy = r->value ? cJSON_GetObjectItemCaseSensitive(r->value, "policy") : NULL;
    cJSON *empty = policy ? NULL : cJSON_CreateObject();
    bool sent = send_metrics(r, m->mid, policy ? policy : empty, (const uint8_t(*)[6])bssids, n, admitted, source, out);
    cJSON_Delete(empty);
    free(bssids);
    return record(&r->reporter_counts, sent ? "ap_metric_query_answered" : "ap_measurements_unavailable");
}

cJSON *em_reporting_status(const em_reporting *r)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddItemToObject(o, "counts", counts_json(&r->counts));
    cJSON_AddItemToObject(o, "received_policy", r->value ? cJSON_Duplicate(r->value, true) : cJSON_CreateNull());
    cJSON_AddFalseToObject(o, "policy_application_proven");
    cJSON_AddFalseToObject(o, "required_reporting_proven");
    cJSON_AddStringToObject(o, "reporting_gap", "qualified_measurements_and_fulfilled_reporting_pending");
    return o;
}

cJSON *em_reporting_metrics_status(const em_reporting *r, const em_metric_source *source)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddItemToObject(o, "counts", counts_json(&r->reporter_counts));
    if (source && source->stats) {
        char *h = em_hex(source->esp_be, 3);
        cJSON_AddStringToObject(o, "esp_be", h);
        free(h);
        cJSON_AddStringToObject(o, "esp_source", "profile");
    } else {
        cJSON_AddFalseToObject(o, "measurement_available");
    }
    return o;
}

/* -- the pod's AP metrics (pod_metrics.PodMetricReporter.tlvs) ------------------------------ */

static const em_station_stats *station_of(const em_pod_stats *s, const uint8_t mac[6])
{
    char *text = em_mac_str(mac);
    const em_station_stats *found = NULL;
    for (size_t i = 0; i < s->nstations && !found; i++)
        if (!strcasecmp(s->stations[i].mac, text))
            found = &s->stations[i];
    free(text);
    return found;
}

static void u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

bool em_pod_metric_tlvs(const em_metric_source *source, const cJSON *policy, const uint8_t (*bssids)[6],
                        size_t nbssids, em_tlv_vec *out)
{
    const em_pod_stats *stats = source->stats;
    const em_radio_view *radio = source->radio;
    double now = source->wall();
    if (!stats || !radio || !stats->nsurveys)
        return false;
    const em_survey_stats *survey = &stats->surveys[0]; /* the first channel surveyed */
    if (now - survey->measured_at > source->freshness)
        return false; /* "no fresh channel survey from the pod" */
    double scaled = rint((double)survey->busy_percent * 255.0 / 100.0); /* Python's round: ties to even */
    uint8_t util = scaled < 0 ? 0 : scaled > 255 ? 255 : (uint8_t)scaled;
    /* the radio's reporting policy: include traffic and link metrics */
    bool traffic = false, link = false;
    char *ruid = em_hex(radio->ruid, 6);
    const cJSON *entry;
    cJSON_ArrayForEach(entry, cJSON_GetObjectItemCaseSensitive(metrics_of(policy), "radios"))
    {
        const char *id = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(entry, "ruid"));
        if (id && !strcmp(id, ruid)) {
            traffic = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(entry, "include_traffic"));
            link = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(entry, "include_link"));
            break;
        }
    }
    free(ruid);
    static const int COUNTERS[7] = {0, 1, 2, 3, 6, 7, 4}; /* tx/rx bytes, frames, errors, tx retries */
    for (size_t i = 0; i < radio->nbss; i++) {
        const em_bss_view *bss = &radio->bss[i];
        bool wanted = !bssids;
        for (size_t k = 0; k < nbssids && !wanted; k++)
            wanted = !memcmp(bssids[k], bss->bssid, 6);
        if (!wanted)
            continue;
        uint8_t v[16];
        memcpy(v, bss->bssid, 6);
        v[6] = util;
        v[7] = (uint8_t)(bss->nstations >> 8);
        v[8] = (uint8_t)bss->nstations;
        v[9] = 0x80; /* BE only */
        memcpy(v + 10, source->esp_be, 3);
        push(out, 0x94, v, 13);
        for (size_t k = 0; k < bss->nstations; k++) {
            const em_station_stats *st = station_of(stats, bss->stations[k]);
            if (!st || now - st->measured_at > source->freshness)
                continue;
            if (traffic) {
                bool complete = true;
                for (int c = 0; c < 7; c++)
                    complete = complete && st->known[COUNTERS[c]];
                if (complete) {
                    uint8_t t[34];
                    memcpy(t, bss->stations[k], 6);
                    for (int c = 0; c < 7; c++)
                        u32(t + 6 + 4 * c, (uint32_t)(st->counters[COUNTERS[c]] & 0xFFFFFFFFu));
                    push(out, 0xA2, t, 34);
                }
            }
            if (link && st->has_snr) {
                uint8_t l[26];
                double age = now - st->measured_at;
                memcpy(l, bss->stations[k], 6);
                l[6] = 1;
                memcpy(l + 7, bss->bssid, 6);
                u32(l + 13, (uint32_t)(long)(age > 0 ? age * 1000 : 0));
                u32(l + 17, (uint32_t)(st->has_tx_rate ? (long)st->tx_rate : 0)); /* the AP's rate to the station */
                u32(l + 21, (uint32_t)(st->has_rx_rate ? (long)st->rx_rate : 0));
                int rssi = (int)st->snr + NOISE_FLOOR_DBM, rcpi = 2 * (rssi + 110);
                l[25] = (uint8_t)(rcpi < 0 ? 0 : rcpi > 220 ? 220 : rcpi);
                push(out, 0x96, l, 26);
            }
        }
    }
    return out->count > 0; /* "no queried BSS of the pod" */
}
