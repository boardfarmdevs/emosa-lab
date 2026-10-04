/* SPDX-License-Identifier: Apache-2.0 */
/* The durable operation journal (emosa.store.Store). */
#include "journal.h"

#include <errno.h>
#include <limits.h>
#include <fcntl.h>
#include <sqlite3.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#include "canon.h"
#include "log.h"

#define MAX_OPERATIONS 10000
#define MAX_EVENTS 10000
/* spec §6, the journal's retention: besides every active operation and each pod's latest
 * one in a state reconciliation follows, the most recent operations */
#define RETAINED_RECENT 16

struct em_journal {
    char directory[512];
    char process_id[37];
    int lock;
    sqlite3 *db;
    em_journal_schemas schemas;
    cJSON *ops; /* every operation, oldest first, parsed once (NULL: not read yet) */
};

static const char *const SENSITIVE[] = {"psk", "password", "wpa_psks", "security", "private_key",
                                        "credential_fingerprint"};

cJSON *em_redact(const cJSON *value)
{
    if (cJSON_IsObject(value)) {
        cJSON *out = cJSON_CreateObject();
        const cJSON *m;
        cJSON_ArrayForEach(m, value)
        {
            bool secret = false;
            for (size_t i = 0; i < sizeof(SENSITIVE) / sizeof(*SENSITIVE); i++)
                secret = secret || !strcmp(m->string, SENSITIVE[i]);
            cJSON_AddItemToObject(out, m->string, secret ? cJSON_CreateString("[REDACTED]") : em_redact(m));
        }
        return out;
    }
    if (cJSON_IsArray(value)) {
        cJSON *out = cJSON_CreateArray();
        const cJSON *m;
        cJSON_ArrayForEach(m, value) cJSON_AddItemToArray(out, em_redact(m));
        return out;
    }
    return cJSON_Duplicate(value, true);
}

static bool exec(em_journal *j, const char *sql)
{
    return sqlite3_exec(j->db, sql, NULL, NULL, NULL) == SQLITE_OK;
}

em_journal *em_journal_open(const char *directory, const em_journal_schemas *schemas, em_reason *why)
{
    *why = EM_NOT_READY;
    if (mkdir(directory, 0700) && errno != EEXIST)
        return NULL;
    em_journal *j = em_calloc(1, sizeof(*j));
    if (!em_copy(j->directory, sizeof(j->directory), directory)) { /* 511 bytes at most */
        free(j);
        *why = EM_INVALID_INPUT;
        return NULL;
    }
    em_uuid4(j->process_id);
    if (schemas)
        j->schemas = *schemas;
    char path[600];
    EM_FORMAT_FIXED(path, sizeof(path), "%s/writer.lock", directory);
    j->lock = open(path, O_RDWR | O_CREAT | O_APPEND, 0644);
    if (j->lock < 0 || flock(j->lock, LOCK_EX | LOCK_NB)) {
        *why = EM_BUSY; /* "state directory already has an active writer" */
        if (j->lock >= 0)
            close(j->lock);
        free(j);
        return NULL;
    }
    EM_FORMAT_FIXED(path, sizeof(path), "%s/journal.db", directory);
    if (sqlite3_open(path, &j->db) != SQLITE_OK) {
        em_journal_close(j);
        return NULL;
    }
    sqlite3_busy_timeout(j->db, 2000);
    chmod(path, 0600);
    if (!exec(j, "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;"
                 "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                 "INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1');"
                 "CREATE TABLE IF NOT EXISTS operations ("
                 " id TEXT PRIMARY KEY, source TEXT NOT NULL, pod TEXT NOT NULL,"
                 " idem TEXT NOT NULL, fingerprint TEXT NOT NULL, run TEXT NOT NULL,"
                 " record TEXT NOT NULL, UNIQUE(source,pod,idem));"
                 "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, record TEXT NOT NULL);"
                 "CREATE TABLE IF NOT EXISTS events ("
                 " run TEXT NOT NULL, seq INTEGER NOT NULL, record TEXT NOT NULL,"
                 " PRIMARY KEY(run,seq));"
                 "CREATE TABLE IF NOT EXISTS ownership (pod TEXT PRIMARY KEY, record TEXT NOT NULL);"
                 "CREATE TABLE IF NOT EXISTS wsc_receipts (operation_id TEXT PRIMARY KEY, record TEXT NOT NULL);")) {
        em_journal_close(j);
        return NULL;
    }
    sqlite3_stmt *st;
    bool version = false;
    if (sqlite3_prepare_v2(j->db, "SELECT value FROM metadata WHERE key='schema_version'", -1, &st, NULL) ==
        SQLITE_OK) {
        const char *value = sqlite3_step(st) == SQLITE_ROW ? (const char *)sqlite3_column_text(st, 0) : NULL;
        version = value && !strcmp(value, "1"); /* NULL in a journal another writer made: no version */
        sqlite3_finalize(st);
    }
    if (!version) {
        *why = EM_SCHEMA_MISMATCH;
        em_journal_close(j);
        return NULL;
    }
    *why = EM_OK;
    return j;
}

void em_journal_close(em_journal *j)
{
    if (!j)
        return;
    if (j->db)
        sqlite3_close(j->db);
    if (j->lock >= 0)
        close(j->lock);
    cJSON_Delete(j->ops);
    free(j);
}

const char *em_journal_directory(const em_journal *j) { return j->directory; }
const char *em_journal_process_id(const em_journal *j) { return j->process_id; }

/* the JSON records of a one-column query, as an array */
static cJSON *records(em_journal *j, const char *sql, const char *const *args, size_t nargs)
{
    sqlite3_stmt *st;
    cJSON *out = cJSON_CreateArray();
    if (sqlite3_prepare_v2(j->db, sql, -1, &st, NULL) != SQLITE_OK)
        return out;
    bool bound = true; /* a parameter not bound is no query (EXP12-C) */
    for (size_t i = 0; i < nargs; i++)
        bound = bound && sqlite3_bind_text(st, (int)i + 1, args[i], -1, SQLITE_TRANSIENT) == SQLITE_OK;
    while (bound && sqlite3_step(st) == SQLITE_ROW) {
        cJSON *r = cJSON_Parse((const char *)sqlite3_column_text(st, 0));
        if (r)
            cJSON_AddItemToArray(out, r);
    }
    sqlite3_finalize(st);
    return out;
}

static cJSON *first(cJSON *array)
{
    cJSON *r = cJSON_GetArraySize(array) ? cJSON_DetachItemFromArray(array, 0) : NULL;
    cJSON_Delete(array);
    return r;
}

/* Records read back are checked as they were when written: one altered on disk, or a
 * stranger's, is skipped, so no caller meets a record without its required fields. */
static bool valid(em_schema *schema, const cJSON *value);

static cJSON *valid_operations(em_journal *j, cJSON *list)
{
    cJSON *op = list ? list->child : NULL;
    while (op) {
        cJSON *next = op->next;
        if (!valid(j->schemas.operation, op)) {
            em_log(EM_LOG_WARNING, "emosa.store", "journal %s: an operation record not valid, skipped",
                   j->directory);
            cJSON_Delete(cJSON_DetachItemViaPointer(list, op));
        }
        op = next;
    }
    return list;
}

const cJSON *em_journal_operations_view(em_journal *j)
{
    if (!j->ops)
        j->ops = valid_operations(j, records(j, "SELECT record FROM operations ORDER BY rowid", NULL, 0));
    return j->ops;
}

const cJSON *em_journal_latest_view(em_journal *j)
{
    const cJSON *ops = em_journal_operations_view(j);
    int n = cJSON_GetArraySize(ops);
    return n ? cJSON_GetArrayItem(ops, n - 1) : NULL;
}

/* the journal's own copy of an operation (callers get duplicates) */
static cJSON *cached(em_journal *j, const char *id)
{
    (void)em_journal_operations_view(j); /* read once */
    cJSON *op;
    cJSON_ArrayForEach(op, j->ops)
    {
        const char *oid = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(op, "operation_id"));
        if (oid && id && !strcmp(oid, id))
            return op;
    }
    return NULL;
}

cJSON *em_journal_get(em_journal *j, const char *id)
{
    const cJSON *op = cached(j, id);
    return op ? cJSON_Duplicate(op, true) : NULL;
}

cJSON *em_journal_lookup(em_journal *j, const char *source, const char *pod, const char *key)
{
    const char *a[] = {source, pod, key};
    return first(valid_operations(j, records(j, "SELECT record FROM operations WHERE source=? AND pod=? AND idem=?", a, 3)));
}

cJSON *em_journal_operations(em_journal *j, const char *run_id)
{
    if (!run_id)
        return cJSON_Duplicate(em_journal_operations_view(j), true);
    cJSON *out = cJSON_CreateArray();
    const cJSON *op;
    cJSON_ArrayForEach(op, em_journal_operations_view(j))
    {
        const char *run = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(op, "run_id"));
        if (run && !strcmp(run, run_id))
            cJSON_AddItemToArray(out, cJSON_Duplicate(op, true));
    }
    return out;
}

cJSON *em_journal_latest(em_journal *j)
{
    const cJSON *op = em_journal_latest_view(j);
    return op ? cJSON_Duplicate(op, true) : NULL;
}

size_t em_journal_count(em_journal *j) { return (size_t)cJSON_GetArraySize(em_journal_operations_view(j)); }

cJSON *em_journal_wsc_receipt(em_journal *j, const char *id)
{
    const char *a[] = {id};
    return first(records(j, "SELECT record FROM wsc_receipts WHERE operation_id=?", a, 1));
}

cJSON *em_journal_events(em_journal *j, const char *run_id, long after, int limit)
{
    char a1[24], a2[24];
    EM_FORMAT_FIXED(a1, sizeof(a1), "%ld", after);
    EM_FORMAT_FIXED(a2, sizeof(a2), "%d", limit < 1 ? 1 : limit > 500 ? 500 : limit);
    const char *a[] = {run_id, a1, a2};
    return records(j, "SELECT record FROM events WHERE run=? AND seq>CAST(?2 AS INTEGER) ORDER BY seq LIMIT CAST(?3 AS INTEGER)", a, 3);
}

cJSON *em_journal_ownership(em_journal *j, const char *pod)
{
    const char *a[] = {pod};
    return first(records(j, "SELECT record FROM ownership WHERE pod=?", a, 1));
}

static bool run_sql(em_journal *j, const char *sql, const char *const *args, size_t nargs)
{
    sqlite3_stmt *st;
    if (sqlite3_prepare_v2(j->db, sql, -1, &st, NULL) != SQLITE_OK)
        return false;
    bool ok = true;
    for (size_t i = 0; i < nargs; i++)
        ok = ok && sqlite3_bind_text(st, (int)i + 1, args[i], -1, SQLITE_TRANSIENT) == SQLITE_OK;
    ok = ok && sqlite3_step(st) == SQLITE_DONE;
    sqlite3_finalize(st);
    return ok;
}

static bool valid(em_schema *schema, const cJSON *value)
{
    char where[256];
    return !schema || em_schema_valid(schema, value, where, sizeof(where));
}

static const char *str(const cJSON *o, const char *k)
{
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, k));
}

/* one event (inside the caller's transaction) */
static bool event(em_journal *j, const char *run, const char *op_id, const char *pod, const char *phase,
                  const cJSON *payload, const char *timestamp)
{
    sqlite3_stmt *st;
    long seq = 0;
    if (sqlite3_prepare_v2(j->db, "SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run=?", -1, &st, NULL) !=
        SQLITE_OK)
        return false;
    if (sqlite3_bind_text(st, 1, run, -1, SQLITE_TRANSIENT) == SQLITE_OK && sqlite3_step(st) == SQLITE_ROW) {
        sqlite3_int64 next = sqlite3_column_int64(st, 0);
        seq = next >= 1 && next <= LONG_MAX ? (long)next : 0; /* one a long holds (INT31-C) */
    }
    sqlite3_finalize(st);
    if (!seq)
        return false;
    const cJSON *reason = cJSON_GetObjectItemCaseSensitive(payload, "reason");
    const char *reason_text = cJSON_IsString(reason)   ? reason->valuestring
                              : cJSON_IsObject(reason) ? (str(reason, "code") ? str(reason, "code") : "NOT_READY")
                                                       : NULL;
    char id[37], now[32];
    em_uuid4(id);
    em_utc_now(now);
    cJSON *r = cJSON_CreateObject();
    cJSON_AddNumberToObject(r, "schema_version", 1);
    cJSON_AddStringToObject(r, "event_id", id);
    cJSON_AddNumberToObject(r, "sequence", (double)seq);
    cJSON_AddStringToObject(r, "timestamp", timestamp ? timestamp : now);
    cJSON_AddStringToObject(r, "process_clock_id", j->process_id);
    cJSON_AddStringToObject(r, "run_id", run);
    cJSON_AddItemToObject(r, "operation_id", op_id ? cJSON_CreateString(op_id) : cJSON_CreateNull());
    cJSON_AddItemToObject(r, "pod_id", pod ? cJSON_CreateString(pod) : cJSON_CreateNull());
    cJSON_AddStringToObject(r, "phase", phase);
    cJSON_AddItemToObject(r, "reason", reason_text ? cJSON_CreateString(reason_text) : cJSON_CreateNull());
    cJSON_AddItemToObject(r, "payload", payload ? em_redact(payload) : cJSON_CreateObject());
    bool ok = valid(j->schemas.event, r);
    if (ok) {
        char *text = cJSON_PrintUnformatted(r), s1[24], s2[24];
        EM_FORMAT_FIXED(s1, sizeof(s1), "%ld", seq);
        EM_FORMAT_FIXED(s2, sizeof(s2), "%ld", seq - MAX_EVENTS);
        const char *a[] = {run, s1, text};
        const char *b[] = {run, s2};
        ok = run_sql(j, "INSERT INTO events VALUES (?,CAST(?2 AS INTEGER),?3)", a, 3) &&
             run_sql(j, "DELETE FROM events WHERE run=? AND seq<=CAST(?2 AS INTEGER)", b, 2);
        free(text);
    }
    cJSON_Delete(r);
    return ok;
}

static cJSON *state_of(const cJSON *op) { return cJSON_GetObjectItemCaseSensitive(op, "state"); }

static bool named(const char *s, const char *const *set, size_t n)
{
    for (size_t i = 0; s && i < n; i++)
        if (!strcmp(s, set[i]))
            return true;
    return false;
}

/* Spec §6: with `added` appended to the kept operations, delete (in the caller's
 * transaction) every operation that is not active, not its pod's latest in a state
 * reconciliation follows and not among the RETAINED_RECENT most recent, with its WSC
 * receipt. The deleted operations' IDs go into `dropped` (owned by the caller). */
static bool prune(em_journal *j, const cJSON *added, cJSON *dropped)
{
    static const char *const active[] = {"REQUESTED", "VALIDATED", "SUBMITTED", "CONFIG_COMMITTED", "INDETERMINATE"};
    static const char *const reconciled[] = {"SUBMITTED", "CONFIG_COMMITTED", "OBSERVED_APPLIED",
                                             "INDETERMINATE", "TIMED_OUT", "OWNERSHIP_CONFLICT"};
    const cJSON *kept = em_journal_operations_view(j);
    int n = cJSON_GetArraySize(kept) + 1;
    if (n <= RETAINED_RECENT)
        return true;
    const cJSON **ops = em_calloc((size_t)n, sizeof(*ops));
    const char **seen = em_calloc((size_t)n, sizeof(*seen)); /* pods whose latest is found */
    size_t nseen = 0, i = 0;
    const cJSON *op;
    cJSON_ArrayForEach(op, kept) ops[i++] = op;
    ops[i] = added;
    bool ok = true;
    for (int k = n - 1; ok && k >= 0; k--) { /* newest first: the first of a pod is its latest */
        const char *state = cJSON_GetStringValue(state_of(ops[k]));
        const char *pod = str(cJSON_GetObjectItemCaseSensitive(ops[k], "intent"), "pod_id");
        bool keep = k >= n - RETAINED_RECENT || named(state, active, 5);
        if (named(state, reconciled, 6) && pod) {
            bool latest = true;
            for (size_t s = 0; s < nseen && latest; s++)
                latest = strcmp(seen[s], pod) != 0;
            if (latest) {
                seen[nseen++] = pod;
                keep = true;
            }
        }
        if (keep)
            continue;
        const char *id = str(ops[k], "operation_id");
        const char *a[] = {id};
        ok = id && run_sql(j, "DELETE FROM operations WHERE id=?", a, 1) &&
             run_sql(j, "DELETE FROM wsc_receipts WHERE operation_id=?", a, 1);
        if (ok)
            cJSON_AddItemToArray(dropped, cJSON_CreateString(id));
    }
    free(ops);
    free(seen);
    return ok;
}

em_reason em_journal_add(em_journal *j, const cJSON *op, const cJSON *receipt)
{
    if (!valid(j->schemas.operation, op) || (receipt && !valid(j->schemas.receipt, receipt)))
        return EM_INVALID_INPUT;
    const cJSON *intent = cJSON_GetObjectItemCaseSensitive(op, "intent");
    const char *interface = str(op, "initiating_interface");
    if (receipt) {
        char source[64];
        /* a record: a receipt without a controller that fits names no source */
        const char *al = str(receipt, "controller_al");
        if (!al || !em_format(source, sizeof(source), "wsc-component:%s", al))
            return EM_INVALID_INPUT;
        if (strcmp(interface, "wsc-component") || strcmp(str(receipt, "process_id"), j->process_id) ||
            strcmp(str(receipt, "exchange_id"), str(op, "idempotency_key")) ||
            strcmp(str(receipt, "pod_id"), str(intent, "pod_id")) ||
            strcmp(str(receipt, "radio_id"), str(intent, "radio_id")) ||
            strcmp(str(receipt, "bss_id"), str(intent, "bss_id")) || strcmp(str(op, "request_source"), source))
            return EM_INVALID_INPUT; /* "WSC receipt and operation binding differ" */
    } else if (!strcmp(interface, "wsc-component")) {
        return EM_INVALID_INPUT;
    }
    /* (the kept operations are read here, before the transaction: prune() adds this one) */
    if (em_journal_count(j) >= MAX_OPERATIONS)
        return EM_BUSY;
    char *text = cJSON_PrintUnformatted(op);
    const char *a[] = {str(op, "operation_id"), str(op, "request_source"), str(intent, "pod_id"),
                       str(op, "idempotency_key"), str(op, "intent_fingerprint"), str(op, "run_id"), text};
    bool ok = exec(j, "BEGIN IMMEDIATE") && run_sql(j, "INSERT INTO operations VALUES (?,?,?,?,?,?,?)", a, 7);
    cJSON *empty = cJSON_CreateObject();
    ok = ok && event(j, str(op, "run_id"), str(op, "operation_id"), str(intent, "pod_id"),
                     state_of(op)->valuestring, empty, str(op, "created_at"));
    cJSON_Delete(empty);
    if (ok && receipt) {
        char *rt = cJSON_PrintUnformatted(receipt);
        const char *b[] = {str(op, "operation_id"), rt};
        ok = run_sql(j, "INSERT INTO wsc_receipts VALUES (?,?)", b, 2);
        free(rt);
    }
    cJSON *dropped = cJSON_CreateArray();
    ok = ok && prune(j, op, dropped) && exec(j, "COMMIT");
    if (!ok) {
        exec(j, "ROLLBACK");
    } else if (j->ops) {
        cJSON_AddItemToArray(j->ops, cJSON_Duplicate(op, true));
        const cJSON *id;
        cJSON_ArrayForEach(id, dropped)
        {
            cJSON *gone = cached(j, id->valuestring);
            if (gone)
                cJSON_Delete(cJSON_DetachItemViaPointer(j->ops, gone));
        }
    }
    cJSON_Delete(dropped);
    free(text);
    return ok ? EM_OK : EM_NOT_READY;
}

em_reason em_journal_save(em_journal *j, const cJSON *op, const cJSON *payload)
{
    if (!valid(j->schemas.operation, op))
        return EM_INVALID_INPUT;
    char *text = cJSON_PrintUnformatted(op);
    const char *a[] = {text, str(op, "operation_id")};
    cJSON *p = cJSON_CreateObject();
    const cJSON *reason = cJSON_GetObjectItemCaseSensitive(op, "reason");
    cJSON_AddItemToObject(p, "reason", reason ? cJSON_Duplicate(reason, true) : cJSON_CreateNull());
    const cJSON *m;
    cJSON_ArrayForEach(m, payload) cJSON_AddItemToObject(p, m->string, cJSON_Duplicate(m, true));
    const cJSON *intent = cJSON_GetObjectItemCaseSensitive(op, "intent");
    bool ok = exec(j, "BEGIN IMMEDIATE") && run_sql(j, "UPDATE operations SET record=? WHERE id=?", a, 2) &&
              event(j, str(op, "run_id"), str(op, "operation_id"), str(intent, "pod_id"),
                    state_of(op)->valuestring, p, str(op, "updated_at")) &&
              exec(j, "COMMIT");
    if (!ok) {
        exec(j, "ROLLBACK");
    } else if (j->ops) {
        cJSON *mine = cached(j, str(op, "operation_id"));
        if (mine) /* the stored record replaces the kept one */
            cJSON_ReplaceItemViaPointer(j->ops, mine, cJSON_Duplicate(op, true));
    }
    cJSON_Delete(p);
    free(text);
    return ok ? EM_OK : EM_NOT_READY;
}

em_reason em_journal_event(em_journal *j, const char *run_id, const char *phase, const cJSON *payload,
                           const char *pod_id)
{
    bool ok = exec(j, "BEGIN IMMEDIATE") && event(j, run_id, NULL, pod_id, phase, payload, NULL) &&
              exec(j, "COMMIT");
    if (!ok)
        exec(j, "ROLLBACK");
    return ok ? EM_OK : EM_NOT_READY;
}

void em_journal_conflict(em_journal *j, const char *pod, const cJSON *evidence)
{
    cJSON *r = em_redact(evidence);
    char *text = cJSON_PrintUnformatted(r);
    const char *a[] = {pod, text};
    run_sql(j, "INSERT OR REPLACE INTO ownership VALUES (?,?)", a, 2);
    free(text);
    cJSON_Delete(r);
}

void em_journal_release(em_journal *j, const char *pod)
{
    const char *a[] = {pod};
    run_sql(j, "DELETE FROM ownership WHERE pod=?", a, 1);
}
