/* The telemetry scope (emosa.opensync.telemetry, emosa.agent.telemetry). */
#include "scope_telemetry.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "log.h"
#include "ovs.h"
#include "scope.h"
#include "southbound.h"

#define MODE "opensync-6.6-telemetry"
#define PROVENANCE "opensync:AWLAN_Node.mqtt_settings+Wifi_Stats_Config"
#define SOURCE "telemetry-policy"
#define DEADLINE 30

static const char *str(const cJSON *o, const char *k)
{
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, k));
}

static int num(const cJSON *o, const char *k, int fallback)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, k);
    return cJSON_IsNumber(v) ? v->valueint : fallback;
}

/* TelemetryIntent.validate */
static em_reason validate(const em_telemetry_intent *i)
{
    static const char *const types[] = {"2.4G", "5G", "5GL", "5GU", "6G"};
    if (!*i->broker || strpbrk(i->broker, " /:") || strlen(i->broker) > 253)
        return EM_INVALID_INPUT;
    if (i->port < 1 || i->port > 65535)
        return EM_INVALID_INPUT;
    if (!*i->topic || strlen(i->topic) > 128 || strpbrk(i->topic, "#+"))
        return EM_INVALID_INPUT;
    bool known = false;
    for (size_t k = 0; k < 5; k++)
        known = known || !strcmp(i->radio_type, types[k]);
    if (!known)
        return EM_INVALID_INPUT;
    if (!(1 <= i->sampling_interval && i->sampling_interval <= i->reporting_interval && i->reporting_interval <= 3600))
        return EM_INVALID_INPUT;
    if (i->publish_interval && (i->publish_interval < 1 || i->publish_interval > 3600))
        return EM_INVALID_INPUT;
    return EM_OK;
}

bool em_telemetry_intent_from(const char *pod_id, const char *serial, const cJSON *config,
                              em_telemetry_intent *out, em_reason *why)
{
    memset(out, 0, sizeof(*out));
    *why = EM_INVALID_INPUT;
    const char *broker = str(config, "broker");
    if (!broker || !*broker)
        return false; /* "telemetry: mode mqtt needs a broker" */
    /* a value that does not fit is refused, not cut: validate() would accept the cut one */
    const char *topic = str(config, "topic"), *radio_type = str(config, "radio_type");
    if (!em_copy(out->pod_id, sizeof(out->pod_id), pod_id) || !em_copy(out->broker, sizeof(out->broker), broker) ||
        !(topic && *topic ? em_copy(out->topic, sizeof(out->topic), topic)
                          : em_format(out->topic, sizeof(out->topic), "emosa/stats/%s", serial)) ||
        !em_copy(out->radio_type, sizeof(out->radio_type), radio_type ? radio_type : "2.4G"))
        return false;
    out->port = num(config, "port", 8883);
    out->reporting_interval = num(config, "reporting_interval", 10);
    out->sampling_interval = num(config, "sampling_interval", 5);
    out->publish_interval = num(config, "publish_interval", 0);
    out->survey = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(config, "survey"));
    *why = validate(out);
    return *why == EM_OK;
}

/* TelemetryIntent.record() */
static cJSON *record(const em_telemetry_intent *i)
{
    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "pod_id", i->pod_id);
    cJSON_AddStringToObject(r, "broker", i->broker);
    cJSON_AddNumberToObject(r, "port", i->port);
    cJSON_AddStringToObject(r, "topic", i->topic);
    cJSON_AddStringToObject(r, "radio_type", i->radio_type);
    cJSON_AddNumberToObject(r, "reporting_interval", i->reporting_interval);
    cJSON_AddNumberToObject(r, "sampling_interval", i->sampling_interval);
    if (i->publish_interval)
        cJSON_AddNumberToObject(r, "publish_interval", i->publish_interval);
    if (i->survey)
        cJSON_AddTrueToObject(r, "survey");
    return r;
}

static bool from_record(const cJSON *r, em_telemetry_intent *i)
{
    memset(i, 0, sizeof(*i));
    if (!str(r, "pod_id") || !str(r, "broker") || !str(r, "topic") || !str(r, "radio_type"))
        return false;
    if (!em_copy(i->pod_id, sizeof(i->pod_id), str(r, "pod_id")) || !em_copy(i->broker, sizeof(i->broker), str(r, "broker")) ||
        !em_copy(i->topic, sizeof(i->topic), str(r, "topic")) ||
        !em_copy(i->radio_type, sizeof(i->radio_type), str(r, "radio_type")))
        return false;
    i->port = num(r, "port", 0);
    i->reporting_interval = num(r, "reporting_interval", 10);
    i->sampling_interval = num(r, "sampling_interval", 5);
    i->publish_interval = num(r, "publish_interval", 0);
    i->survey = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(r, "survey"));
    return validate(i) == EM_OK;
}

static void client_stats(const em_telemetry_intent *i, char *out, size_t n)
{
    EM_FORMAT_FIXED(out, n, "%s/raw/%d/%d", i->radio_type, i->reporting_interval, i->sampling_interval);
}

static void survey_stats(const em_telemetry_intent *i, char *out, size_t n)
{
    EM_FORMAT_FIXED(out, n, "%s/on-chan/raw/%d/%d", i->radio_type, i->reporting_interval, i->sampling_interval);
}

/* TelemetryIntent.settings(): mqtt_settings, sorted by key as the reference writes them */
static cJSON *settings(const em_telemetry_intent *i)
{
    char port[12], publish[12];
    EM_FORMAT_FIXED(port, sizeof(port), "%d", i->port);
    EM_FORMAT_FIXED(publish, sizeof(publish), "%d", i->publish_interval);
    cJSON *s = cJSON_CreateObject();
    if (i->publish_interval)
        cJSON_AddStringToObject(s, "agg_stats_interval", publish);
    cJSON_AddStringToObject(s, "broker", i->broker);
    cJSON_AddStringToObject(s, "compress", "none");
    cJSON_AddStringToObject(s, "port", port);
    cJSON_AddStringToObject(s, "qos", "0");
    cJSON_AddStringToObject(s, "topics", i->topic);
    return s;
}

static cJSON *target_of(const em_telemetry_intent *i)
{
    char text[64];
    cJSON *t = cJSON_CreateObject();
    cJSON_AddItemToObject(t, "mqtt", settings(i));
    client_stats(i, text, sizeof(text));
    cJSON_AddStringToObject(t, "client_stats", text);
    if (i->survey) {
        survey_stats(i, text, sizeof(text));
        cJSON_AddStringToObject(t, "survey_stats", text);
    }
    return t;
}

static cJSON *target(void *ctx, const cJSON *intent, em_reason *why)
{
    (void)ctx;
    em_telemetry_intent i;
    if (!from_record(intent, &i)) {
        *why = EM_INVALID_INPUT;
        return NULL;
    }
    return target_of(&i);
}

/* -- the pod's rows -------------------------------------------------------------------- */

/* row_key: radio/report/reporting/sampling */
static void row_key(const cJSON *row, char *out, size_t n)
{
    const char *report = ovs_str(row, "report_type");
    long ri = 0, si = 0;
    const char *radio = ovs_str(row, "radio_type");
    bool has_ri = ovs_int(row, "reporting_interval", &ri), has_si = ovs_int(row, "sampling_interval", &si);
    char r[24] = "None", s[24] = "None"; /* any long */
    if (has_ri)
        EM_FORMAT_FIXED(r, sizeof(r), "%ld", ri);
    if (has_si)
        EM_FORMAT_FIXED(s, sizeof(s), "%ld", si);
    /* the pod's values: a key cut here is longer than any key compared with it, so it never matches */
    (void)em_format(out, n, "%s/%s/%s/%s", radio ? radio : "None", report && *report ? report : "raw", r, s);
}

static size_t rows_of(const cJSON *tables, const char *type, const char *radio_type, bool survey, const cJSON **out,
                      size_t max)
{
    size_t n = 0;
    const cJSON *r;
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_Stats_Config"))
    {
        const char *st = ovs_str(r, "stats_type"), *rt = ovs_str(r, "radio_type"), *sv = ovs_str(r, "survey_type");
        if (!st || strcmp(st, type) || !rt || strcmp(rt, radio_type))
            continue;
        if (survey && (!sv || strcmp(sv, "on-chan")))
            continue;
        if (n < max)
            out[n] = r;
        n++;
    }
    return n;
}

static cJSON *mqtt_settings(const cJSON *node)
{
    cJSON *m = cJSON_CreateObject();
    const char *keys[32];
    size_t n = ovs_map_keys(node, "mqtt_settings", keys, 32);
    for (size_t i = 0; i < n; i++)
        cJSON_AddStringToObject(m, keys[i], ovs_map_get(node, "mqtt_settings", keys[i]));
    return m;
}

/* TelemetryBackend._configured */
static cJSON *configured(const em_telemetry_scope *t, const cJSON *tables, const cJSON *node)
{
    cJSON *c = cJSON_CreateObject();
    cJSON_AddItemToObject(c, "mqtt", mqtt_settings(node));
    const cJSON *rows[4];
    char key[96];
    if (rows_of(tables, "client", t->intent.radio_type, false, rows, 4) == 1) {
        row_key(rows[0], key, sizeof(key));
        cJSON_AddStringToObject(c, "client_stats", key);
    } else {
        cJSON_AddNullToObject(c, "client_stats");
    }
    if (t->intent.survey) {
        if (rows_of(tables, "survey", t->intent.radio_type, true, rows, 4) == 1) {
            char base[96], survey[112];
            row_key(rows[0], base, sizeof(base));
            const char *rest = strchr(base, '/');
            (void)em_format(survey, sizeof(survey), "%s/on-chan/%s", ovs_str(rows[0], "radio_type"), rest ? rest + 1 : ""); /* shown */
            cJSON_AddStringToObject(c, "survey_stats", survey);
        } else {
            cJSON_AddNullToObject(c, "survey_stats");
        }
    }
    return c;
}

static const cJSON *bind(em_telemetry_scope *t, const char **node_uuid)
{
    const cJSON *tables = em_ovsdb_tables(t->ovs);
    const cJSON *node = em_bound_node(tables, t->serial, node_uuid);
    if (!node)
        return NULL;
    t->has_instance = em_start_instance(tables, t->instance);
    return t->has_instance ? node : NULL;
}

static bool snapshot(void *ctx, em_snapshot *out)
{
    em_telemetry_scope *t = ctx;
    memset(out, 0, sizeof(*out));
    const char *node_uuid;
    const cJSON *node = em_ovsdb_ready(t->ovs) ? bind(t, &node_uuid) : NULL;
    if (!node) {
        if (!t->has_last)
            return false;
        out->config = cJSON_Duplicate(t->last.config, true);
        out->observed = cJSON_Duplicate(t->last.observed, true);
        cJSON_ReplaceItemInObjectCaseSensitive(out->observed, "fresh", cJSON_CreateFalse());
        out->generation = t->last.generation;
        memcpy(out->schema_fingerprint, t->last.schema_fingerprint, 65);
        return true;
    }
    out->config = configured(t, em_ovsdb_tables(t->ovs), node);
    out->ready = true;
    out->generation = em_ovsdb_generation(t->ovs);
    EM_FORMAT_FIXED(out->schema_fingerprint, 65, "%s", em_ovsdb_schema_fingerprint(t->ovs)); /* SHA-256 hex */
    out->observed = em_observation(t->intent.pod_id, "telemetry", cJSON_Duplicate(out->config, true), MODE,
                                   out->generation, true, PROVENANCE, em_ovsdb_revision(t->ovs));
    em_snapshot_clear(&t->last);
    t->last.config = cJSON_Duplicate(out->config, true);
    t->last.observed = cJSON_Duplicate(out->observed, true);
    t->last.generation = out->generation;
    memcpy(t->last.schema_fingerprint, out->schema_fingerprint, 65);
    t->has_last = true;
    return true;
}

/* TelemetryBackend._check with the pod's rows */
static em_reason check(em_telemetry_scope *t, const em_telemetry_intent *i, const cJSON *node)
{
    if (strcmp(i->pod_id, t->intent.pod_id) || strcmp(i->radio_type, t->intent.radio_type))
        return EM_UNSUPPORTED_OPERATION;
    const char *current = ovs_map_get(node, "mqtt_settings", "broker");
    if (ovs_map_size(node, "mqtt_settings") && (!current || strcmp(current, i->broker)))
        return EM_OWNERSHIP_CONFLICT; /* another manager's broker is not taken over */
    const cJSON *rows[4];
    char key[96], wanted[96];
    size_t n = rows_of(em_ovsdb_tables(t->ovs), "client", i->radio_type, false, rows, 4);
    client_stats(i, wanted, sizeof(wanted));
    if (n > 1 || (n == 1 && (row_key(rows[0], key, sizeof(key)), strcmp(key, wanted))))
        return EM_OWNERSHIP_CONFLICT;
    if (i->survey) {
        n = rows_of(em_ovsdb_tables(t->ovs), "survey", i->radio_type, true, rows, 4);
        if (n == 1) {
            char base[96], survey[112];
            row_key(rows[0], base, sizeof(base));
            /* a key cut before its first '/' (the pod's radio_type) cannot match */
            const char *rest = strchr(base, '/');
            if (!rest)
                return EM_OWNERSHIP_CONFLICT;
            (void)em_format(survey, sizeof(survey), "%s/on-chan/%s", i->radio_type, rest + 1);
            survey_stats(i, wanted, sizeof(wanted));
            if (strcmp(survey, wanted))
                return EM_OWNERSHIP_CONFLICT;
        }
        if (n > 1)
            return EM_OWNERSHIP_CONFLICT;
    }
    return EM_OK;
}

static cJSON *plan(void *ctx, const cJSON *intent, em_reason *why)
{
    em_telemetry_scope *t = ctx;
    em_telemetry_intent i;
    if (!from_record(intent, &i)) {
        *why = EM_INVALID_INPUT;
        return NULL;
    }
    const char *node_uuid;
    const cJSON *node = em_ovsdb_ready(t->ovs) ? bind(t, &node_uuid) : NULL;
    if (!node) {
        *why = EM_NOT_READY;
        return NULL;
    }
    if ((*why = check(t, &i, node)) != EM_OK)
        return NULL;
    char text[300];
    cJSON *p = cJSON_CreateObject();
    cJSON_AddStringToObject(p, "mapping", MODE);
    cJSON_AddStringToObject(p, "action", "statistics-publishing");
    EM_FORMAT_FIXED(text, sizeof(text), "%s:%d", i.broker, i.port); /* validated: 253 and 5 */
    cJSON_AddStringToObject(p, "broker", text);
    cJSON_AddStringToObject(p, "topic", i.topic);
    client_stats(&i, text, sizeof(text));
    cJSON_AddStringToObject(p, "client_stats", text);
    cJSON_AddStringToObject(p, "instance", t->instance);
    const char *fields[] = {"AWLAN_Node.mqtt_settings", "Wifi_Stats_Config"};
    cJSON_AddItemToObject(p, "fields", em_ovs_strings(fields, 2));
    cJSON_AddStringToObject(p, "guard", "pod serial and its current mqtt_settings");
    return p;
}

static cJSON *ovs_map(const cJSON *object)
{
    cJSON *pairs = cJSON_CreateArray(), *map = cJSON_CreateArray();
    const cJSON *m;
    cJSON_ArrayForEach(m, object)
    {
        cJSON *pair = cJSON_CreateArray();
        cJSON_AddItemToArray(pair, cJSON_CreateString(m->string));
        cJSON_AddItemToArray(pair, cJSON_CreateString(m->valuestring));
        cJSON_AddItemToArray(pairs, pair);
    }
    cJSON_AddItemToArray(map, cJSON_CreateString("map"));
    cJSON_AddItemToArray(map, pairs);
    return map;
}

static cJSON *stats_row(const em_telemetry_intent *i, bool survey)
{
    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "stats_type", survey ? "survey" : "client");
    cJSON_AddStringToObject(r, "radio_type", i->radio_type);
    if (survey)
        cJSON_AddStringToObject(r, "survey_type", "on-chan");
    cJSON_AddStringToObject(r, "report_type", "raw");
    cJSON_AddNumberToObject(r, "reporting_interval", i->reporting_interval);
    cJSON_AddNumberToObject(r, "sampling_interval", i->sampling_interval);
    return r;
}

static void submit(void *ctx, const cJSON *intent, const cJSON *attempt, em_submit_out *out)
{
    em_telemetry_scope *t = ctx;
    memset(out, 0, sizeof(*out));
    em_telemetry_intent i;
    em_reason why;
    cJSON *p = plan(ctx, intent, &why);
    if (!p || !from_record(intent, &i)) {
        cJSON_Delete(p);
        strcpy(out->status, "unknown"); /* the plan raised at submission */
        out->reason = EM_OUTCOME_UNKNOWN;
        return;
    }
    cJSON_Delete(p);
    int generation = (int)cJSON_GetNumberValue(cJSON_GetObjectItemCaseSensitive(attempt, "session_generation"));
    const char *node_uuid;
    const cJSON *node = bind(t, &node_uuid);
    if (!node || em_ovsdb_generation(t->ovs) != generation) {
        strcpy(out->status, "rejected");
        out->reason = EM_NOT_READY;
        return;
    }
    cJSON *ops = cJSON_CreateArray();
    int counts[4];
    size_t n = 0;
    /* wait: the node's serial and its current mqtt_settings */
    cJSON *wait = em_ovs_op("wait", "AWLAN_Node", em_ovs_where_uuid(node_uuid));
    const char *cols[] = {"serial_number", "mqtt_settings"};
    cJSON_AddItemToObject(wait, "columns", em_ovs_strings(cols, 2));
    cJSON_AddStringToObject(wait, "until", "==");
    cJSON *rows = cJSON_AddArrayToObject(wait, "rows"), *row = cJSON_CreateObject();
    cJSON_AddStringToObject(row, "serial_number", t->serial);
    cJSON *current = mqtt_settings(node); /* sorted keys (ovs_map_keys) */
    cJSON_AddItemToObject(row, "mqtt_settings", ovs_map(current));
    cJSON_Delete(current);
    cJSON_AddItemToArray(rows, row);
    cJSON_AddNumberToObject(wait, "timeout", 0);
    cJSON_AddItemToArray(ops, wait);
    counts[n++] = -1;
    cJSON *update = em_ovs_op("update", "AWLAN_Node", em_ovs_where_uuid(node_uuid)), *urow = cJSON_CreateObject();
    cJSON *wanted = settings(&i);
    cJSON_AddItemToObject(urow, "mqtt_settings", ovs_map(wanted));
    cJSON_Delete(wanted);
    cJSON_AddItemToObject(update, "row", urow);
    cJSON_AddItemToArray(ops, update);
    counts[n++] = 1;
    const cJSON *found[4];
    if (!rows_of(em_ovsdb_tables(t->ovs), "client", i.radio_type, false, found, 4)) {
        cJSON *insert = em_ovs_op("insert", "Wifi_Stats_Config", NULL);
        cJSON_AddItemToObject(insert, "row", stats_row(&i, false));
        cJSON_AddItemToArray(ops, insert);
        counts[n++] = -1;
    }
    if (i.survey && !rows_of(em_ovsdb_tables(t->ovs), "survey", i.radio_type, true, found, 4)) {
        cJSON *insert = em_ovs_op("insert", "Wifi_Stats_Config", NULL);
        cJSON_AddItemToObject(insert, "row", stats_row(&i, true));
        cJSON_AddItemToArray(ops, insert);
        counts[n++] = -1;
    }
    cJSON *results = t->transact(t->transact_ctx, ops);
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
    cJSON_AddStringToObject(out->evidence, "action", "statistics-publishing");
    cJSON_AddStringToObject(out->evidence, "instance", t->instance);
    cJSON_AddItemToObject(out->evidence, "results", results);
}

/* -- TelemetrySetup ---------------------------------------------------------------------- */

em_reason em_telemetry_open(em_telemetry_scope *t, const char *state_dir, const em_journal_schemas *schemas,
                            const em_vault *vault, double (*monotonic)(void))
{
    char dir[600];
    EM_FORMAT_FIXED(dir, sizeof(dir), "%s/telemetry", state_dir); /* EM_STATE_DIR_MAX */
    em_reason why;
    t->journal = em_journal_open(dir, schemas, &why);
    if (!t->journal)
        return why;
    em_engine_init(&t->engine, t->journal, vault, t->intent.pod_id,
                   (em_backend){MODE, target, snapshot, plan, submit}, t, monotonic);
    em_engine_recover(&t->engine);
    return EM_OK;
}

void em_telemetry_close(em_telemetry_scope *t)
{
    em_engine_free(&t->engine);
    em_journal_close(t->journal);
    em_snapshot_clear(&t->last);
}

static void wait_for(em_telemetry_scope *t, const char *why)
{
    t->has_waiting = why != NULL;
    (void)em_copy(t->waiting, sizeof(t->waiting), why ? why : ""); /* a message */
}

/* the key: one write per OpenSync start and requested configuration */
static void key_of(em_telemetry_scope *t, char *out, size_t n)
{
    char cs[64], ss[64];
    client_stats(&t->intent, cs, sizeof(cs));
    /* at most 16 + 35 + 128 + 14 + 51 characters of a validated intent: n is 400 */
    EM_FORMAT_FIXED(out, n, "%s:%s:%s", t->instance, cs, t->intent.topic);
    size_t off = strlen(out);
    if (t->intent.publish_interval) {
        EM_FORMAT_FIXED(out + off, n - off, ":publish=%d", t->intent.publish_interval);
        off += strlen(out + off);
    }
    if (t->intent.survey) {
        survey_stats(&t->intent, ss, sizeof(ss));
        EM_FORMAT_FIXED(out + off, n - off, ":survey=%s", ss);
    }
}

static void settle(em_telemetry_scope *t, cJSON *op, const em_snapshot *snap, bool have_snap)
{
    const char *state = em_state_of(op);
    if (strcmp(state, "CONFIG_COMMITTED") && strcmp(state, "INDETERMINATE"))
        return;
    em_reason why;
    cJSON *target = t->engine.backend.target(t, cJSON_GetObjectItemCaseSensitive(op, "intent"), &why);
    const char *planned = str(cJSON_GetObjectItemCaseSensitive(op, "plan"), "instance");
    bool same_start = have_snap && t->has_instance && planned && !strcmp(planned, t->instance);
    if (target && same_start && snap->ready && em_snapshot_satisfies(snap, target) && em_matches(snap->config, target)) {
        cJSON *evidence = em_engine_evidence(snap, false, t->instance);
        if (!strcmp(state, "INDETERMINATE"))
            cJSON_AddStringToObject(evidence, "attribution", "current_condition_only");
        if (em_transition(op, "OBSERVED_APPLIED", evidence)) {
            cJSON_ReplaceItemInObjectCaseSensitive(op, "application_evidence", evidence);
            em_set_reason(op, NULL);
            em_engine_save(&t->engine, op, NULL);
            em_log(EM_LOG_INFO, "emosa.agent.telemetry", "statistics publishing configured on the pod (%s)",
                    t->intent.topic);
        } else {
            cJSON_Delete(evidence);
        }
    } else if (em_engine_expired(&t->engine, op)) {
        cJSON_ReplaceItemInObjectCaseSensitive(op, "original_outcome", cJSON_CreateString(state));
        cJSON_ReplaceItemInObjectCaseSensitive(op, "deadline_elapsed", cJSON_CreateTrue());
        em_transition(op, "TIMED_OUT", NULL);
        em_set_reason(op, "APPLY_TIMEOUT");
        em_engine_save(&t->engine, op, NULL);
    }
    cJSON_Delete(target);
}

void em_telemetry_tick(em_telemetry_scope *t)
{
    em_snapshot snap = {0};
    bool have = t->engine.backend.snapshot(t, &snap);
    cJSON *op = em_journal_latest(t->journal);
    if (op)
        settle(t, op, &snap, have);
    cJSON_Delete(op);
    if (!have) {
        em_snapshot_clear(&snap);
        return;
    }
    /* TelemetrySetup._wanted */
    op = em_journal_latest(t->journal);
    cJSON *owned = em_journal_ownership(t->journal, t->intent.pod_id);
    char key[400];
    key_of(t, key, sizeof(key));
    cJSON *done = NULL;
    bool wanted = false;
    if (!snap.ready) {
        wait_for(t, "pod not bound");
    } else if (owned) {
        wait_for(t, "another manager changed the configuration: admit the pod anew");
    } else if (op && em_state_active(em_state_of(op))) {
        wait_for(t, "write in progress");
    } else if ((done = em_journal_lookup(t->journal, SOURCE, t->intent.pod_id, key))) {
        if (!strcmp(em_state_of(done), "OBSERVED_APPLIED")) {
            wait_for(t, NULL);
        } else {
            char text[128];
            const char *reason = str(done, "reason");
            (void)em_format(text, sizeof(text), "not configured on this start: %s%s%s", em_state_of(done), /* a message */
                     reason ? " " : "", reason ? reason : "");
            wait_for(t, text);
        }
    } else {
        wait_for(t, NULL);
        wanted = true;
    }
    cJSON_Delete(done);
    cJSON_Delete(owned);
    cJSON_Delete(op);
    em_snapshot_clear(&snap);
    if (!wanted)
        return;
    cJSON *intent = record(&t->intent);
    em_reason why;
    op = em_engine_request(&t->engine, intent, SOURCE, key, t->run_id, DEADLINE, "semantic", NULL, &why);
    cJSON_Delete(intent);
    if (op && !strcmp(em_state_of(op), "REQUESTED")) {
        cJSON *result = em_engine_execute(&t->engine,
                                          cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(op, "operation_id")));
        const char *reason = str(result, "reason");
        em_log(EM_LOG_INFO, "emosa.agent.telemetry", "statistics publishing %s: %s %s", str(result, "operation_id"),
                em_state_of(result), reason ? reason : "");
        cJSON_Delete(result);
    }
    cJSON_Delete(op);
}

cJSON *em_telemetry_status(em_telemetry_scope *t)
{
    cJSON *o = cJSON_CreateObject(), *op = em_journal_latest(t->journal);
    cJSON_AddItemToObject(o, "waiting", t->has_waiting ? cJSON_CreateString(t->waiting) : cJSON_CreateNull());
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

em_backend em_telemetry_backend(void) { return (em_backend){MODE, target, snapshot, plan, submit}; }
