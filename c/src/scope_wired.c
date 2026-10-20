/* SPDX-License-Identifier: Apache-2.0 */
/* The wired uplink scope (emosa.opensync.wired, emosa.agent.wired; spec §8.4). */
#include "scope_wired.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "canon.h"
#include "log.h"
#include "ovs.h"
#include "scope.h"
#include "southbound.h"

#define MODE "opensync-6.6-wired-uplink"
#define PROVENANCE "opensync-cm:Connection_Manager_Uplink"
#define SOURCE "wired-uplink-policy"
#define DEADLINE 30

static const char *str(const cJSON *o, const char *k)
{
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, k));
}

/* an interface name: 1 to 15 of [A-Za-z0-9_.-] (the reference's INTERFACE) */
static bool interface_name(const char *s)
{
    size_t n = s ? strlen(s) : 0;
    if (n < 1 || n > 15)
        return false;
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '.' ||
              c == '-'))
            return false;
    }
    return true;
}

/* WiredIntent.validate */
static em_reason validate(const em_wired_intent *i)
{
    return interface_name(i->port) && interface_name(i->bridge) ? EM_OK : EM_INVALID_INPUT;
}

bool em_wired_intent_from(const char *pod_id, const cJSON *uplink, const char *bridge, em_wired_intent *out,
                          em_reason *why)
{
    memset(out, 0, sizeof(*out));
    *why = EM_INVALID_INPUT;
    const char *port = str(uplink, "port");
    /* a value that does not fit is refused, not cut: validate() would accept the cut one */
    if (!bridge || !em_copy(out->pod_id, sizeof(out->pod_id), pod_id) ||
        !em_copy(out->port, sizeof(out->port), port ? port : "eth1") ||
        !em_copy(out->bridge, sizeof(out->bridge), bridge))
        return false;
    *why = validate(out);
    return *why == EM_OK;
}

/* WiredIntent.record() */
static cJSON *record(const em_wired_intent *i)
{
    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "pod_id", i->pod_id);
    cJSON_AddStringToObject(r, "port", i->port);
    cJSON_AddStringToObject(r, "bridge", i->bridge);
    return r;
}

static bool from_record(const cJSON *r, em_wired_intent *i)
{
    memset(i, 0, sizeof(*i));
    if (!str(r, "pod_id") || !str(r, "port") || !str(r, "bridge"))
        return false;
    if (!em_copy(i->pod_id, sizeof(i->pod_id), str(r, "pod_id")) || !em_copy(i->port, sizeof(i->port), str(r, "port")) ||
        !em_copy(i->bridge, sizeof(i->bridge), str(r, "bridge")))
        return false;
    return validate(i) == EM_OK;
}

static cJSON *target_of(const em_wired_intent *i)
{
    cJSON *t = cJSON_CreateObject();
    cJSON_AddStringToObject(t, "port", i->port);
    cJSON_AddTrueToObject(t, "uplink");
    cJSON_AddStringToObject(t, "bridge", i->bridge);
    return t;
}

static cJSON *target(void *ctx, const cJSON *intent, em_reason *why)
{
    (void)ctx;
    em_wired_intent i;
    if (!from_record(intent, &i)) {
        *why = EM_INVALID_INPUT;
        return NULL;
    }
    return target_of(&i);
}

/* -- the pod's rows -------------------------------------------------------------------- */

/* the port's Connection_Manager_Uplink row, when exactly one (the reference's uplink_row) */
static const cJSON *uplink_row(const cJSON *tables, const char *port, const char **uuid)
{
    const cJSON *found = NULL, *r;
    size_t n = 0;
    cJSON_ArrayForEach(r, em_table(tables, "Connection_Manager_Uplink"))
    {
        const char *name = ovs_str(r, "if_name");
        if (name && !strcmp(name, port)) {
            found = r;
            n++;
        }
    }
    if (n != 1)
        return NULL;
    if (uuid)
        *uuid = found->string; /* the row's key in {uuid: row} */
    return found;
}

static bool in_use(const cJSON *row)
{
    const char *type = row ? ovs_str(row, "if_type") : NULL;
    return type && !strcmp(type, "eth") && ovs_true(row, "is_used");
}

/* the row's bridge, NULL when unset or empty (the reference's `or None`) */
static const char *bridge_of(const cJSON *row)
{
    const char *b = row ? ovs_str(row, "bridge") : NULL;
    return b && *b ? b : NULL;
}

/* -- a legacy pod's patch pair (spec 8.4) --------------------------------------------------- */

#define PATCH_WAN "patch-w2h"
#define PATCH_HOME "patch-h2w"

/* the one Bridge `name`, else NULL (bridge_ports) */
static const cJSON *bridge_row(const cJSON *tables, const char *name)
{
    const cJSON *found = NULL, *r;
    size_t n = 0;
    cJSON_ArrayForEach(r, em_table(tables, "Bridge"))
    {
        const char *rn = ovs_str(r, "name");
        if (rn && !strcmp(rn, name)) {
            found = r;
            n++;
        }
    }
    return n == 1 ? found : NULL;
}

/* the Port `port` is one of the bridge's ports */
static bool bridge_has_port(const cJSON *tables, const cJSON *bridge, const char *port)
{
    const char *uuids[256];
    size_t n = bridge ? ovs_uuids(bridge, "ports", uuids, 256) : 0;
    for (size_t i = 0; i < n; i++) {
        const char *name = ovs_str(cJSON_GetObjectItemCaseSensitive(em_table(tables, "Port"), uuids[i]), "name");
        if (name && !strcmp(name, port))
            return true;
    }
    return false;
}

static bool port_exists(const cJSON *tables, const char *name)
{
    const cJSON *r;
    cJSON_ArrayForEach(r, em_table(tables, "Port"))
    {
        const char *rn = ovs_str(r, "name");
        if (rn && !strcmp(rn, name))
            return true;
    }
    return false;
}

typedef enum { PAIR_NONE, PAIR_PAIRED, PAIR_OTHER } pair_state;

/* WiredBackend._paired: both ends in their bridges, neither Port there, or anything else */
static pair_state paired(const em_wired_scope *w, const cJSON *tables, const char *home)
{
    if (bridge_has_port(tables, bridge_row(tables, w->wan_bridge), PATCH_WAN) &&
        bridge_has_port(tables, bridge_row(tables, home), PATCH_HOME))
        return PAIR_PAIRED;
    if (!port_exists(tables, PATCH_WAN) && !port_exists(tables, PATCH_HOME))
        return PAIR_NONE;
    return PAIR_OTHER;
}

/* WiredBackend._configured */
static cJSON *configured(const em_wired_scope *w, const cJSON *tables)
{
    const cJSON *row = uplink_row(tables, w->intent.port, NULL);
    cJSON *c = cJSON_CreateObject();
    cJSON_AddStringToObject(c, "port", w->intent.port);
    cJSON_AddBoolToObject(c, "uplink", in_use(row));
    /* a legacy pod: the bridge the pair joins the WAN bridge to, while the pair is there */
    const char *bridge = w->legacy ? (paired(w, tables, w->intent.bridge) == PAIR_PAIRED ? w->intent.bridge : NULL)
                                   : bridge_of(row);
    cJSON_AddItemToObject(c, "bridge", bridge ? cJSON_CreateString(bridge) : cJSON_CreateNull());
    return c;
}

static const cJSON *bind(em_wired_scope *w, const char **node_uuid)
{
    const cJSON *tables = em_ovsdb_tables(w->ovs);
    const cJSON *node = em_bound_node(tables, w->serial, node_uuid);
    if (!node)
        return NULL;
    w->has_instance = em_start_instance(tables, w->instance);
    return w->has_instance ? node : NULL;
}

static bool snapshot(void *ctx, em_snapshot *out)
{
    em_wired_scope *w = ctx;
    memset(out, 0, sizeof(*out));
    const char *node_uuid;
    const cJSON *node = em_ovsdb_ready(w->ovs) ? bind(w, &node_uuid) : NULL;
    if (!node) {
        if (!w->has_last)
            return false;
        out->config = cJSON_Duplicate(w->last.config, true);
        out->observed = cJSON_Duplicate(w->last.observed, true);
        cJSON_ReplaceItemInObjectCaseSensitive(out->observed, "fresh", cJSON_CreateFalse());
        out->generation = w->last.generation;
        memcpy(out->schema_fingerprint, w->last.schema_fingerprint, 65);
        return true;
    }
    out->config = configured(w, em_ovsdb_tables(w->ovs));
    out->ready = true;
    out->generation = em_ovsdb_generation(w->ovs);
    EM_FORMAT_FIXED(out->schema_fingerprint, 65, "%s", em_ovsdb_schema_fingerprint(w->ovs)); /* SHA-256 hex */
    out->observed = em_observation(w->intent.pod_id, "wired-uplink", cJSON_Duplicate(out->config, true), MODE,
                                   out->generation, true, PROVENANCE, em_ovsdb_revision(w->ovs));
    em_snapshot_clear(&w->last);
    w->last.config = cJSON_Duplicate(out->config, true);
    w->last.observed = cJSON_Duplicate(out->observed, true);
    w->last.generation = out->generation;
    memcpy(w->last.schema_fingerprint, out->schema_fingerprint, 65);
    w->has_last = true;
    return true;
}

/* WiredBackend._check with the pod's rows: the port's row in use, its bridge none or ours */
static em_reason check(em_wired_scope *w, const em_wired_intent *i, const cJSON **row, const char **row_uuid)
{
    if (strcmp(i->pod_id, w->intent.pod_id) || strcmp(i->port, w->intent.port))
        return EM_UNSUPPORTED_OPERATION;
    const cJSON *tables = em_ovsdb_tables(w->ovs);
    *row = uplink_row(tables, i->port, row_uuid);
    if (!*row || !in_use(*row))
        return EM_NOT_READY; /* "the port is not the pod's Ethernet uplink in use" */
    if (w->legacy) {
        if (!bridge_row(tables, w->wan_bridge) || !bridge_row(tables, i->bridge))
            return EM_NOT_READY; /* "the pod's WAN or home bridge is absent" */
        /* half a pair, or its ends elsewhere: another manager's, not taken over */
        return paired(w, tables, i->bridge) == PAIR_OTHER ? EM_OWNERSHIP_CONFLICT : EM_OK;
    }
    const char *current = bridge_of(*row);
    if (current && strcmp(current, i->bridge))
        return EM_OWNERSHIP_CONFLICT; /* another manager's bridge is not taken over */
    return EM_OK;
}

static cJSON *plan(void *ctx, const cJSON *intent, em_reason *why)
{
    em_wired_scope *w = ctx;
    em_wired_intent i;
    if (!from_record(intent, &i)) {
        *why = EM_INVALID_INPUT;
        return NULL;
    }
    if (strcmp(i.pod_id, w->intent.pod_id) || strcmp(i.port, w->intent.port)) {
        *why = EM_UNSUPPORTED_OPERATION;
        return NULL;
    }
    const char *node_uuid, *row_uuid;
    const cJSON *row, *node = em_ovsdb_ready(w->ovs) ? bind(w, &node_uuid) : NULL;
    if (!node) {
        *why = EM_NOT_READY;
        return NULL;
    }
    if ((*why = check(w, &i, &row, &row_uuid)) != EM_OK)
        return NULL;
    cJSON *p = cJSON_CreateObject();
    cJSON_AddStringToObject(p, "mapping", MODE);
    if (w->legacy) {
        cJSON_AddStringToObject(p, "action", "wired-uplink-patch-pair");
        cJSON_AddStringToObject(p, "port", i.port);
        cJSON_AddStringToObject(p, "bridge", i.bridge);
        cJSON_AddStringToObject(p, "wan_bridge", w->wan_bridge);
        cJSON_AddStringToObject(p, "instance", w->instance);
        const char *fields[] = {"Interface", "Port", "Bridge.ports"};
        cJSON_AddItemToObject(p, "fields", em_ovs_strings(fields, 3));
        cJSON_AddStringToObject(p, "guard", "pod serial, the uplink row's if_name, if_type and is_used, no pair");
        return p;
    }
    cJSON_AddStringToObject(p, "action", "wired-uplink-bridge");
    cJSON_AddStringToObject(p, "port", i.port);
    cJSON_AddStringToObject(p, "bridge", i.bridge);
    cJSON_AddStringToObject(p, "instance", w->instance);
    const char *fields[] = {"Connection_Manager_Uplink.bridge"};
    cJSON_AddItemToObject(p, "fields", em_ovs_strings(fields, 1));
    cJSON_AddStringToObject(p, "guard", "pod serial and the uplink row's if_name, if_type, is_used and bridge");
    return p;
}

static cJSON *named_set(const char *name)
{
    cJSON *ref = cJSON_CreateArray(), *items = cJSON_CreateArray(), *set = cJSON_CreateArray();
    cJSON_AddItemToArray(ref, cJSON_CreateString("named-uuid"));
    cJSON_AddItemToArray(ref, cJSON_CreateString(name));
    cJSON_AddItemToArray(items, ref);
    cJSON_AddItemToArray(set, cJSON_CreateString("set"));
    cJSON_AddItemToArray(set, items);
    return set;
}

/* WiredBackend._pair_transaction after its two guards: no pair yet, the two patch Interfaces and
 * their Ports, each Port into its bridge */
static void pair_ops(const em_wired_scope *w, const char *home, cJSON *ops)
{
    const char *const ends[2] = {PATCH_WAN, PATCH_HOME}, *const tags[2] = {"w", "h"};
    const char *const bridges[2] = {w->wan_bridge, home};
    for (int k = 0; k < 2; k++) {
        cJSON *absent = em_ovs_op("wait", "Port", em_ovs_where_eq("name", ends[k]));
        const char *cols[] = {"name"};
        cJSON_AddItemToObject(absent, "columns", em_ovs_strings(cols, 1));
        cJSON_AddStringToObject(absent, "until", "==");
        cJSON_AddArrayToObject(absent, "rows");
        cJSON_AddNumberToObject(absent, "timeout", 0);
        cJSON_AddItemToArray(ops, absent);
    }
    char label[16];
    for (int k = 0; k < 2; k++) {
        cJSON *ins = em_ovs_op("insert", "Interface", NULL), *row = cJSON_CreateObject();
        EM_FORMAT_FIXED(label, sizeof(label), "patch_%s_if", tags[k]);
        cJSON_AddStringToObject(ins, "uuid-name", label);
        cJSON_AddStringToObject(row, "name", ends[k]);
        cJSON_AddStringToObject(row, "type", "patch");
        cJSON *peer = cJSON_CreateArray(), *pairs = cJSON_CreateArray(), *map = cJSON_CreateArray();
        cJSON_AddItemToArray(peer, cJSON_CreateString("peer"));
        cJSON_AddItemToArray(peer, cJSON_CreateString(ends[1 - k]));
        cJSON_AddItemToArray(pairs, peer);
        cJSON_AddItemToArray(map, cJSON_CreateString("map"));
        cJSON_AddItemToArray(map, pairs);
        cJSON_AddItemToObject(row, "options", map);
        cJSON_AddItemToObject(ins, "row", row);
        cJSON_AddItemToArray(ops, ins);
    }
    for (int k = 0; k < 2; k++) {
        cJSON *ins = em_ovs_op("insert", "Port", NULL), *row = cJSON_CreateObject();
        EM_FORMAT_FIXED(label, sizeof(label), "patch_%s_port", tags[k]);
        cJSON_AddStringToObject(ins, "uuid-name", label);
        cJSON_AddStringToObject(row, "name", ends[k]);
        EM_FORMAT_FIXED(label, sizeof(label), "patch_%s_if", tags[k]);
        cJSON_AddItemToObject(row, "interfaces", named_set(label));
        cJSON_AddItemToObject(ins, "row", row);
        cJSON_AddItemToArray(ops, ins);
    }
    for (int k = 0; k < 2; k++) {
        cJSON *mut = em_ovs_op("mutate", "Bridge", em_ovs_where_eq("name", bridges[k]));
        cJSON *mutations = cJSON_AddArrayToObject(mut, "mutations"), *m = cJSON_CreateArray();
        EM_FORMAT_FIXED(label, sizeof(label), "patch_%s_port", tags[k]);
        cJSON_AddItemToArray(m, cJSON_CreateString("ports"));
        cJSON_AddItemToArray(m, cJSON_CreateString("insert"));
        cJSON_AddItemToArray(m, named_set(label));
        cJSON_AddItemToArray(mutations, m);
        cJSON_AddItemToArray(ops, mut);
    }
}

static void submit(void *ctx, const cJSON *intent, const cJSON *attempt, em_submit_out *out)
{
    em_wired_scope *w = ctx;
    memset(out, 0, sizeof(*out));
    em_wired_intent i;
    em_reason why;
    cJSON *p = plan(ctx, intent, &why);
    if (!p || !from_record(intent, &i)) {
        cJSON_Delete(p);
        strcpy(out->status, "unknown"); /* the plan raised at submission */
        out->reason = EM_OUTCOME_UNKNOWN;
        return;
    }
    cJSON_Delete(p);
    int generation = 0; /* none or no int: another session */
    (void)em_json_int(cJSON_GetObjectItemCaseSensitive(attempt, "session_generation"), &generation);
    const char *node_uuid, *row_uuid;
    const cJSON *row, *node = bind(w, &node_uuid);
    if (!node || em_ovsdb_generation(w->ovs) != generation || check(w, &i, &row, &row_uuid) != EM_OK) {
        strcpy(out->status, "rejected");
        out->reason = EM_NOT_READY;
        return;
    }
    cJSON *ops = cJSON_CreateArray();
    /* a legacy pod's pair: two guards, two absences, four inserts, two Bridge mutations */
    const int counts[] = {-1, -1, 1}, pair_counts[] = {-1, -1, -1, -1, -1, -1, -1, -1, 1, 1};
    /* wait: the node's serial */
    cJSON *wait = em_ovs_op("wait", "AWLAN_Node", em_ovs_where_uuid(node_uuid));
    const char *node_cols[] = {"serial_number"};
    cJSON_AddItemToObject(wait, "columns", em_ovs_strings(node_cols, 1));
    cJSON_AddStringToObject(wait, "until", "==");
    cJSON *rows = cJSON_AddArrayToObject(wait, "rows"), *guard = cJSON_CreateObject();
    cJSON_AddStringToObject(guard, "serial_number", w->serial);
    cJSON_AddItemToArray(rows, guard);
    cJSON_AddNumberToObject(wait, "timeout", 0);
    cJSON_AddItemToArray(ops, wait);
    /* wait: the uplink row as it is now (its bridge none or ours; a legacy pod has no bridge) */
    wait = em_ovs_op("wait", "Connection_Manager_Uplink", em_ovs_where_uuid(row_uuid));
    const char *row_cols[] = {"if_name", "if_type", "is_used", "bridge"};
    cJSON_AddItemToObject(wait, "columns", em_ovs_strings(row_cols, w->legacy ? 3 : 4));
    cJSON_AddStringToObject(wait, "until", "==");
    rows = cJSON_AddArrayToObject(wait, "rows");
    guard = cJSON_CreateObject();
    cJSON_AddStringToObject(guard, "if_name", i.port);
    cJSON_AddStringToObject(guard, "if_type", "eth");
    cJSON_AddTrueToObject(guard, "is_used");
    const char *current = bridge_of(row);
    if (w->legacy) {
        /* the pair is the bridging (spec 8.4) */
    } else if (current) {
        cJSON_AddStringToObject(guard, "bridge", current);
    } else {
        const char *set_items[] = {"set"};
        cJSON *empty = em_ovs_strings(set_items, 1);
        cJSON_AddItemToArray(empty, cJSON_CreateArray());
        cJSON_AddItemToObject(guard, "bridge", empty); /* ["set", []] */
    }
    cJSON_AddItemToArray(rows, guard);
    cJSON_AddNumberToObject(wait, "timeout", 0);
    cJSON_AddItemToArray(ops, wait);
    if (w->legacy) {
        pair_ops(w, i.bridge, ops);
    } else {
        cJSON *update = em_ovs_op("update", "Connection_Manager_Uplink", em_ovs_where_uuid(row_uuid));
        cJSON *urow = cJSON_CreateObject();
        cJSON_AddStringToObject(urow, "bridge", i.bridge);
        cJSON_AddItemToObject(update, "row", urow);
        cJSON_AddItemToArray(ops, update);
    }
    size_t nops = (size_t)cJSON_GetArraySize(ops);
    cJSON *results = w->transact(w->transact_ctx, ops);
    cJSON_Delete(ops);
    if (!results) {
        strcpy(out->status, "unknown");
        out->reason = EM_OUTCOME_UNKNOWN;
        out->evidence = cJSON_CreateObject();
        cJSON_AddStringToObject(out->evidence, "attribution", "unknown");
        return;
    }
    em_reason checked = em_check_results(results, w->legacy ? pair_counts : counts, nops);
    if (checked != EM_OK) {
        out->evidence = em_failed_check_evidence(checked, results); /* mapping.failed_check */
        cJSON_Delete(results);
        EM_FORMAT_FIXED(out->status, sizeof(out->status), "%s",
                        checked == EM_PRECONDITION_FAILED ? "conflict" : checked == EM_OUTCOME_UNKNOWN ? "unknown" : "rejected");
        out->reason = checked;
        return;
    }
    strcpy(out->status, "committed");
    out->evidence = cJSON_CreateObject();
    cJSON_AddStringToObject(out->evidence, "attribution", "reply");
    cJSON_AddTrueToObject(out->evidence, "transaction_validated");
    cJSON_AddStringToObject(out->evidence, "transaction_id", str(attempt, "transaction_id"));
    cJSON_AddNumberToObject(out->evidence, "session_generation", generation);
    cJSON_AddStringToObject(out->evidence, "action", w->legacy ? "wired-uplink-patch-pair" : "wired-uplink-bridge");
    cJSON_AddStringToObject(out->evidence, "instance", w->instance);
    cJSON_AddItemToObject(out->evidence, "results", results);
}

/* -- WiredUplink -------------------------------------------------------------------------- */

em_reason em_wired_open(em_wired_scope *w, const char *state_dir, const em_journal_schemas *schemas,
                        const em_vault *vault, double (*monotonic)(void))
{
    char dir[600];
    EM_FORMAT_FIXED(dir, sizeof(dir), "%s/wired-uplink", state_dir); /* EM_STATE_DIR_MAX */
    em_reason why;
    w->journal = em_journal_open(dir, schemas, &why);
    if (!w->journal)
        return why;
    em_engine_init(&w->engine, w->journal, vault, w->intent.pod_id, (em_backend){MODE, target, snapshot, plan, submit},
                   w, monotonic);
    em_engine_recover(&w->engine);
    return EM_OK;
}

void em_wired_close(em_wired_scope *w)
{
    em_engine_free(&w->engine);
    em_journal_close(w->journal);
    em_snapshot_clear(&w->last);
}

static void wait_for(em_wired_scope *w, const char *why)
{
    w->has_waiting = why != NULL;
    (void)em_copy(w->waiting, sizeof(w->waiting), why ? why : ""); /* a message */
}

static void settle(em_wired_scope *w, cJSON *op, const em_snapshot *snap, bool have_snap)
{
    const char *state = em_state_of(op);
    if (strcmp(state, "CONFIG_COMMITTED") && strcmp(state, "INDETERMINATE"))
        return;
    em_reason why;
    cJSON *target = w->engine.backend.target(w, cJSON_GetObjectItemCaseSensitive(op, "intent"), &why);
    const char *planned = str(cJSON_GetObjectItemCaseSensitive(op, "plan"), "instance");
    bool same_start = have_snap && w->has_instance && planned && !strcmp(planned, w->instance);
    if (target && same_start && snap->ready && em_snapshot_satisfies(snap, target) && em_matches(snap->config, target)) {
        cJSON *evidence = em_engine_evidence(snap, false, w->instance);
        if (!strcmp(state, "INDETERMINATE"))
            cJSON_AddStringToObject(evidence, "attribution", "current_condition_only");
        if (em_transition(op, "OBSERVED_APPLIED", evidence)) {
            cJSON_ReplaceItemInObjectCaseSensitive(op, "application_evidence", evidence);
            em_set_reason(op, NULL);
            em_engine_save(&w->engine, op, NULL);
            em_log(EM_LOG_INFO, "emosa.agent.wired", "the pod's uplink %s is in %s", w->intent.port, w->intent.bridge);
        } else {
            cJSON_Delete(evidence);
        }
    } else if (em_engine_expired(&w->engine, op)) {
        cJSON_ReplaceItemInObjectCaseSensitive(op, "original_outcome", cJSON_CreateString(state));
        cJSON_ReplaceItemInObjectCaseSensitive(op, "deadline_elapsed", cJSON_CreateTrue());
        em_transition(op, "TIMED_OUT", NULL);
        em_set_reason(op, "APPLY_TIMEOUT");
        em_engine_save(&w->engine, op, NULL);
    }
    cJSON_Delete(target);
}

void em_wired_tick(em_wired_scope *w)
{
    em_snapshot snap = {0};
    bool have = w->engine.backend.snapshot(w, &snap);
    cJSON *op = em_journal_latest(w->journal);
    if (op)
        settle(w, op, &snap, have);
    cJSON_Delete(op);
    if (!have) {
        em_snapshot_clear(&snap);
        return;
    }
    /* WiredUplink._wanted */
    op = em_journal_latest(w->journal);
    cJSON *owned = em_journal_ownership(w->journal, w->intent.pod_id);
    char key[64];
    EM_FORMAT_FIXED(key, sizeof(key), "%s:%s:%s", w->instance, w->intent.port, w->intent.bridge); /* 16+1+15+1+15 */
    const char *bridge = str(snap.config, "bridge");
    cJSON *done = NULL;
    bool wanted = false;
    if (!snap.ready) {
        wait_for(w, "pod not bound");
    } else if (owned) {
        wait_for(w, "another manager changed the uplink: admit the pod anew");
    } else if (op && em_state_active(em_state_of(op))) {
        wait_for(w, "write in progress");
    } else if (!cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(snap.config, "uplink"))) {
        char text[128];
        EM_FORMAT_FIXED(text, sizeof(text), "%s is not the pod's Ethernet uplink in use", w->intent.port);
        wait_for(w, text);
    } else if (bridge && !strcmp(bridge, w->intent.bridge)) {
        wait_for(w, NULL); /* already bridged on this start (by EMOSA or anyone) */
    } else if ((done = em_journal_lookup(w->journal, SOURCE, w->intent.pod_id, key))) {
        if (!strcmp(em_state_of(done), "OBSERVED_APPLIED")) {
            wait_for(w, NULL);
        } else {
            char text[128];
            const char *reason = str(done, "reason");
            (void)em_format(text, sizeof(text), "not bridged on this start: %s%s%s", em_state_of(done), /* a message */
                            reason ? " " : "", reason ? reason : "");
            wait_for(w, text);
        }
    } else {
        wait_for(w, NULL);
        wanted = true;
    }
    cJSON_Delete(done);
    cJSON_Delete(owned);
    cJSON_Delete(op);
    em_snapshot_clear(&snap);
    if (!wanted)
        return;
    cJSON *intent = record(&w->intent);
    em_reason why;
    op = em_engine_request(&w->engine, intent, SOURCE, key, w->run_id, DEADLINE, "semantic", NULL, &why);
    cJSON_Delete(intent);
    if (op && !strcmp(em_state_of(op), "REQUESTED")) {
        cJSON *result = em_engine_execute(&w->engine,
                                          cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(op, "operation_id")));
        const char *reason = str(result, "reason");
        em_log(EM_LOG_INFO, "emosa.agent.wired", "wired uplink %s: %s %s", str(result, "operation_id"),
               em_state_of(result), reason ? reason : "");
        cJSON_Delete(result);
    }
    cJSON_Delete(op);
}

cJSON *em_wired_status(em_wired_scope *w)
{
    cJSON *o = cJSON_CreateObject(), *op = em_journal_latest(w->journal);
    cJSON_AddNullToObject(o, "station");
    cJSON_AddStringToObject(o, "mode", "ethernet");
    cJSON_AddStringToObject(o, "port", w->intent.port);
    const char *bridge = w->has_last ? str(w->last.config, "bridge") : NULL;
    cJSON_AddItemToObject(o, "bridge", bridge ? cJSON_CreateString(bridge) : cJSON_CreateNull());
    bool used = w->has_last && cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(w->last.config, "uplink"));
    cJSON_AddItemToObject(o, "in_use", used ? cJSON_CreateString(w->intent.port) : cJSON_CreateNull());
    cJSON_AddItemToObject(o, "waiting", w->has_waiting ? cJSON_CreateString(w->waiting) : cJSON_CreateNull());
    if (op) {
        cJSON *x = cJSON_AddObjectToObject(o, "operation");
        cJSON_AddItemToObject(x, "operation_id", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "operation_id"), 1));
        cJSON_AddItemToObject(x, "state", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "state"), 1));
        cJSON_AddItemToObject(x, "reason", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "reason"), 1));
        const char *instance = str(cJSON_GetObjectItemCaseSensitive(op, "plan"), "instance");
        cJSON_AddItemToObject(x, "instance", instance ? cJSON_CreateString(instance) : cJSON_CreateNull());
    } else {
        cJSON_AddNullToObject(o, "operation");
    }
    cJSON_Delete(op);
    return o;
}

em_backend em_wired_backend(void) { return (em_backend){MODE, target, snapshot, plan, submit}; }
