/* SPDX-License-Identifier: Apache-2.0 */
/* The node-config scope (emosa.opensync.node_config, emosa.agent.node_config; spec 4, the pods'
 * modules). */
#include "scope_node.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "canon.h"
#include "log.h"
#include "ovs.h"
#include "scope.h"

#define MODE "opensync-node-config"
#define PROVENANCE "opensync:Node_Config+Node_State"
#define SOURCE "node-config-policy"
#define DEADLINE 30
#define ROWS 64          /* a module's rows of one table the scope reads, at most */
#define VALUE_MAX 1024   /* Node_Config.value: 1 to 1024, never empty */
#define RESTART_GRACE 60 /* seconds an installed update may take to run before it is flagged */
#define LOGGER "emosa.agent.node_config"

/* the keys of a module's Node_State the status reports (node_config.REPORTED; events_prev: the
 * module's events before the pod's last boot, as its own log on flash has them) */
static const char *const REPORTED[] = {"state",  "boot_id", "version",       "running",
                                       "slot",   "update",  "update_sha256", "events_prev"};
#define NREPORTED 8

static const char *str(const cJSON *o, const char *k)
{
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, k));
}

static bool same(const char *a, const char *b) { return a == b || (a && b && !strcmp(a, b)); }

static bool starts(const char *s, const char *prefix) { return s && !strncmp(s, prefix, strlen(prefix)); }

/* node_config.MODULE and KEY: [a-z0-9], then [a-z0-9] or one of `extra`, `max` in all */
static bool name_ok(const char *s, size_t max, const char *extra)
{
    size_t n = s ? strlen(s) : 0;
    if (!n || n > max)
        return false;
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        bool plain = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9');
        if (!plain && (i == 0 || !strchr(extra, c)))
            return false;
    }
    return true;
}

/* NodeConfigIntent.validate: one to EM_NODE_MODULES modules of one to EM_NODE_SETTINGS settings */
static bool valid_modules(const cJSON *modules)
{
    int n = cJSON_GetArraySize(modules);
    if (!cJSON_IsObject(modules) || n < 1 || n > EM_NODE_MODULES)
        return false;
    const cJSON *m, *v;
    cJSON_ArrayForEach(m, modules)
    {
        int k = cJSON_GetArraySize(m);
        if (!name_ok(m->string, 32, "_-") || !cJSON_IsObject(m) || k < 1 || k > EM_NODE_SETTINGS)
            return false;
        if (!strcmp(m->string, EM_START_MODULE)) /* EMOSA's own: a legacy pod's start marker */
            return false;
        cJSON_ArrayForEach(v, m)
        {
            size_t len = cJSON_IsString(v) ? strlen(v->valuestring) : 0;
            if (!name_ok(v->string, 64, "_") || !len || len > VALUE_MAX)
                return false;
        }
    }
    return true;
}

static int by_name(const void *a, const void *b) { return strcmp(*(const char *const *)a, *(const char *const *)b); }

/* an object's members in name order, copied (one level: its members' own are copied as they are) */
static cJSON *sorted(const cJSON *o)
{
    const char *names[EM_NODE_SETTINGS > EM_NODE_MODULES ? EM_NODE_SETTINGS : EM_NODE_MODULES];
    size_t n = 0;
    const cJSON *m;
    cJSON_ArrayForEach(m, o)
    {
        if (n < sizeof(names) / sizeof(*names))
            names[n++] = m->string;
    }
    qsort(names, n, sizeof(*names), by_name);
    cJSON *out = cJSON_CreateObject();
    for (size_t i = 0; i < n; i++) {
        const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, names[i]);
        cJSON_AddItemToObject(out, names[i], cJSON_IsObject(v) ? sorted(v) : cJSON_Duplicate(v, true));
    }
    return out;
}

bool em_node_intent_from(const char *pod_id, const cJSON *modules, em_node_scope *s, em_reason *why)
{
    *why = EM_INVALID_INPUT;
    if (!pod_id || !*pod_id || !valid_modules(modules))
        return false;
    cJSON_Delete(s->intent);
    s->intent = cJSON_CreateObject();
    cJSON_AddStringToObject(s->intent, "pod_id", pod_id);
    cJSON_AddItemToObject(s->intent, "modules", sorted(modules));
    *why = EM_OK;
    return true;
}

static const cJSON *modules_of(const cJSON *intent) { return cJSON_GetObjectItemCaseSensitive(intent, "modules"); }

/* NodeConfigIntent.target */
static cJSON *target(void *ctx, const cJSON *intent, em_reason *why)
{
    (void)ctx;
    if (!str(intent, "pod_id") || !valid_modules(modules_of(intent))) {
        *why = EM_INVALID_INPUT;
        return NULL;
    }
    cJSON *t = cJSON_CreateObject();
    cJSON_AddArrayToObject(t, "differs");
    return t;
}

/* -- the pod's rows -------------------------------------------------------------------- */

typedef struct {
    const char *uuid, *key, *value;
    const cJSON *row;
    const char *module; /* an insert's */
} node_row;

typedef struct {
    size_t ndeletes, nupdates, ninserts;
    node_row deletes[EM_NODE_MODULES * ROWS];
    node_row updates[EM_NODE_MODULES * EM_NODE_SETTINGS];
    node_row inserts[EM_NODE_MODULES * EM_NODE_SETTINGS];
} node_changes;

static int key_then_uuid(const void *a, const void *b)
{
    const node_row *x = a, *y = b;
    int k = strcmp(x->key, y->key);
    return k ? k : strcmp(x->uuid, y->uuid);
}

/* node_config.module_rows: the table's rows of `module` with a key, by key then uuid */
static size_t module_rows(const cJSON *tables, const char *table, const char *module, node_row *out)
{
    size_t n = 0;
    const cJSON *r;
    cJSON_ArrayForEach(r, em_table(tables, table))
    {
        const char *m = ovs_str(r, "module"), *k = ovs_str(r, "key");
        if (m && k && !strcmp(m, module) && n < ROWS)
            out[n++] = (node_row){.uuid = r->string, .key = k, .value = ovs_str(r, "value"), .row = r};
    }
    qsort(out, n, sizeof(*out), key_then_uuid);
    return n;
}

static int text_order(const void *a, const void *b)
{
    return strcmp(cJSON_GetStringValue(*(cJSON *const *)a), cJSON_GetStringValue(*(cJSON *const *)b));
}

/* NodeConfigBackend._changes: what the write makes now (`c`), and the configured view */
static cJSON *changes(const cJSON *modules, const cJSON *tables, node_changes *c)
{
    memset(c, 0, sizeof(*c));
    cJSON *differs[EM_NODE_MODULES * (ROWS + EM_NODE_SETTINGS)];
    size_t ndiffers = 0;
    cJSON *view = cJSON_CreateObject(), *seen_modules = cJSON_CreateObject();
    const cJSON *m;
    node_row rows[ROWS];
    cJSON_ArrayForEach(m, modules)
    {
        size_t n = module_rows(tables, "Node_Config", m->string, rows);
        cJSON *config = cJSON_CreateObject();
        const char *last = NULL;
        for (size_t i = 0; i < n; i++) {
            const char *wanted = str(m, rows[i].key);
            char name[100];
            EM_FORMAT_FIXED(name, sizeof(name), "%s/%s", m->string, rows[i].key); /* 32 + 64 */
            if ((last && !strcmp(last, rows[i].key)) || !wanted) { /* a second row of a key, or not ours */
                c->deletes[c->ndeletes++] = rows[i];
                differs[ndiffers++] = cJSON_CreateString(name);
                continue;
            }
            last = rows[i].key;
            cJSON_AddItemToObject(config, rows[i].key, rows[i].value ? cJSON_CreateString(rows[i].value) : cJSON_CreateNull());
            if (!same(rows[i].value, wanted)) {
                node_row update = rows[i];
                update.value = wanted;
                c->updates[c->nupdates++] = update;
                differs[ndiffers++] = cJSON_CreateString(name);
            }
        }
        const cJSON *v;
        cJSON_ArrayForEach(v, m) /* the record's settings: sorted */
        {
            if (cJSON_GetObjectItemCaseSensitive(config, v->string))
                continue;
            char name[100];
            EM_FORMAT_FIXED(name, sizeof(name), "%s/%s", m->string, v->string);
            c->inserts[c->ninserts++] = (node_row){.module = m->string, .key = v->string, .value = v->valuestring};
            differs[ndiffers++] = cJSON_CreateString(name);
        }
        /* node_config.module_state: the first row of each key */
        cJSON *state = cJSON_CreateObject();
        n = module_rows(tables, "Node_State", m->string, rows);
        for (size_t i = 0; i < n; i++)
            if (!cJSON_GetObjectItemCaseSensitive(state, rows[i].key))
                cJSON_AddItemToObject(state, rows[i].key, rows[i].value ? cJSON_CreateString(rows[i].value) : cJSON_CreateNull());
        cJSON *entry = cJSON_AddObjectToObject(seen_modules, m->string);
        cJSON_AddItemToObject(entry, "config", config);
        cJSON_AddItemToObject(entry, "state", state);
    }
    qsort(differs, ndiffers, sizeof(*differs), text_order);
    cJSON *list = cJSON_AddArrayToObject(view, "differs");
    for (size_t i = 0; i < ndiffers; i++) {
        if (i && !strcmp(differs[i]->valuestring, differs[i - 1]->valuestring)) /* each once */
            cJSON_Delete(differs[i]);
        else
            cJSON_AddItemToArray(list, differs[i]);
    }
    cJSON_AddItemToObject(view, "modules", seen_modules);
    cJSON_AddBoolToObject(view, "pending", c->ndeletes || c->nupdates || c->ninserts);
    return view;
}

static const cJSON *bind(em_node_scope *s, const char **node_uuid)
{
    const cJSON *tables = em_ovsdb_tables(s->ovs);
    const cJSON *node = em_bound_node(tables, s->serial, node_uuid);
    if (!node)
        return NULL;
    s->has_instance = em_start_instance(tables, s->instance);
    return s->has_instance ? node : NULL;
}

static bool snapshot(void *ctx, em_snapshot *out)
{
    em_node_scope *s = ctx;
    memset(out, 0, sizeof(*out));
    const char *node_uuid;
    const cJSON *node = em_ovsdb_ready(s->ovs) ? bind(s, &node_uuid) : NULL;
    if (!node) {
        if (!s->has_last)
            return false;
        out->config = cJSON_Duplicate(s->last.config, true);
        out->observed = cJSON_Duplicate(s->last.observed, true);
        cJSON_ReplaceItemInObjectCaseSensitive(out->observed, "fresh", cJSON_CreateFalse());
        out->generation = s->last.generation;
        memcpy(out->schema_fingerprint, s->last.schema_fingerprint, 65);
        return true;
    }
    node_changes *c = malloc(sizeof(*c));
    if (!c)
        return false;
    out->config = changes(modules_of(s->intent), em_ovsdb_tables(s->ovs), c);
    free(c);
    out->ready = true;
    out->generation = em_ovsdb_generation(s->ovs);
    EM_FORMAT_FIXED(out->schema_fingerprint, 65, "%s", em_ovsdb_schema_fingerprint(s->ovs)); /* SHA-256 hex */
    out->observed = em_observation(str(s->intent, "pod_id"), "node-config", cJSON_Duplicate(out->config, true), MODE,
                                   out->generation, true, PROVENANCE, em_ovsdb_revision(s->ovs));
    em_snapshot_clear(&s->last);
    s->last.config = cJSON_Duplicate(out->config, true);
    s->last.observed = cJSON_Duplicate(out->observed, true);
    s->last.generation = out->generation;
    memcpy(s->last.schema_fingerprint, out->schema_fingerprint, 65);
    s->has_last = true;
    return true;
}

static cJSON *plan(void *ctx, const cJSON *intent, em_reason *why)
{
    em_node_scope *s = ctx;
    if (!str(intent, "pod_id") || !valid_modules(modules_of(intent))) {
        *why = EM_INVALID_INPUT;
        return NULL;
    }
    if (strcmp(str(intent, "pod_id"), str(s->intent, "pod_id"))) {
        *why = EM_UNSUPPORTED_OPERATION;
        return NULL;
    }
    const char *node_uuid;
    if (!(em_ovsdb_ready(s->ovs) && bind(s, &node_uuid))) {
        *why = EM_NOT_READY;
        return NULL;
    }
    node_changes *c = malloc(sizeof(*c));
    if (!c) {
        *why = EM_NOT_READY;
        return NULL;
    }
    cJSON *view = changes(modules_of(intent), em_ovsdb_tables(s->ovs), c);
    free(c);
    cJSON *p = cJSON_CreateObject();
    cJSON_AddStringToObject(p, "mapping", MODE);
    cJSON_AddStringToObject(p, "action", "node-config");
    cJSON_AddItemToObject(p, "settings", cJSON_DetachItemFromObjectCaseSensitive(view, "differs"));
    cJSON_Delete(view);
    cJSON_AddStringToObject(p, "instance", s->instance);
    const char *fields[] = {"Node_Config.module", "Node_Config.key", "Node_Config.value"};
    cJSON_AddItemToObject(p, "fields", em_ovs_strings(fields, 3));
    cJSON_AddStringToObject(p, "guard", "pod serial and each changed row as it is");
    return p;
}

static void submit(void *ctx, const cJSON *intent, const cJSON *attempt, em_submit_out *out)
{
    em_node_scope *s = ctx;
    memset(out, 0, sizeof(*out));
    em_reason why;
    cJSON *p = plan(ctx, intent, &why);
    if (!p) {
        strcpy(out->status, "unknown"); /* the plan raised at submission */
        out->reason = EM_OUTCOME_UNKNOWN;
        return;
    }
    cJSON_Delete(p);
    int generation = 0; /* none or no int: another session */
    (void)em_json_int(cJSON_GetObjectItemCaseSensitive(attempt, "session_generation"), &generation);
    const char *node_uuid;
    const cJSON *node = bind(s, &node_uuid);
    node_changes *c = calloc(1, sizeof(*c));
    if (c && node)
        cJSON_Delete(changes(modules_of(intent), em_ovsdb_tables(s->ovs), c));
    if (!c || !node || em_ovsdb_generation(s->ovs) != generation || !(c->ndeletes || c->nupdates || c->ninserts)) {
        free(c);
        strcpy(out->status, "rejected"); /* not bound, another session, or nothing differs */
        out->reason = EM_NOT_READY;
        return;
    }
    cJSON *ops = cJSON_CreateArray(), *row;
    int counts[1 + 2 * (EM_NODE_MODULES * (ROWS + 2 * EM_NODE_SETTINGS))];
    size_t n = 0;
    const char *node_cols[] = {"serial_number"};
    row = cJSON_CreateObject();
    cJSON_AddStringToObject(row, "serial_number", s->serial);
    cJSON *wait = em_ovs_op("wait", "AWLAN_Node", em_ovs_where_uuid(node_uuid));
    cJSON_AddItemToObject(wait, "columns", em_ovs_strings(node_cols, 1));
    cJSON_AddStringToObject(wait, "until", "==");
    cJSON_AddItemToArray(cJSON_AddArrayToObject(wait, "rows"), row);
    cJSON_AddNumberToObject(wait, "timeout", 0);
    cJSON_AddItemToArray(ops, wait);
    counts[n++] = -1;
    const char *cols[] = {"module", "key", "value"};
    for (size_t k = 0; k < c->ndeletes; k++) { /* each row as it is */
        cJSON_AddItemToArray(ops, em_ovs_guard("Node_Config", c->deletes[k].uuid, c->deletes[k].row, cols, 3));
        cJSON_AddItemToArray(ops, em_ovs_op("delete", "Node_Config", em_ovs_where_uuid(c->deletes[k].uuid)));
        counts[n++] = -1;
        counts[n++] = 1;
    }
    for (size_t k = 0; k < c->nupdates; k++) {
        cJSON_AddItemToArray(ops, em_ovs_guard("Node_Config", c->updates[k].uuid, c->updates[k].row, cols, 3));
        cJSON *update = em_ovs_op("update", "Node_Config", em_ovs_where_uuid(c->updates[k].uuid));
        row = cJSON_AddObjectToObject(update, "row");
        cJSON_AddStringToObject(row, "value", c->updates[k].value);
        cJSON_AddTrueToObject(row, "persist");
        cJSON_AddItemToArray(ops, update);
        counts[n++] = -1;
        counts[n++] = 1;
    }
    for (size_t k = 0; k < c->ninserts; k++) {
        /* the key not there as it was read: a row another writer added since is a conflict */
        cJSON *where = em_ovs_where_eq("module", c->inserts[k].module), *more = em_ovs_where_eq("key", c->inserts[k].key);
        cJSON_AddItemToArray(where, cJSON_DetachItemFromArray(more, 0));
        cJSON_Delete(more);
        cJSON_AddItemToArray(ops, em_ovs_absent("Node_Config", where, "key"));
        cJSON *insert = em_ovs_op("insert", "Node_Config", NULL);
        row = cJSON_AddObjectToObject(insert, "row");
        cJSON_AddStringToObject(row, "module", c->inserts[k].module);
        cJSON_AddStringToObject(row, "key", c->inserts[k].key);
        cJSON_AddStringToObject(row, "value", c->inserts[k].value);
        cJSON_AddTrueToObject(row, "persist");
        cJSON_AddItemToArray(ops, insert);
        counts[n++] = -1;
        counts[n++] = -1;
    }
    free(c);
    cJSON *results = s->transact(s->transact_ctx, ops);
    cJSON_Delete(ops);
    if (!results) {
        strcpy(out->status, "unknown");
        out->reason = EM_OUTCOME_UNKNOWN;
        out->evidence = cJSON_CreateObject();
        cJSON_AddStringToObject(out->evidence, "attribution", "unknown");
        return;
    }
    em_reason checked = em_check_results(results, counts, n);
    if (checked != EM_OK) {
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
    cJSON_AddStringToObject(out->evidence, "action", "node-config");
    cJSON_AddStringToObject(out->evidence, "instance", s->instance);
    cJSON_AddItemToObject(out->evidence, "results", results);
}

/* -- NodeConfig ---------------------------------------------------------------------------- */

em_reason em_node_open(em_node_scope *s, const char *state_dir, const em_journal_schemas *schemas,
                       const em_vault *vault, double (*monotonic)(void))
{
    char dir[600];
    EM_FORMAT_FIXED(dir, sizeof(dir), "%s/node-config", state_dir); /* EM_STATE_DIR_MAX */
    s->monotonic = monotonic;
    em_reason why;
    s->journal = em_journal_open(dir, schemas, &why);
    if (!s->journal)
        return why;
    em_engine_init(&s->engine, s->journal, vault, str(s->intent, "pod_id"), (em_backend){MODE, target, snapshot, plan, submit},
                   s, monotonic);
    em_engine_recover(&s->engine);
    return EM_OK;
}

void em_node_close(em_node_scope *s)
{
    em_engine_free(&s->engine);
    em_journal_close(s->journal);
    em_snapshot_clear(&s->last);
    cJSON_Delete(s->intent);
    s->intent = NULL;
}

static void wait_for(em_node_scope *s, const char *why)
{
    s->has_waiting = why != NULL;
    (void)em_copy(s->waiting, sizeof(s->waiting), why ? why : ""); /* a message */
}

static void settle(em_node_scope *s, cJSON *op, const em_snapshot *snap, bool have_snap)
{
    const char *state = em_state_of(op);
    if (strcmp(state, "CONFIG_COMMITTED") && strcmp(state, "INDETERMINATE"))
        return;
    em_reason why;
    cJSON *target = s->engine.backend.target(s, cJSON_GetObjectItemCaseSensitive(op, "intent"), &why);
    const char *planned = str(cJSON_GetObjectItemCaseSensitive(op, "plan"), "instance");
    bool same_start = have_snap && s->has_instance && planned && !strcmp(planned, s->instance);
    if (target && same_start && snap->ready && em_snapshot_satisfies(snap, target) && em_matches(snap->config, target)) {
        cJSON *evidence = em_engine_evidence(snap, false, s->instance);
        if (!strcmp(state, "INDETERMINATE"))
            cJSON_AddStringToObject(evidence, "attribution", "current_condition_only");
        if (em_transition(op, "OBSERVED_APPLIED", evidence)) {
            cJSON_ReplaceItemInObjectCaseSensitive(op, "application_evidence", evidence);
            em_set_reason(op, NULL);
            em_engine_save(&s->engine, op, NULL);
            em_log(EM_LOG_INFO, LOGGER, "the pod's module settings are applied");
        } else {
            cJSON_Delete(evidence);
        }
    } else if (em_engine_expired(&s->engine, op)) {
        cJSON_ReplaceItemInObjectCaseSensitive(op, "original_outcome", cJSON_CreateString(state));
        cJSON_ReplaceItemInObjectCaseSensitive(op, "deadline_elapsed", cJSON_CreateTrue());
        em_transition(op, "TIMED_OUT", NULL);
        em_set_reason(op, "APPLY_TIMEOUT");
        em_engine_save(&s->engine, op, NULL);
    }
    cJSON_Delete(target);
}

static em_node_seen *seen_of(em_node_scope *s, const char *module)
{
    for (size_t i = 0; i < s->nseen; i++)
        if (!strcmp(s->seen[i].module, module))
            return &s->seen[i];
    if (s->nseen >= EM_NODE_MODULES)
        return NULL;
    em_node_seen *seen = &s->seen[s->nseen++];
    memset(seen, 0, sizeof(*seen));
    (void)em_copy(seen->module, sizeof(seen->module), module); /* a valid module name: 32 at most */
    seen->seq = -1;
    return seen;
}

/* NodeConfig._report: each module's new events logged once (by boot and seq), its updates
 * followed */
static void report(em_node_scope *s, const cJSON *config)
{
    const cJSON *m;
    cJSON_ArrayForEach(m, cJSON_GetObjectItemCaseSensitive(config, "modules"))
    {
        em_node_seen *seen = seen_of(s, m->string);
        if (!seen)
            continue;
        const cJSON *state = cJSON_GetObjectItemCaseSensitive(m, "state");
        const char *boot = str(state, "boot_id");
        if (!same(boot, seen->has_boot ? seen->boot : NULL)) {
            if (seen->has_boot)
                em_log(EM_LOG_INFO, LOGGER, "module %s: a new boot of the pod (%s)", m->string, boot ? boot : "None");
            seen->seq = -1;
            seen->has_boot = boot && em_copy(seen->boot, sizeof(seen->boot), boot); /* 1024 at most */
        }
        char events[VALUE_MAX + 1];
        const char *text = str(state, "events");
        if (text && em_copy(events, sizeof(events), text)) {
            char *save = NULL;
            for (char *entry = strtok_r(events, ";", &save); entry; entry = strtok_r(NULL, ";", &save)) {
                while (isspace((unsigned char)*entry))
                    entry++;
                size_t len = strlen(entry);
                while (len && isspace((unsigned char)entry[len - 1]))
                    entry[--len] = 0;
                char *rest = strchr(entry, ' ');
                bool digits = rest && rest > entry;
                for (const char *d = entry; digits && d < rest; d++)
                    digits = isdigit((unsigned char)*d);
                if (!digits || !rest[1])
                    continue;
                long seq = strtol(entry, NULL, 10);
                if (seq > seen->seq) {
                    em_log(EM_LOG_INFO, LOGGER, "module %s: event %ld %s", m->string, seq, rest + 1);
                    seen->seq = seq;
                }
            }
        }
        const char *wanted = str(cJSON_GetObjectItemCaseSensitive(modules_of(s->intent), m->string), "update_sha256");
        bool taken = wanted && same(str(state, "update"), "ok") && same(str(state, "update_sha256"), wanted) &&
                     !same(str(state, "running"), str(state, "version"));
        if (!taken) {
            seen->started = false;
        } else if (!seen->started) {
            seen->started = true;
            seen->since = s->monotonic();
        }
    }
}

void em_node_tick(em_node_scope *s)
{
    em_snapshot snap = {0};
    bool have = s->engine.backend.snapshot(s, &snap);
    if (have && snap.ready)
        report(s, snap.config);
    cJSON *op = em_journal_latest(s->journal);
    if (op)
        settle(s, op, &snap, have);
    cJSON_Delete(op);
    if (!have) {
        em_snapshot_clear(&snap);
        return;
    }
    /* NodeConfig._wanted */
    const char *pod_id = str(s->intent, "pod_id");
    op = em_journal_latest(s->journal);
    cJSON *owned = em_journal_ownership(s->journal, pod_id);
    char key[32];
    EM_FORMAT_FIXED(key, sizeof(key), "%s:node-config", s->instance); /* 16+12 */
    cJSON *done = NULL;
    bool wanted = false;
    if (!snap.ready) {
        wait_for(s, "pod not bound");
    } else if (owned) {
        wait_for(s, "another manager changed the settings: admit the pod anew");
    } else if (op && em_state_active(em_state_of(op))) {
        wait_for(s, "write in progress");
    } else if ((done = em_journal_lookup(s->journal, SOURCE, pod_id, key))) {
        if (!strcmp(em_state_of(done), "OBSERVED_APPLIED")) {
            wait_for(s, NULL);
        } else {
            char text[128];
            const char *reason = str(done, "reason");
            (void)em_format(text, sizeof(text), "not written on this start: %s%s%s", em_state_of(done), /* a message */
                            reason ? " " : "", reason ? reason : "");
            wait_for(s, text);
        }
    } else {
        wait_for(s, NULL);
        wanted = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(snap.config, "pending"));
    }
    cJSON_Delete(done);
    cJSON_Delete(owned);
    cJSON_Delete(op);
    em_snapshot_clear(&snap);
    if (!wanted)
        return;
    em_reason why;
    op = em_engine_request(&s->engine, s->intent, SOURCE, key, s->run_id, DEADLINE, "semantic", NULL, &why);
    if (op && !strcmp(em_state_of(op), "REQUESTED")) {
        cJSON *result = em_engine_execute(&s->engine,
                                          cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(op, "operation_id")));
        const char *reason = str(result, "reason");
        em_log(EM_LOG_INFO, LOGGER, "module settings %s: %s %s", str(result, "operation_id"), em_state_of(result),
               reason ? reason : "");
        cJSON_Delete(result);
    }
    cJSON_Delete(op);
}

/* NodeConfig.flags: what the module's report means */
static cJSON *flags(em_node_scope *s, const char *module, const cJSON *settings, const cJSON *state)
{
    cJSON *f = cJSON_CreateArray();
    if (starts(str(state, "state"), "error:"))
        cJSON_AddItemToArray(f, cJSON_CreateString("state_error"));
    if (same(str(state, "slot"), "none")) /* the module's hook would not run at the next boot */
        cJSON_AddItemToArray(f, cJSON_CreateString("not_started_at_boot"));
    const char *wanted = str(settings, "update_sha256");
    if (wanted && same(str(state, "update_sha256"), wanted)) {
        if (starts(str(state, "update"), "failed:"))
            cJSON_AddItemToArray(f, cJSON_CreateString("update_failed"));
        em_node_seen *seen = NULL;
        for (size_t i = 0; i < s->nseen; i++)
            if (!strcmp(s->seen[i].module, module))
                seen = &s->seen[i];
        if (seen && seen->started && s->monotonic() - seen->since > RESTART_GRACE) /* installed, not running */
            cJSON_AddItemToArray(f, cJSON_CreateString("restart_not_taken"));
    } else if (wanted) {
        cJSON_AddItemToArray(f, cJSON_CreateString("update_pending"));
    }
    return f;
}

cJSON *em_node_status(em_node_scope *s)
{
    cJSON *o = cJSON_CreateObject(), *op = em_journal_latest(s->journal);
    const cJSON *config = s->has_last ? s->last.config : NULL;
    const cJSON *seen = cJSON_GetObjectItemCaseSensitive(config, "modules");
    cJSON *modules = cJSON_AddObjectToObject(o, "modules");
    const cJSON *m, *v;
    cJSON_ArrayForEach(m, modules_of(s->intent))
    {
        const cJSON *mine = cJSON_GetObjectItemCaseSensitive(seen, m->string);
        const cJSON *written = cJSON_GetObjectItemCaseSensitive(mine, "config");
        const cJSON *state = cJSON_GetObjectItemCaseSensitive(mine, "state");
        cJSON *entry = cJSON_AddObjectToObject(modules, m->string);
        cJSON_AddBoolToObject(entry, "settings_written", written && cJSON_Compare(written, m, true));
        cJSON *echoed = cJSON_AddArrayToObject(entry, "settings_echoed"); /* the record's settings: sorted */
        cJSON_ArrayForEach(v, m)
        {
            if (same(str(state, v->string), v->valuestring))
                cJSON_AddItemToArray(echoed, cJSON_CreateString(v->string));
        }
        for (size_t k = 0; k < NREPORTED; k++) {
            const char *value = str(state, REPORTED[k]);
            cJSON_AddItemToObject(entry, REPORTED[k], value ? cJSON_CreateString(value) : cJSON_CreateNull());
        }
        cJSON_AddItemToObject(entry, "flags", flags(s, m->string, m, state));
    }
    const cJSON *differs = cJSON_GetObjectItemCaseSensitive(config, "differs");
    cJSON_AddItemToObject(o, "differs", differs ? cJSON_Duplicate(differs, 1) : cJSON_CreateNull());
    cJSON_AddItemToObject(o, "waiting", s->has_waiting ? cJSON_CreateString(s->waiting) : cJSON_CreateNull());
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
