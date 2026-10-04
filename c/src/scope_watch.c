/* SPDX-License-Identifier: Apache-2.0 */
/* The probe watch (emosa.opensync.probe_watch, emosa.agent.probe_watch). */
#include "scope_watch.h"

#include <regex.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "canon.h"
#include "ovs.h"
#include "scope.h"
#include "southbound.h"

#define MODE "opensync-6.6-probe-watch"
#define PROVENANCE "owm:Band_Steering_Clients(cs_params emosa=watch)"
#define SOURCE "probe-watch"
#define TTL 600
#define MIN_INTERVAL 10
#define DEADLINE 30
#define MAX_WATCHED 32

static const char *str(const cJSON *o, const char *k)
{
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, k));
}

static const char *group_column(const char *band)
{
    if (!band)
        return NULL;
    if (!strcmp(band, "2.4G"))
        return "if_name_2g";
    if (!strcmp(band, "5G") || !strcmp(band, "5GL") || !strcmp(band, "5GU"))
        return "if_name_5g";
    return NULL;
}

static bool mac_ok(const char *m)
{
    regex_t re;
    if (!m || regcomp(&re, "^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", REG_EXTENDED | REG_NOSUB))
        return false;
    bool ok = regexec(&re, m, 0, NULL, 0) == 0;
    regfree(&re);
    return ok;
}

/* WatchIntent.target: {"watched": "a,b,..."} (stations sorted) */
static cJSON *target(void *ctx, const cJSON *intent, em_reason *why)
{
    (void)ctx;
    *why = EM_INVALID_INPUT;
    const cJSON *stations = cJSON_GetObjectItemCaseSensitive(intent, "stations"), *m;
    if (!str(intent, "if_name") || !*str(intent, "if_name") || !group_column(str(intent, "band")) ||
        cJSON_GetArraySize(stations) > MAX_WATCHED)
        return NULL;
    char text[MAX_WATCHED * 18 + 1] = "", previous[18] = "";
    cJSON_ArrayForEach(m, stations)
    {
        if (!mac_ok(cJSON_GetStringValue(m)) || (previous[0] && strcmp(m->valuestring, previous) <= 0))
            return NULL; /* distinct lower-case MACs, sorted */
        EM_FORMAT_FIXED(previous, sizeof(previous), "%s", m->valuestring); /* a checked MAC */
        if (text[0])
            strcat(text, ",");
        strcat(text, m->valuestring);
    }
    *why = EM_OK;
    cJSON *t = cJSON_CreateObject();
    cJSON_AddStringToObject(t, "watched", text);
    return t;
}

static bool bound(em_watch_scope *w, const char **node)
{
    const cJSON *tables = em_ovsdb_tables(w->ovs);
    return em_ovsdb_ready(w->ovs) && em_bound_node(tables, w->serial, node) && em_start_instance(tables, w->instance);
}

static int by_text(const void *a, const void *b) { return strcmp(*(const char *const *)a, *(const char *const *)b); }

/* WatchBackend.rows: (watch rows mac -> UUID, MACs with any other client row) */
static void rows(const cJSON *tables, cJSON *watch, cJSON *blocked)
{
    const cJSON *row;
    cJSON_ArrayForEach(row, em_table(tables, "Band_Steering_Clients"))
    {
        const char *mac = ovs_str(row, "mac");
        if (!mac)
            continue;
        if (em_is_watch_row(row) && !cJSON_GetObjectItemCaseSensitive(watch, mac))
            cJSON_AddStringToObject(watch, mac, row->string);
        else if (!cJSON_GetObjectItemCaseSensitive(blocked, mac))
            cJSON_AddTrueToObject(blocked, mac);
    }
}

static cJSON *sorted_keys(const cJSON *object)
{
    size_t n = (size_t)cJSON_GetArraySize(object), i = 0;
    const char **keys = em_calloc(n, sizeof(*keys));
    const cJSON *m;
    cJSON_ArrayForEach(m, object) keys[i++] = m->string;
    em_sort(keys, n, sizeof(*keys), by_text);
    cJSON *a = cJSON_CreateArray();
    for (i = 0; i < n; i++)
        cJSON_AddItemToArray(a, cJSON_CreateString(keys[i]));
    free(keys);
    return a;
}

/* The strings of array, comma-separated. A list that does not fit ends where it stops
 * fitting: it is then longer than any wanted list (32 MACs), so it never equals one. */
static void joined(const cJSON *array, char *out, size_t n)
{
    out[0] = 0;
    const cJSON *m;
    size_t off = 0;
    cJSON_ArrayForEach(m, array)
    {
        if (!cJSON_IsString(m))
            continue;
        if (!em_format(out + off, n - off, "%s%s", off ? "," : "", m->valuestring)) {
            out[off] = 0;
            return;
        }
        off += strlen(out + off);
    }
}

static bool snapshot(void *ctx, em_snapshot *out)
{
    em_watch_scope *w = ctx;
    memset(out, 0, sizeof(*out));
    const char *node;
    if (!bound(w, &node)) {
        if (!w->has_last)
            return false;
        out->config = cJSON_Duplicate(w->last.config, true);
        out->observed = cJSON_Duplicate(w->last.observed, true);
        cJSON_ReplaceItemInObjectCaseSensitive(out->observed, "fresh", cJSON_CreateFalse());
        out->generation = w->last.generation;
        memcpy(out->schema_fingerprint, w->last.schema_fingerprint, 65);
        return true;
    }
    const cJSON *tables = em_ovsdb_tables(w->ovs);
    cJSON *watch = cJSON_CreateObject(), *blocked = cJSON_CreateObject(), *associated = cJSON_CreateObject();
    rows(tables, watch, blocked);
    const cJSON *row;
    cJSON_ArrayForEach(row, em_table(tables, "Wifi_Associated_Clients"))
    {
        const char *mac = ovs_str(row, "mac");
        if (mac && *mac && !cJSON_GetObjectItemCaseSensitive(associated, mac))
            cJSON_AddTrueToObject(associated, mac);
    }
    char text[4096];
    cJSON *watched = sorted_keys(watch);
    joined(watched, text, sizeof(text));
    cJSON_Delete(watched);
    out->config = cJSON_CreateObject();
    cJSON_AddStringToObject(out->config, "watched", text);
    cJSON_AddItemToObject(out->config, "blocked", sorted_keys(blocked));
    cJSON_AddItemToObject(out->config, "associated", sorted_keys(associated));
    cJSON_Delete(watch);
    cJSON_Delete(blocked);
    cJSON_Delete(associated);
    cJSON *values = cJSON_CreateObject();
    cJSON_AddStringToObject(values, "watched", text);
    out->ready = true;
    out->generation = em_ovsdb_generation(w->ovs);
    EM_FORMAT_FIXED(out->schema_fingerprint, 65, "%s", em_ovsdb_schema_fingerprint(w->ovs)); /* SHA-256 hex */
    out->observed = em_observation(w->pod_id, "probe-watch", values, MODE, out->generation, true, PROVENANCE,
                                   em_ovsdb_revision(w->ovs));
    em_snapshot_clear(&w->last);
    w->last.config = cJSON_Duplicate(out->config, true);
    w->last.observed = cJSON_Duplicate(out->observed, true);
    w->last.generation = out->generation;
    memcpy(w->last.schema_fingerprint, out->schema_fingerprint, 65);
    w->has_last = true;
    return true;
}

static cJSON *plan(void *ctx, const cJSON *intent, em_reason *why)
{
    em_watch_scope *w = ctx;
    cJSON *t = target(ctx, intent, why);
    if (!t)
        return NULL;
    cJSON_Delete(t);
    if (strcmp(str(intent, "pod_id"), w->pod_id)) {
        *why = EM_UNSUPPORTED_OPERATION;
        return NULL;
    }
    const char *node;
    if (!bound(w, &node)) {
        *why = EM_NOT_READY;
        return NULL;
    }
    const cJSON *tables = em_ovsdb_tables(w->ovs), *row;
    const char *if_name = str(intent, "if_name"), *column = group_column(str(intent, "band"));
    bool ap = false;
    cJSON_ArrayForEach(row, em_table(tables, "Wifi_VIF_State"))
    {
        const char *name = ovs_str(row, "if_name"), *mode = ovs_str(row, "mode");
        ap = ap || (name && !strcmp(name, if_name) && mode && !strcmp(mode, "ap"));
    }
    if (!ap) {
        *why = EM_NOT_READY; /* "the watch VIF is not an AP VIF of the pod" */
        return NULL;
    }
    size_t ngroups = 0;
    const char *group = NULL;
    cJSON_ArrayForEach(row, em_table(tables, "Band_Steering_Config"))
    {
        const char *a = ovs_str(row, "if_name_2g"), *b = ovs_str(row, "if_name_5g");
        if ((a && !strcmp(a, if_name)) || (b && !strcmp(b, if_name))) {
            ngroups++;
            group = row->string;
        }
    }
    if (ngroups > 1) {
        *why = EM_OWNERSHIP_CONFLICT; /* "ambiguous steering group" */
        return NULL;
    }
    cJSON *watch = cJSON_CreateObject(), *blocked = cJSON_CreateObject();
    rows(tables, watch, blocked);
    const cJSON *m;
    cJSON_ArrayForEach(m, cJSON_GetObjectItemCaseSensitive(intent, "stations"))
    {
        if (cJSON_GetObjectItemCaseSensitive(blocked, m->valuestring)) {
            cJSON_Delete(watch);
            cJSON_Delete(blocked);
            *why = EM_OWNERSHIP_CONFLICT; /* "a watched station has another client row" */
            return NULL;
        }
    }
    cJSON *add = cJSON_CreateArray(), *remove = cJSON_CreateObject();
    cJSON_ArrayForEach(m, cJSON_GetObjectItemCaseSensitive(intent, "stations"))
    if (!cJSON_GetObjectItemCaseSensitive(watch, m->valuestring))
        cJSON_AddItemToArray(add, cJSON_CreateString(m->valuestring));
    cJSON *keys = sorted_keys(watch);
    cJSON_ArrayForEach(m, keys)
    {
        bool wanted = false;
        const cJSON *s;
        cJSON_ArrayForEach(s, cJSON_GetObjectItemCaseSensitive(intent, "stations")) wanted = wanted || !strcmp(s->valuestring, m->valuestring);
        if (!wanted)
            cJSON_AddStringToObject(remove, m->valuestring, str(watch, m->valuestring));
    }
    cJSON_Delete(keys);
    cJSON_Delete(watch);
    cJSON_Delete(blocked);
    cJSON *p = cJSON_CreateObject();
    cJSON_AddStringToObject(p, "mapping", MODE);
    cJSON_AddStringToObject(p, "action", "probe-watch");
    cJSON_AddStringToObject(p, "group_column", column);
    cJSON_AddItemToObject(p, "group", group ? cJSON_CreateString(group) : cJSON_CreateNull());
    cJSON_AddItemToObject(p, "add", add);
    cJSON_AddItemToObject(p, "remove", remove);
    cJSON_AddStringToObject(p, "instance", w->instance);
    const char *fields[] = {"Band_Steering_Config", "Band_Steering_Clients"};
    cJSON_AddItemToObject(p, "fields", em_ovs_strings(fields, 2));
    cJSON_AddStringToObject(p, "guard", "pod serial; no client row for an added station; removed rows as seen");
    return p;
}

static cJSON *wait_op(const char *table, cJSON *where, const char *const *columns, size_t ncol, cJSON *rows)
{
    cJSON *o = em_ovs_op("wait", table, where);
    cJSON_AddItemToObject(o, "columns", em_ovs_strings(columns, ncol));
    cJSON_AddStringToObject(o, "until", "==");
    cJSON_AddItemToObject(o, "rows", rows);
    cJSON_AddNumberToObject(o, "timeout", 0);
    return o;
}

static cJSON *one_row(const char *k1, const char *v1, const char *k2, const char *v2)
{
    cJSON *rows = cJSON_CreateArray(), *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, k1, v1);
    if (k2)
        cJSON_AddStringToObject(r, k2, v2);
    cJSON_AddItemToArray(rows, r);
    return rows;
}

/* the complete client row owm reads, steering off, marked as EMOSA's watch row */
static cJSON *watch_row(const char *mac)
{
    static const char *const zero[] = {"hwm", "lwm", "bottom_lwm", "kick_reason", "max_rejects",
        "rejects_tmout_secs", "backoff_secs", "pref_5g_pre_assoc_block_timeout_msecs",
        "pref_6g_pre_assoc_block_timeout_msecs", "kick_debounce_period", "sc_kick_reason",
        "sc_kick_debounce_period", "sticky_kick_debounce_period", "sticky_kick_reason",
        "steering_success_cnt", "steering_fail_cnt", "steering_kick_cnt", "sticky_kick_cnt"};
    static const char *const off[] = {"kick_upon_idle", "pre_assoc_auth_block", "send_rrm_after_assoc",
        "neighbor_list_filter_by_beacon_report"};
    cJSON *r = cJSON_CreateObject();
    for (size_t i = 0; i < sizeof(zero) / sizeof(*zero); i++)
        cJSON_AddNumberToObject(r, zero[i], 0);
    cJSON_AddStringToObject(r, "kick_type", "none");
    cJSON_AddStringToObject(r, "reject_detection", "none");
    cJSON_AddStringToObject(r, "pref_5g", "never");
    cJSON_AddStringToObject(r, "pref_bs_allowed", "never");
    cJSON_AddStringToObject(r, "pref_6g", "never");
    for (size_t i = 0; i < sizeof(off) / sizeof(*off); i++)
        cJSON_AddFalseToObject(r, off[i]);
    cJSON_AddStringToObject(r, "cs_mode", "off");
    cJSON *params = cJSON_CreateArray(), *pairs = cJSON_CreateArray(), *pair = cJSON_CreateArray();
    cJSON_AddItemToArray(pair, cJSON_CreateString("emosa"));
    cJSON_AddItemToArray(pair, cJSON_CreateString("watch"));
    cJSON_AddItemToArray(pairs, pair);
    cJSON_AddItemToArray(params, cJSON_CreateString("map"));
    cJSON_AddItemToArray(params, pairs);
    cJSON_AddItemToObject(r, "cs_params", params);
    cJSON_AddStringToObject(r, "mac", mac);
    return r;
}

static void submit(void *ctx, const cJSON *intent, const cJSON *attempt, em_submit_out *out)
{
    em_watch_scope *w = ctx;
    memset(out, 0, sizeof(*out));
    em_reason why;
    cJSON *p = plan(ctx, intent, &why);
    if (!p) {
        strcpy(out->status, "unknown"); /* the plan raised at submission */
        out->reason = EM_OUTCOME_UNKNOWN;
        return;
    }
    const char *node;
    int generation = (int)cJSON_GetNumberValue(cJSON_GetObjectItemCaseSensitive(attempt, "session_generation"));
    if (!bound(w, &node) || em_ovsdb_generation(w->ovs) != generation) {
        cJSON_Delete(p);
        strcpy(out->status, "rejected");
        out->reason = EM_NOT_READY;
        return;
    }
    const char *column = str(p, "group_column"), *if_name = str(intent, "if_name");
    cJSON *ops = cJSON_CreateArray();
    int counts[3 + 2 * 2 * MAX_WATCHED + 8];
    size_t n = 0;
    const char *serial_col[] = {"serial_number"};
    cJSON_AddItemToArray(ops, wait_op("AWLAN_Node", em_ovs_where_uuid(node), serial_col, 1,
                                      one_row("serial_number", w->serial, NULL, NULL)));
    counts[n++] = -1;
    const char *uuid_col[] = {"_uuid"};
    if (str(p, "group")) {
        const char *cols[] = {column};
        cJSON_AddItemToArray(ops, wait_op("Band_Steering_Config", em_ovs_where_uuid(str(p, "group")), cols, 1,
                                          one_row(column, if_name, NULL, NULL)));
        counts[n++] = -1;
    } else {
        cJSON_AddItemToArray(ops, wait_op("Band_Steering_Config", em_ovs_where_eq("if_name_2g", if_name), uuid_col, 1,
                                          cJSON_CreateArray()));
        cJSON_AddItemToArray(ops, wait_op("Band_Steering_Config", em_ovs_where_eq("if_name_5g", if_name), uuid_col, 1,
                                          cJSON_CreateArray()));
        cJSON *ins = em_ovs_op("insert", "Band_Steering_Config", NULL), *row = cJSON_CreateObject();
        cJSON_AddStringToObject(row, column, if_name);
        cJSON_AddItemToObject(ins, "row", row);
        cJSON_AddItemToArray(ops, ins);
        counts[n++] = -1;
        counts[n++] = -1;
        counts[n++] = -1;
    }
    const cJSON *m;
    cJSON_ArrayForEach(m, cJSON_GetObjectItemCaseSensitive(p, "remove")) /* sorted by MAC */
    {
        const char *cols[] = {"mac", "cs_mode"};
        cJSON_AddItemToArray(ops, wait_op("Band_Steering_Clients", em_ovs_where_uuid(m->valuestring), cols, 2,
                                          one_row("mac", m->string, "cs_mode", "off")));
        cJSON_AddItemToArray(ops, em_ovs_op("delete", "Band_Steering_Clients", em_ovs_where_uuid(m->valuestring)));
        counts[n++] = -1;
        counts[n++] = 1;
    }
    cJSON_ArrayForEach(m, cJSON_GetObjectItemCaseSensitive(p, "add"))
    {
        cJSON_AddItemToArray(ops, wait_op("Band_Steering_Clients", em_ovs_where_eq("mac", m->valuestring), uuid_col, 1,
                                          cJSON_CreateArray()));
        cJSON *ins = em_ovs_op("insert", "Band_Steering_Clients", NULL);
        cJSON_AddItemToObject(ins, "row", watch_row(m->valuestring));
        cJSON_AddItemToArray(ops, ins);
        counts[n++] = -1;
        counts[n++] = -1;
    }
    cJSON *results = w->transact(w->transact_ctx, ops);
    cJSON_Delete(ops);
    if (!results) {
        cJSON_Delete(p);
        strcpy(out->status, "unknown");
        out->reason = EM_OUTCOME_UNKNOWN;
        out->evidence = cJSON_CreateObject();
        cJSON_AddStringToObject(out->evidence, "attribution", "unknown");
        return;
    }
    em_reason checked = em_check_results(results, counts, n);
    cJSON_Delete(results);
    if (checked != EM_OK) {
        cJSON_Delete(p);
        EM_FORMAT_FIXED(out->status, sizeof(out->status), "%s", checked == EM_OUTCOME_UNKNOWN ? "unknown" : "rejected");
        out->reason = checked;
        return;
    }
    strcpy(out->status, "committed");
    out->evidence = cJSON_CreateObject();
    cJSON_AddStringToObject(out->evidence, "attribution", "reply");
    cJSON_AddTrueToObject(out->evidence, "transaction_validated");
    cJSON_AddStringToObject(out->evidence, "transaction_id", str(attempt, "transaction_id"));
    cJSON_AddItemToObject(out->evidence, "added", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(p, "add"), true));
    cJSON_AddItemToObject(out->evidence, "removed", sorted_keys(cJSON_GetObjectItemCaseSensitive(p, "remove")));
    cJSON_Delete(p);
}

/* -- ProbeWatch ---------------------------------------------------------------------------- */

em_reason em_watch_open(em_watch_scope *w, const char *state_dir, const em_journal_schemas *schemas,
                        const em_vault *vault)
{
    char dir[600];
    EM_FORMAT_FIXED(dir, sizeof(dir), "%s/probe-watch", state_dir); /* EM_STATE_DIR_MAX */
    em_reason why;
    w->journal = em_journal_open(dir, schemas, &why);
    if (!w->journal)
        return why;
    em_engine_init(&w->engine, w->journal, vault, w->pod_id, (em_backend){MODE, target, snapshot, plan, submit}, w,
                   w->monotonic);
    em_engine_recover(&w->engine);
    return EM_OK;
}

void em_watch_close(em_watch_scope *w)
{
    em_engine_free(&w->engine);
    em_journal_close(w->journal);
    em_snapshot_clear(&w->last);
}

void em_watch_ask(em_watch_scope *w, const uint8_t (*stations)[6], size_t n)
{
    double now = w->monotonic();
    for (size_t k = 0; k < n; k++) {
        char mac[18];
        EM_FORMAT_FIXED(mac, sizeof(mac), "%02x:%02x:%02x:%02x:%02x:%02x", stations[k][0], stations[k][1], stations[k][2],
                 stations[k][3], stations[k][4], stations[k][5]);
        size_t i = 0;
        while (i < w->nasked && strcmp(w->asked[i].mac, mac))
            i++;
        if (i == w->nasked) {
            if (w->nasked == EM_WATCH_ASKED) { /* the one asked about longest ago goes */
                size_t oldest = 0;
                for (size_t j = 1; j < w->nasked; j++)
                    if (w->asked[j].at < w->asked[oldest].at)
                        oldest = j;
                i = oldest;
            } else {
                w->nasked++;
            }
            EM_FORMAT_FIXED(w->asked[i].mac, sizeof(w->asked[i].mac), "%s", mac);
        }
        w->asked[i].at = now;
    }
}

static void wait_for(em_watch_scope *w, const char *why)
{
    w->has_waiting = why != NULL;
    (void)em_copy(w->waiting, sizeof(w->waiting), why ? why : ""); /* a message */
}

static int by_time(const void *a, const void *b)
{
    const double x = *(const double *)a, y = *(const double *)b;
    return x < y ? -1 : x > y;
}

/* ProbeWatch.wanted: the recently asked, not excluded, at most 32 (the latest), sorted */
static cJSON *wanted(em_watch_scope *w, const cJSON *config)
{
    double now = w->monotonic();
    const cJSON *blocked = cJSON_GetObjectItemCaseSensitive(config, "blocked"),
                *associated = cJSON_GetObjectItemCaseSensitive(config, "associated"), *m;
    struct {
        double at;
        char mac[18];
    } recent[EM_WATCH_ASKED];
    size_t n = 0;
    for (size_t i = 0; i < w->nasked; i++) {
        if (now - w->asked[i].at > TTL)
            continue;
        bool excluded = false;
        cJSON_ArrayForEach(m, blocked) excluded = excluded || !strcmp(m->valuestring, w->asked[i].mac);
        cJSON_ArrayForEach(m, associated) excluded = excluded || !strcmp(m->valuestring, w->asked[i].mac);
        if (excluded)
            continue;
        recent[n].at = w->asked[i].at;
        memcpy(recent[n].mac, w->asked[i].mac, 18);
        n++;
    }
    em_sort(recent, n, sizeof(recent[0]), by_time); /* (time, mac) order: equal times are rare */
    size_t first = n > MAX_WATCHED ? n - MAX_WATCHED : 0;
    cJSON *keep = cJSON_CreateObject();
    for (size_t i = first; i < n; i++)
        cJSON_AddTrueToObject(keep, recent[i].mac);
    cJSON *list = sorted_keys(keep);
    cJSON_Delete(keep);
    return list;
}

static void settle(em_watch_scope *w, cJSON *op, const em_snapshot *snap, bool have)
{
    const char *state = em_state_of(op);
    if (strcmp(state, "CONFIG_COMMITTED") && strcmp(state, "INDETERMINATE"))
        return;
    em_reason why;
    cJSON *t = target(w, cJSON_GetObjectItemCaseSensitive(op, "intent"), &why);
    if (t && have && snap->ready && em_snapshot_satisfies(snap, t) && em_matches(snap->config, t)) {
        cJSON *evidence = em_engine_evidence(snap, false, w->instance);
        if (!strcmp(state, "INDETERMINATE"))
            cJSON_AddStringToObject(evidence, "attribution", "current_condition_only");
        em_transition(op, "OBSERVED_APPLIED", evidence);
        cJSON_ReplaceItemInObjectCaseSensitive(op, "application_evidence", evidence);
        em_set_reason(op, NULL);
        em_engine_save(&w->engine, op, NULL);
    } else if (em_engine_expired(&w->engine, op)) {
        cJSON_ReplaceItemInObjectCaseSensitive(op, "original_outcome", cJSON_CreateString(state));
        cJSON_ReplaceItemInObjectCaseSensitive(op, "deadline_elapsed", cJSON_CreateTrue());
        em_transition(op, "TIMED_OUT", NULL);
        em_set_reason(op, "APPLY_TIMEOUT");
        em_engine_save(&w->engine, op, NULL);
    }
    cJSON_Delete(t);
}

void em_watch_tick(em_watch_scope *w)
{
    em_snapshot snap = {0};
    bool have = snapshot(w, &snap);
    if (!have)
        return;
    cJSON *op = em_journal_latest(w->journal);
    if (op) {
        settle(w, op, &snap, have);
        cJSON_Delete(op);
        op = em_journal_latest(w->journal);
    }
    cJSON *owned = NULL, *list = NULL;
    if (!snap.ready) {
        wait_for(w, "pod not bound");
    } else if ((owned = em_journal_ownership(w->journal, w->pod_id))) {
        wait_for(w, "another manager changed the configuration: admit the pod anew");
    } else if (op && em_state_active(em_state_of(op))) {
        wait_for(w, "write in progress");
    } else {
        list = wanted(w, snap.config);
        char text[4096];
        joined(list, text, sizeof(text));
        double now = w->monotonic();
        if (!strcmp(text, str(snap.config, "watched"))) {
            wait_for(w, NULL);
        } else if (w->has_last_request && now - w->last_request < MIN_INTERVAL) {
            wait_for(w, "next write after the minimum interval");
        } else {
            w->last_request = now;
            w->has_last_request = true;
            cJSON *intent = cJSON_CreateObject();
            cJSON_AddStringToObject(intent, "pod_id", w->pod_id);
            cJSON_AddStringToObject(intent, "if_name", w->if_name);
            cJSON_AddStringToObject(intent, "band", w->band);
            cJSON_AddItemToObject(intent, "stations", cJSON_Duplicate(list, true));
            char digest[65], key[128];
            em_sha256_hex(text, strlen(text), digest);
            EM_FORMAT_FIXED(key, sizeof(key), "%s:%zu:%.12s", w->instance, em_journal_count(w->journal), digest);
            em_reason why;
            cJSON *req = em_engine_request(&w->engine, intent, SOURCE, key, w->run_id, DEADLINE, "semantic", NULL, &why);
            if (req && !strcmp(em_state_of(req), "REQUESTED"))
                cJSON_Delete(em_engine_execute(&w->engine,
                                               cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(req, "operation_id"))));
            cJSON_Delete(req);
            cJSON_Delete(intent);
            wait_for(w, NULL);
        }
    }
    cJSON_Delete(list);
    cJSON_Delete(owned);
    cJSON_Delete(op);
    em_snapshot_clear(&snap);
}

cJSON *em_watch_status(em_watch_scope *w)
{
    cJSON *o = cJSON_CreateObject(), *watched = cJSON_AddArrayToObject(o, "watched");
    const char *text = w->has_last ? str(w->last.config, "watched") : NULL;
    if (text && *text) {
        char copy[4096], *save = NULL;
        /* a journal record: a list that does not fit is not shown in part */
        if (!em_copy(copy, sizeof(copy), text))
            copy[0] = 0;
        for (char *t = strtok_r(copy, ",", &save); t; t = strtok_r(NULL, ",", &save))
            cJSON_AddItemToArray(watched, cJSON_CreateString(t));
    }
    cJSON_AddNumberToObject(o, "asked", (double)w->nasked);
    cJSON_AddItemToObject(o, "waiting", w->has_waiting ? cJSON_CreateString(w->waiting) : cJSON_CreateNull());
    cJSON *op = em_journal_latest(w->journal);
    if (op) {
        cJSON *x = cJSON_AddObjectToObject(o, "operation");
        cJSON_AddItemToObject(x, "operation_id", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "operation_id"), 1));
        cJSON_AddItemToObject(x, "state", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "state"), 1));
        cJSON_AddItemToObject(x, "reason", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "reason"), 1));
    } else {
        cJSON_AddNullToObject(o, "operation");
    }
    cJSON_Delete(op);
    return o;
}

em_backend em_watch_backend(void) { return (em_backend){MODE, target, snapshot, plan, submit}; }
