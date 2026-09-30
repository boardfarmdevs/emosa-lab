/* The client steering scope (emosa.opensync.steering, emosa.agent.steering). */
#include "scope_steering.h"

#include <math.h>
#include <regex.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "canon.h"
#include "ovs.h"
#include "scope.h"

#define MODE "opensync-6.6-client-steering"
#define PROVENANCE "owm:Band_Steering_Clients.cs_state"
#define SOURCE "steering-request"
#define APPLY 10  /* owm takes the row within a second or two (seen live) */
#define GENTLE 8  /* < owm's 10 s deauthentication delay after a BTM request */
#define MARGIN 5
#define WAIT 10   /* a controller verifies a steer for about this long */

static const char *str(const cJSON *o, const char *k)
{
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, k));
}

static int num(const cJSON *o, const char *k) { return (int)cJSON_GetNumberValue(cJSON_GetObjectItemCaseSensitive(o, k)); }

static const char *record(em_steering_scope *s, const char *event)
{
    for (size_t i = 0; i < s->counts.n; i++)
        if (!strcmp(s->counts.items[i].name, event)) {
            s->counts.items[i].count++;
            return event;
        }
    if (s->counts.n < 32) {
        EM_FORMAT_FIXED(s->counts.items[s->counts.n].name, sizeof(s->counts.items[0].name), "%.63s", event);
        s->counts.items[s->counts.n++].count = 1;
    }
    return event;
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

/* SteeringIntent.target: {station: "<target>/steering"} */
static cJSON *target(void *ctx, const cJSON *intent, em_reason *why)
{
    (void)ctx;
    const char *station = str(intent, "station"), *source = str(intent, "source_bssid"),
               *to = str(intent, "target_bssid");
    int op_class = num(intent, "op_class"), channel = num(intent, "channel"), window = num(intent, "window");
    *why = EM_INVALID_INPUT;
    if (!mac_ok(station) || !mac_ok(source) || !mac_ok(to) || !strcmp(to, source) || op_class <= 0 ||
        op_class >= 256 || channel <= 0 || channel >= 234 || window < 5 || window > 120)
        return NULL;
    *why = EM_OK;
    char value[40];
    EM_FORMAT_FIXED(value, sizeof(value), "%s/steering", to); /* a checked MAC */
    cJSON *t = cJSON_CreateObject();
    cJSON_AddStringToObject(t, station, value);
    return t;
}

/* SteeringBackend.windows: station -> "<target>/<cs_state>" for every away window */
static cJSON *windows(const cJSON *tables)
{
    cJSON *w = cJSON_CreateObject();
    const cJSON *row;
    cJSON_ArrayForEach(row, em_table(tables, "Band_Steering_Clients"))
    {
        const char *mode = ovs_str(row, "cs_mode"), *kick = ovs_str(row, "sc_kick_type"), *mac = ovs_str(row, "mac");
        if (!mode || strcmp(mode, "away") || !kick || strcmp(kick, "btm_deauth") || !mac)
            continue;
        const char *to = ovs_map_get(row, "sc_btm_params", "bssid"), *state = ovs_str(row, "cs_state");
        char value[64];
        /* the pod's values: a value cut here still starts with its target, followed by '/'
         * only when the target itself fitted, which is all close_window() compares */
        (void)em_format(value, sizeof(value), "%s/%s", to ? to : "None", state && *state ? state : "none");
        if (cJSON_GetObjectItemCaseSensitive(w, mac))
            cJSON_ReplaceItemInObjectCaseSensitive(w, mac, cJSON_CreateString(value));
        else
            cJSON_AddStringToObject(w, mac, value);
    }
    return w;
}

static bool bound(em_steering_scope *s)
{
    const cJSON *tables = em_ovsdb_tables(s->ovs);
    return em_ovsdb_ready(s->ovs) && em_bound_node(tables, s->serial, NULL) && em_start_instance(tables, s->instance);
}

static bool snapshot(void *ctx, em_snapshot *out)
{
    em_steering_scope *s = ctx;
    memset(out, 0, sizeof(*out));
    if (!bound(s)) {
        if (!s->has_last)
            return false;
        out->config = cJSON_Duplicate(s->last.config, true);
        out->observed = cJSON_Duplicate(s->last.observed, true);
        cJSON_ReplaceItemInObjectCaseSensitive(out->observed, "fresh", cJSON_CreateFalse());
        out->generation = s->last.generation;
        memcpy(out->schema_fingerprint, s->last.schema_fingerprint, 65);
        return true;
    }
    out->config = windows(em_ovsdb_tables(s->ovs));
    out->ready = true;
    out->generation = em_ovsdb_generation(s->ovs);
    EM_FORMAT_FIXED(out->schema_fingerprint, 65, "%s", em_ovsdb_schema_fingerprint(s->ovs)); /* SHA-256 hex */
    out->observed = em_observation(s->pod_id, "client-steering", cJSON_Duplicate(out->config, true), MODE,
                                   out->generation, true, PROVENANCE, em_ovsdb_revision(s->ovs));
    em_snapshot_clear(&s->last);
    s->last.config = cJSON_Duplicate(out->config, true);
    s->last.observed = cJSON_Duplicate(out->observed, true);
    s->last.generation = out->generation;
    memcpy(s->last.schema_fingerprint, out->schema_fingerprint, 65);
    s->has_last = true;
    return true;
}

/* SteeringBackend.source: the pod's AP VIF with bssid, its group column, its stations */
static const cJSON *source_vif(const cJSON *tables, const char *bssid, const char **column)
{
    const cJSON *vif;
    cJSON_ArrayForEach(vif, em_table(tables, "Wifi_VIF_State"))
    {
        const char *mac = ovs_str(vif, "mac"), *mode = ovs_str(vif, "mode");
        if (!mac || strcmp(mac, bssid) || !mode || strcmp(mode, "ap") || !ovs_true(vif, "enabled"))
            continue;
        *column = NULL;
        const cJSON *radio;
        cJSON_ArrayForEach(radio, em_table(tables, "Wifi_Radio_State"))
        {
            if (!em_row_has_uuid(radio, "vif_states", vif->string))
                continue;
            const char *band = ovs_str(radio, "freq_band");
            *column = !band                                                                     ? NULL
                      : !strcmp(band, "2.4G")                                                   ? "if_name_2g"
                      : !strcmp(band, "5G") || !strcmp(band, "5GL") || !strcmp(band, "5GU") ? "if_name_5g"
                                                                                                : NULL;
            break;
        }
        return vif;
    }
    return NULL;
}

static bool on_vif(const cJSON *tables, const cJSON *vif, const char *station)
{
    const char *assoc[256];
    size_t n = ovs_uuids(vif, "associated_clients", assoc, 256);
    for (size_t i = 0; i < n; i++) {
        const cJSON *c = cJSON_GetObjectItemCaseSensitive(em_table(tables, "Wifi_Associated_Clients"), assoc[i]);
        const char *mac = c ? ovs_str(c, "mac") : NULL;
        if (mac && !strcmp(mac, station))
            return true;
    }
    return false;
}

static cJSON *plan(void *ctx, const cJSON *intent, em_reason *why)
{
    em_steering_scope *s = ctx;
    cJSON *t = target(ctx, intent, why);
    if (!t)
        return NULL;
    cJSON_Delete(t);
    if (strcmp(str(intent, "pod_id"), s->pod_id)) {
        *why = EM_UNSUPPORTED_OPERATION;
        return NULL;
    }
    if (!bound(s)) {
        *why = EM_NOT_READY;
        return NULL;
    }
    const cJSON *tables = em_ovsdb_tables(s->ovs);
    const char *column = NULL, *station = str(intent, "station"), *to = str(intent, "target_bssid");
    const cJSON *vif = source_vif(tables, str(intent, "source_bssid"), &column);
    if (!vif || !column) {
        *why = EM_UNSUPPORTED_OPERATION; /* the source is not an AP BSS of the pod, or has no group */
        return NULL;
    }
    if (!on_vif(tables, vif, station)) {
        *why = EM_NOT_READY; /* "the station is not associated with the source" */
        return NULL;
    }
    const char *if_name = ovs_str(vif, "if_name");
    cJSON *groups = cJSON_CreateArray(), *neighbors = cJSON_CreateArray(), *watch = cJSON_CreateArray();
    bool conflict = false;
    const cJSON *row;
    cJSON_ArrayForEach(row, em_table(tables, "Band_Steering_Config"))
    {
        const char *a = ovs_str(row, "if_name_2g"), *b = ovs_str(row, "if_name_5g");
        if ((a && !strcmp(a, if_name)) || (b && !strcmp(b, if_name)))
            cJSON_AddItemToArray(groups, cJSON_CreateString(row->string));
    }
    cJSON_ArrayForEach(row, em_table(tables, "Wifi_VIF_Neighbors"))
    {
        const char *bssid = ovs_str(row, "bssid"), *name = ovs_str(row, "if_name");
        if (bssid && !strcmp(bssid, to) && name && !strcmp(name, if_name))
            cJSON_AddItemToArray(neighbors, cJSON_CreateString(row->string));
    }
    cJSON_ArrayForEach(row, em_table(tables, "Band_Steering_Clients"))
    {
        const char *mac = ovs_str(row, "mac");
        if (!mac || strcmp(mac, station))
            continue;
        if (em_is_watch_row(row))
            cJSON_AddItemToArray(watch, cJSON_CreateString(row->string));
        else
            conflict = true;
    }
    if (conflict || cJSON_GetArraySize(groups) > 1 || cJSON_GetArraySize(neighbors) > 1) {
        cJSON_Delete(groups);
        cJSON_Delete(neighbors);
        cJSON_Delete(watch);
        *why = EM_OWNERSHIP_CONFLICT;
        return NULL;
    }
    cJSON *p = cJSON_CreateObject();
    cJSON_AddStringToObject(p, "mapping", MODE);
    cJSON_AddStringToObject(p, "action", "client-steering-window");
    cJSON_AddStringToObject(p, "source_if", if_name);
    cJSON_AddStringToObject(p, "group_column", column);
    cJSON_AddItemToObject(p, "group", cJSON_GetArraySize(groups) ? cJSON_Duplicate(groups->child, 1) : cJSON_CreateNull());
    cJSON_AddItemToObject(p, "neighbor",
                          cJSON_GetArraySize(neighbors) ? cJSON_Duplicate(neighbors->child, 1) : cJSON_CreateNull());
    cJSON_AddItemToObject(p, "watch", watch);
    cJSON_AddStringToObject(p, "instance", s->instance);
    const char *fields[] = {"Band_Steering_Config", "Wifi_VIF_Neighbors", "Band_Steering_Clients"};
    cJSON_AddItemToObject(p, "fields", em_ovs_strings(fields, 3));
    cJSON_AddStringToObject(p, "guard", "pod serial; no steering row for the station; group and neighbor as seen");
    cJSON_Delete(groups);
    cJSON_Delete(neighbors);
    return p;
}

static void intent_struct(const cJSON *intent, em_steering_intent *in)
{
    memset(in, 0, sizeof(*in));
    /* a journal record: MACs that do not fit leave all three empty, which match no row */
    const char *station = str(intent, "station"), *source = str(intent, "source_bssid"), *target = str(intent, "target_bssid");
    if (!station || !source || !target || !em_copy(in->station, sizeof(in->station), station) ||
        !em_copy(in->source_bssid, sizeof(in->source_bssid), source) ||
        !em_copy(in->target_bssid, sizeof(in->target_bssid), target))
        in->station[0] = in->source_bssid[0] = in->target_bssid[0] = 0;
    in->op_class = num(intent, "op_class");
    in->channel = num(intent, "channel");
    in->window = num(intent, "window");
    in->disassoc_imminent = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(intent, "disassoc_imminent"));
}

static em_ovs_session session(em_steering_scope *s)
{
    return (em_ovs_session){em_ovsdb_tables(s->ovs), em_ovsdb_generation(s->ovs), s->transact, s->transact_ctx};
}

static void submit(void *ctx, const cJSON *intent, const cJSON *attempt, em_submit_out *out)
{
    em_steering_scope *s = ctx;
    memset(out, 0, sizeof(*out));
    int generation = num(attempt, "session_generation");
    if (!bound(s) || em_ovsdb_generation(s->ovs) != generation) {
        strcpy(out->status, "rejected");
        out->reason = EM_NOT_READY;
        return;
    }
    em_steering_intent in;
    intent_struct(intent, &in);
    em_ovs_session os = session(s);
    em_submit_result result;
    em_steering_rows created;
    em_reason e = em_steering_open(s->serial, &os, &in, &result, &created);
    if (e != EM_OK) { /* the plan raised at submission */
        strcpy(out->status, "unknown");
        out->reason = EM_OUTCOME_UNKNOWN;
        return;
    }
    EM_FORMAT_FIXED(out->status, sizeof(out->status), "%s", result.status);
    out->reason = result.reason;
    if (strcmp(result.status, "committed"))
        return;
    out->evidence = cJSON_CreateObject();
    cJSON_AddStringToObject(out->evidence, "attribution", "reply");
    cJSON_AddTrueToObject(out->evidence, "transaction_validated");
    cJSON_AddStringToObject(out->evidence, "transaction_id", str(attempt, "transaction_id"));
    cJSON_AddNumberToObject(out->evidence, "session_generation", generation);
    cJSON_AddStringToObject(out->evidence, "action", "client-steering-window");
    cJSON_AddStringToObject(out->evidence, "instance", s->instance);
    cJSON *rows = cJSON_AddObjectToObject(out->evidence, "created");
    if (created.group[0])
        cJSON_AddStringToObject(rows, "group", created.group);
    if (created.neighbor[0])
        cJSON_AddStringToObject(rows, "neighbor", created.neighbor);
    if (created.client[0])
        cJSON_AddStringToObject(rows, "client", created.client);
}

/* -- ClientSteering ---------------------------------------------------------------------- */

static double clock_now(em_steering_scope *s) { return s->monotonic(); }

static bool closed_ops(em_steering_scope *s, const char *id)
{
    long after = 0;
    for (;;) {
        cJSON *events = em_journal_events(s->journal, s->run_id, after, 500), *e;
        if (!cJSON_GetArraySize(events)) {
            cJSON_Delete(events);
            return false;
        }
        bool found = false;
        cJSON_ArrayForEach(e, events)
        {
            const char *op = str(e, "operation_id");
            if (op && !strcmp(op, id) &&
                cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(e, "payload"), "window_closed"))
                found = true;
            after = (long)cJSON_GetNumberValue(cJSON_GetObjectItemCaseSensitive(e, "sequence"));
        }
        cJSON_Delete(events);
        if (found)
            return true;
    }
}

em_reason em_steering_scope_open(em_steering_scope *s, const char *state_dir, const em_journal_schemas *schemas,
                                 const em_vault *vault)
{
    char dir[600];
    EM_FORMAT_FIXED(dir, sizeof(dir), "%s/steering", state_dir); /* EM_STATE_DIR_MAX */
    em_reason why;
    s->journal = em_journal_open(dir, schemas, &why);
    if (!s->journal)
        return why;
    em_engine_init(&s->engine, s->journal, vault, s->pod_id, (em_backend){MODE, target, snapshot, plan, submit}, s,
                   s->monotonic);
    em_engine_recover(&s->engine);
    s->history = cJSON_CreateArray();
    /* windows a previous process left open: the latest eight, closed once the pod is reachable */
    s->leftover = cJSON_CreateArray();
    cJSON *ops = em_journal_operations(s->journal, NULL), *op;
    int n = cJSON_GetArraySize(ops), i = 0;
    cJSON_ArrayForEach(op, ops)
    {
        if (i++ < n - 8)
            continue;
        const char *state = em_state_of(op);
        if ((!strcmp(state, "REQUESTED") || !strcmp(state, "CONFIG_COMMITTED") || !strcmp(state, "OBSERVED_APPLIED") ||
             !strcmp(state, "INDETERMINATE")) &&
            !closed_ops(s, str(op, "operation_id")))
            cJSON_AddItemToArray(s->leftover, cJSON_Duplicate(op, true));
    }
    cJSON_Delete(ops);
    return EM_OK;
}

void em_steering_scope_close(em_steering_scope *s)
{
    em_engine_free(&s->engine);
    em_journal_close(s->journal);
    em_snapshot_clear(&s->last);
    cJSON_Delete(s->intent);
    cJSON_Delete(s->leftover);
    cJSON_Delete(s->history);
}

static void history(em_steering_scope *s, const char *station, const char *to, const char *outcome, const char *op)
{
    cJSON *e = cJSON_CreateObject();
    cJSON_AddStringToObject(e, "station", station);
    cJSON_AddStringToObject(e, "target", to);
    cJSON_AddStringToObject(e, "outcome", outcome);
    cJSON_AddItemToObject(e, "operation_id", op ? cJSON_CreateString(op) : cJSON_CreateNull());
    cJSON_AddItemToArray(s->history, e);
    while (cJSON_GetArraySize(s->history) > 8)
        cJSON_DeleteItemFromArray(s->history, 0);
    (void)fprintf(stderr, "INFO emosa.agent.steering: client steering %s -> %s: %s\n", station, to, outcome);
}

static void mac_text(const uint8_t m[6], char out[18])
{
    EM_FORMAT_FIXED(out, 18, "%02x:%02x:%02x:%02x:%02x:%02x", m[0], m[1], m[2], m[3], m[4], m[5]);
}

static void drop(em_steering_scope *s, const em_steering_queued *q, const char *outcome)
{
    char station[18], to[18];
    mac_text(q->request.stations[0], station);
    mac_text(q->request.targets[0].bssid, to);
    record(s, outcome);
    history(s, station, to, outcome, NULL);
}

/* window_seconds: EasyMesh gives seconds; the pod enforces 15..120 */
static int window_seconds(const em_steering_request *r)
{
    int w = r->window < 15 ? 15 : r->window;
    return w > 120 ? 120 : w;
}

static const char *begin(em_steering_scope *s, const em_steering_request *r, uint16_t mid)
{
    static char reason[96];
    char station[18], source[18], to[18], now[32], key[80];
    mac_text(r->stations[0], station);
    mac_text(r->source_bssid, source);
    mac_text(r->targets[0].bssid, to);
    cJSON *intent = cJSON_CreateObject();
    cJSON_AddStringToObject(intent, "pod_id", s->pod_id);
    cJSON_AddStringToObject(intent, "station", station);
    cJSON_AddStringToObject(intent, "source_bssid", source);
    cJSON_AddStringToObject(intent, "target_bssid", to);
    cJSON_AddNumberToObject(intent, "op_class", r->targets[0].op_class);
    cJSON_AddNumberToObject(intent, "channel", r->targets[0].channel);
    cJSON_AddBoolToObject(intent, "disassoc_imminent", r->disassoc_imminent);
    cJSON_AddNumberToObject(intent, "window", window_seconds(r));
    cJSON_AddNumberToObject(intent, "request_mid", mid);
    em_utc_now(now);
    EM_FORMAT_FIXED(key, sizeof(key), "%u:%s:%s", mid, station, now);
    em_reason why;
    cJSON *op = em_engine_request(&s->engine, intent, SOURCE, key, s->run_id, APPLY, "semantic", NULL, &why);
    if (!op) {
        cJSON_Delete(intent);
        EM_FORMAT_FIXED(reason, sizeof(reason), "invalid_%s", em_reason_name(why));
        return reason;
    }
    if (strcmp(em_state_of(op), "REQUESTED")) {
        const char *r2 = str(op, "reason");
        (void)em_format(reason, sizeof(reason), "%s_%s", em_state_of(op), r2 ? r2 : ""); /* an outcome label */
        for (char *p = reason; *p; p++)
            if (*p >= 'A' && *p <= 'Z')
                *p = (char)(*p + 32);
        size_t n = strlen(reason);
        while (n && reason[n - 1] == '_')
            reason[--n] = 0;
        cJSON_Delete(op);
        cJSON_Delete(intent);
        return reason;
    }
    s->active = true;
    const char *op_id = str(op, "operation_id");
    EM_FORMAT_FIXED(s->op, sizeof(s->op), "%s", op_id ? op_id : ""); /* the engine's UUID */
    EM_FORMAT_FIXED(s->phase, sizeof(s->phase), "requested");
    cJSON_Delete(s->intent);
    s->intent = intent;
    cJSON_Delete(op);
    record(s, "started");
    return NULL;
}

const char *em_steering_start(em_steering_scope *s, const em_steering_request *r, uint16_t mid)
{
    if (!s->active && !s->nqueue)
        return begin(s, r, mid);
    size_t kept = 0;
    for (size_t i = 0; i < s->nqueue; i++)
        if (memcmp(s->queue[i].request.stations[0], r->stations[0], 6))
            s->queue[kept++] = s->queue[i];
    if (kept >= EM_STEERING_QUEUE) {
        return "busy";
    }
    if (kept < s->nqueue)
        record(s, "queued_replaced");
    s->nqueue = kept;
    s->queue[s->nqueue++] = (em_steering_queued){*r, mid, clock_now(s)};
    record(s, "queued");
    return NULL;
}

/* (cs_state or NULL, 1 on the source / 0 not / -1 unknown) */
static const char *observe(em_steering_scope *s, const char *station, const char *source, int *on_source)
{
    *on_source = -1;
    if (!bound(s))
        return NULL;
    const cJSON *tables = em_ovsdb_tables(s->ovs), *row, *found = NULL;
    size_t rows = 0;
    cJSON_ArrayForEach(row, em_table(tables, "Band_Steering_Clients"))
    {
        const char *mac = ovs_str(row, "mac");
        if (mac && !strcmp(mac, station)) {
            rows++;
            found = row;
        }
    }
    const char *column;
    const cJSON *vif = source_vif(tables, source, &column);
    *on_source = vif && on_vif(tables, vif, station);
    return rows == 1 ? ovs_str(found, "cs_state") : NULL;
}

static void dequeue(em_steering_scope *s)
{
    while (!s->active && s->nqueue) {
        em_steering_queued q = s->queue[0];
        memmove(&s->queue[0], &s->queue[1], (s->nqueue - 1) * sizeof(s->queue[0]));
        s->nqueue--;
        if (clock_now(s) - q.at > WAIT) {
            drop(s, &q, "queue_expired");
            continue;
        }
        char station[18], source[18];
        mac_text(q.request.stations[0], station);
        mac_text(q.request.source_bssid, source);
        int on_source;
        observe(s, station, source, &on_source);
        if (on_source == 0) {
            drop(s, &q, "moved_while_queued");
            continue;
        }
        const char *reason = begin(s, &q.request, q.mid);
        if (reason) {
            char outcome[128];
            (void)em_format(outcome, sizeof(outcome), "refused_%s", reason); /* an outcome label */
            drop(s, &q, outcome);
        }
    }
}

static void finish(em_steering_scope *s, const char *outcome)
{
    s->active = false;
    record(s, outcome);
    history(s, str(s->intent, "station"), str(s->intent, "target_bssid"), outcome, s->op);
}

/* _end: the window's operation leaves the active states, with its outcome in the journal */
static void end(em_steering_scope *s, cJSON *op, const char *outcome)
{
    const char *state = em_state_of(op);
    if (!strcmp(state, "REQUESTED")) {
        cJSON *cancelled = em_engine_cancel(&s->engine, str(op, "operation_id"));
        if (cancelled) {
            cJSON *payload = cJSON_CreateObject();
            cJSON_AddStringToObject(payload, "window_closed", outcome);
            em_engine_save(&s->engine, cancelled, payload);
            cJSON_Delete(payload);
            cJSON_Delete(cancelled);
        }
        return;
    }
    if (!strcmp(state, "CONFIG_COMMITTED") || !strcmp(state, "INDETERMINATE")) {
        cJSON_ReplaceItemInObjectCaseSensitive(op, "original_outcome", cJSON_CreateString(state));
        cJSON_ReplaceItemInObjectCaseSensitive(op, "deadline_elapsed", cJSON_CreateTrue());
        em_transition(op, "TIMED_OUT", NULL);
        if (!str(op, "reason"))
            em_set_reason(op, "APPLY_TIMEOUT");
    }
    cJSON *payload = cJSON_CreateObject();
    cJSON_AddStringToObject(payload, "window_closed", outcome);
    em_engine_save(&s->engine, op, payload);
    cJSON_Delete(payload);
}

/* _close: delete the rows the window created (or, outcome unknown, its client row) */
static void close_window(em_steering_scope *s, const cJSON *op, const cJSON *intent)
{
    em_steering_intent in;
    intent_struct(intent, &in);
    em_steering_rows rows = {0};
    const cJSON *created = cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(op, "commit_evidence"), "created");
    if (created) {
        /* UUIDs from a journal record: one that does not fit is not deleted */
        const char *client = str(created, "client"), *neighbor = str(created, "neighbor"), *group = str(created, "group");
        if (!em_copy(rows.client, sizeof(rows.client), client ? client : ""))
            rows.client[0] = 0;
        if (!em_copy(rows.neighbor, sizeof(rows.neighbor), neighbor ? neighbor : ""))
            rows.neighbor[0] = 0;
        if (!em_copy(rows.group, sizeof(rows.group), group ? group : ""))
            rows.group[0] = 0;
    } else {
        if (!bound(s))
            return;
        cJSON *w = windows(em_ovsdb_tables(s->ovs));
        const char *value = str(w, in.station);
        bool ours = value && !strncmp(value, in.target_bssid, strlen(in.target_bssid)) && value[strlen(in.target_bssid)] == '/';
        cJSON_Delete(w);
        if (!ours)
            return;
        const cJSON *row, *found = NULL;
        size_t n = 0;
        cJSON_ArrayForEach(row, em_table(em_ovsdb_tables(s->ovs), "Band_Steering_Clients"))
        {
            const char *mac = ovs_str(row, "mac");
            if (mac && !strcmp(mac, in.station)) {
                n++;
                found = row;
            }
        }
        /* the pod's row UUID: a key that does not fit one is left alone (client stays "") */
        if (n == 1 && !em_copy(rows.client, sizeof(rows.client), found->string))
            rows.client[0] = 0;
    }
    em_ovs_session os = session(s);
    if (em_steering_close(&os, &in, &rows) != EM_OK) {
        (void)fprintf(stderr, "WARNING emosa.agent.steering: client steering close failed\n");
        record(s, "close_failed");
    }
}

static void step(em_steering_scope *s)
{
    if (cJSON_GetArraySize(s->leftover)) {
        em_snapshot snap = {0};
        bool reachable = snapshot(s, &snap) && snap.ready;
        em_snapshot_clear(&snap);
        if (!reachable)
            return; /* not while the pod is away: every close would wait out its timeout */
        while (cJSON_GetArraySize(s->leftover)) {
            cJSON *op = cJSON_DetachItemFromArray(s->leftover, cJSON_GetArraySize(s->leftover) - 1);
            if (strcmp(em_state_of(op), "REQUESTED"))
                close_window(s, op, cJSON_GetObjectItemCaseSensitive(op, "intent"));
            end(s, op, "closed_after_restart");
            cJSON_Delete(op);
        }
    }
    dequeue(s);
    if (!s->active)
        return;
    cJSON *op = em_journal_get(s->journal, s->op);
    if (!op) {
        s->active = false;
        return;
    }
    if (!strcmp(s->phase, "requested")) {
        cJSON_Delete(op);
        op = em_engine_execute(&s->engine, s->op);
        const char *state = em_state_of(op);
        if (!strcmp(state, "REJECTED") || !strcmp(state, "FAILED") || !strcmp(state, "OWNERSHIP_CONFLICT") ||
            !strcmp(state, "TIMED_OUT")) {
            char outcome[96];
            const char *reason = str(op, "reason");
            (void)em_format(outcome, sizeof(outcome), "refused_%s", reason ? reason : state); /* an outcome label */
            for (char *p = outcome; *p; p++)
                if (*p >= 'A' && *p <= 'Z')
                    *p = (char)(*p + 32);
            cJSON_Delete(op);
            finish(s, outcome);
            return;
        }
        if (!strcmp(state, "INDETERMINATE")) {
            close_window(s, op, s->intent);
            end(s, op, "outcome_unknown");
            cJSON_Delete(op);
            finish(s, "outcome_unknown");
            return;
        }
        EM_FORMAT_FIXED(s->phase, sizeof(s->phase), "opening");
    }
    double now = clock_now(s);
    int on_source;
    const char *cs_state = observe(s, str(s->intent, "station"), str(s->intent, "source_bssid"), &on_source);
    bool steering = cs_state && !strcmp(cs_state, "steering");
    if (!strcmp(s->phase, "opening")) {
        em_snapshot snap = {0};
        em_reason why;
        cJSON *t = steering ? target(s, s->intent, &why) : NULL;
        if (steering && snapshot(s, &snap) && snap.ready && t && em_snapshot_satisfies(&snap, t)) {
            cJSON *evidence = em_engine_evidence(&snap, false, s->instance);
            em_transition(op, "OBSERVED_APPLIED", evidence);
            cJSON_ReplaceItemInObjectCaseSensitive(op, "application_evidence", evidence);
            em_engine_save(&s->engine, op, NULL);
            const char *client = str(cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(op, "commit_evidence"), "created"), "client");
            em_ovs_session os = session(s);
            if (!client || em_steering_kick(s->serial, &os, str(s->intent, "station"), client) != EM_OK) {
                (void)fprintf(stderr, "WARNING emosa.agent.steering: client steering kick failed\n");
                close_window(s, op, s->intent);
                end(s, op, "kick_failed");
                finish(s, "kick_failed");
            } else {
                EM_FORMAT_FIXED(s->phase, sizeof(s->phase), "kicked");
                s->kicked = now;
                record(s, "kicked");
            }
        } else if (em_engine_expired(&s->engine, op)) {
            cJSON_ReplaceItemInObjectCaseSensitive(op, "original_outcome", cJSON_CreateString(em_state_of(op)));
            cJSON_ReplaceItemInObjectCaseSensitive(op, "deadline_elapsed", cJSON_CreateTrue());
            em_transition(op, "TIMED_OUT", NULL);
            em_set_reason(op, "APPLY_TIMEOUT");
            em_engine_save(&s->engine, op, NULL);
            close_window(s, op, s->intent);
            end(s, op, "not_applied");
            finish(s, "not_applied");
        }
        cJSON_Delete(t);
        em_snapshot_clear(&snap);
        cJSON_Delete(op);
        return;
    }
    bool imminent = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(s->intent, "disassoc_imminent"));
    double limit = imminent ? num(s->intent, "window") + MARGIN : GENTLE;
    if (steering && on_source != 0 && now < s->kicked + limit) {
        cJSON_Delete(op);
        return;
    }
    close_window(s, op, s->intent);
    const char *outcome = on_source == 0 ? "left_source" : on_source == 1 ? "stayed" : "unobserved";
    end(s, op, outcome);
    cJSON_Delete(op);
    finish(s, outcome);
}

void em_steering_tick(em_steering_scope *s)
{
    step(s);
    if (!s->active && s->nqueue)
        step(s); /* the next queued mandate goes out in the same tick */
}

cJSON *em_steering_status(em_steering_scope *s)
{
    cJSON *o = cJSON_CreateObject(), *counts = cJSON_AddObjectToObject(o, "counts");
    for (size_t i = 0; i < s->counts.n; i++)
        cJSON_AddNumberToObject(counts, s->counts.items[i].name, s->counts.items[i].count);
    if (s->active) {
        cJSON *a = cJSON_AddObjectToObject(o, "active");
        cJSON_AddStringToObject(a, "station", str(s->intent, "station"));
        cJSON_AddStringToObject(a, "target", str(s->intent, "target_bssid"));
        cJSON_AddStringToObject(a, "phase", s->phase);
        cJSON_AddStringToObject(a, "operation_id", s->op);
    } else {
        cJSON_AddNullToObject(o, "active");
    }
    cJSON *queued = cJSON_AddArrayToObject(o, "queued");
    for (size_t i = 0; i < s->nqueue; i++) {
        char station[18], to[18];
        mac_text(s->queue[i].request.stations[0], station);
        mac_text(s->queue[i].request.targets[0].bssid, to);
        cJSON *q = cJSON_CreateObject();
        cJSON_AddStringToObject(q, "station", station);
        cJSON_AddStringToObject(q, "target", to);
        cJSON_AddNumberToObject(q, "waited_seconds", rint((clock_now(s) - s->queue[i].at) * 10) / 10.0);
        cJSON_AddItemToArray(queued, q);
    }
    cJSON_AddItemToObject(o, "history", cJSON_Duplicate(s->history, true));
    return o;
}

em_backend em_steering_backend(void) { return (em_backend){MODE, target, snapshot, plan, submit}; }
