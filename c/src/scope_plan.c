/* SPDX-License-Identifier: Apache-2.0 */
/* The radio plan scope (emosa.opensync.radio_plan, emosa.agent.radio_plan; spec 3.10). */
#include "scope_plan.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "canon.h"
#include "log.h"
#include "ovs.h"
#include "scope.h"

#define MODE "opensync-2.0-radio-plan"
#define PROVENANCE "opensync-wm:Wifi_VIF_State+Wifi_Radio_Config"
#define SOURCE "radio-plan-policy"
#define DEADLINE 30
#define ROWS 16 /* the unclaimed APs and their Inet rows the scope reads, at most */
#define STATS 64 /* the statistics rows removed in one write, at most */
#define NEUTRAL_REF "emosa-unclaimed" /* RadioPlanIntent's default neutral_ref */

/* radio_plan.CLOUD: AWLAN_Node's columns that point qm at a broker, the operator's cloud on a seed */
static const char *const CLOUD[] = {"mqtt_headers", "mqtt_settings", "mqtt_topics"};
#define NCLOUD 3

static const char *str(const cJSON *o, const char *k)
{
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, k));
}

static int by_name(const void *a, const void *b) { return strcmp((const char *)a, (const char *)b); }

/* RadioPlanIntent.validate */
static em_reason validate(const em_plan_intent *i)
{
    if (!*i->pod_id || !i->nclaimed || !*i->band || !*i->ht_mode || !*i->neutral_ref)
        return EM_INVALID_INPUT;
    for (size_t k = 0; k < i->nclaimed; k++)
        if (!*i->claimed[k])
            return EM_INVALID_INPUT;
    return EM_OK;
}

/* adds a name to the claimed set, once; false when it does not fit */
static bool claim(em_plan_intent *i, const char *name)
{
    if (!name)
        return true;
    for (size_t k = 0; k < i->nclaimed; k++)
        if (!strcmp(i->claimed[k], name))
            return true;
    return i->nclaimed < EM_PLAN_CLAIMED && em_copy(i->claimed[i->nclaimed++], 16, name);
}

bool em_plan_intent_from(const char *pod_id, const em_profile *profile, em_plan_intent *out, em_reason *why)
{
    memset(out, 0, sizeof(*out));
    *why = EM_INVALID_INPUT;
    if (!em_copy(out->pod_id, sizeof(out->pod_id), pod_id) || !claim(out, profile->fronthaul_if))
        return false;
    for (size_t k = 0; k < profile->nslots; k++)
        if (!claim(out, profile->slots[k].if_name))
            return false;
    qsort(out->claimed, out->nclaimed, sizeof(out->claimed[0]), by_name);
    if (!profile->band || !profile->ht_mode || !em_copy(out->band, sizeof(out->band), profile->band) ||
        !em_copy(out->ht_mode, sizeof(out->ht_mode), profile->ht_mode))
        return false;
    out->channel = profile->channel;
    EM_FORMAT_FIXED(out->neutral_ref, sizeof(out->neutral_ref), "%s", NEUTRAL_REF);
    *why = validate(out);
    return *why == EM_OK;
}

/* RadioPlanIntent.record() */
static cJSON *record(const em_plan_intent *i)
{
    cJSON *r = cJSON_CreateObject(), *claimed = cJSON_CreateArray();
    cJSON_AddStringToObject(r, "pod_id", i->pod_id);
    for (size_t k = 0; k < i->nclaimed; k++)
        cJSON_AddItemToArray(claimed, cJSON_CreateString(i->claimed[k]));
    cJSON_AddItemToObject(r, "claimed", claimed);
    cJSON_AddStringToObject(r, "band", i->band);
    cJSON_AddNumberToObject(r, "channel", i->channel);
    cJSON_AddStringToObject(r, "ht_mode", i->ht_mode);
    cJSON_AddStringToObject(r, "neutral_ref", i->neutral_ref);
    return r;
}

static bool from_record(const cJSON *r, em_plan_intent *i)
{
    memset(i, 0, sizeof(*i));
    const cJSON *claimed = cJSON_GetObjectItemCaseSensitive(r, "claimed"), *name;
    const cJSON *channel = cJSON_GetObjectItemCaseSensitive(r, "channel");
    if (!str(r, "pod_id") || !cJSON_IsArray(claimed) || !str(r, "band") || !str(r, "ht_mode") ||
        !cJSON_IsNumber(channel))
        return false;
    if (!em_copy(i->pod_id, sizeof(i->pod_id), str(r, "pod_id")) ||
        !em_copy(i->band, sizeof(i->band), str(r, "band")) ||
        !em_copy(i->ht_mode, sizeof(i->ht_mode), str(r, "ht_mode")))
        return false;
    cJSON_ArrayForEach(name, claimed)
    {
        if (!cJSON_IsString(name) || i->nclaimed >= EM_PLAN_CLAIMED ||
            !em_copy(i->claimed[i->nclaimed++], 16, name->valuestring))
            return false;
    }
    i->channel = channel->valueint;
    /* absent in records made before it: the default */
    const char *ref = str(r, "neutral_ref") ? str(r, "neutral_ref") : NEUTRAL_REF;
    if (!em_copy(i->neutral_ref, sizeof(i->neutral_ref), ref))
        return false;
    return validate(i) == EM_OK;
}

/* RadioPlanIntent.target */
static cJSON *target(void *ctx, const cJSON *intent, em_reason *why)
{
    (void)ctx;
    em_plan_intent i;
    if (!from_record(intent, &i)) {
        *why = EM_INVALID_INPUT;
        return NULL;
    }
    cJSON *t = cJSON_CreateObject();
    cJSON_AddArrayToObject(t, "unclaimed_inet_on");
    cJSON_AddArrayToObject(t, "unclaimed_exposed");
    cJSON_AddArrayToObject(t, "operator_cloud");
    cJSON_AddTrueToObject(t, "radio_planned");
    return t;
}

/* -- the pod's rows -------------------------------------------------------------------- */

typedef struct {
    char name[16];
    const char *uuid;
} named;

typedef struct {
    size_t nexposed, ninets, nall;
    named exposed[ROWS]; /* to write: the unclaimed APs not neutral (hidden, in no bridge, EMOSA's key) */
    named inets[ROWS];   /* to write: the unclaimed APs' Inet rows on (the APs themselves left on) */
    char all[ROWS][16]; /* every unclaimed AP */
    const char *radio_uuid;        /* to write: the wired pod's radio, off the profile's channel */
    const cJSON *radio;
    const cJSON *node; /* to write: the pod's node, an MQTT setting of its operator's on it */
    size_t nstats;
    const cJSON *stats[STATS]; /* to remove: the statistics rows (each row's uuid its key) */
} plan_changes;

static int named_order(const void *a, const void *b) { return strcmp(((const named *)a)->name, ((const named *)b)->name); }

static int uuid_order(const void *a, const void *b)
{
    return strcmp((*(const cJSON *const *)a)->string, (*(const cJSON *const *)b)->string);
}

/* the node's MQTT settings, headers or topics not empty: the cloud it points qm at */
static bool cloud_column(const cJSON *node, size_t k) { return node && ovs_map_size(node, CLOUD[k]) > 0; }

static bool claimed(const em_plan_intent *i, const char *name)
{
    for (size_t k = 0; k < i->nclaimed; k++)
        if (!strcmp(i->claimed[k], name))
            return true;
    return false;
}

static bool unclaimed_ap(const plan_changes *c, const char *name)
{
    for (size_t k = 0; k < c->nall; k++)
        if (!strcmp(c->all[k], name))
            return true;
    return false;
}

/* RadioPlanBackend._changes: what the plan writes now, and the configured view. An AP is never
 * disabled: the 2.4.3 pods' vendor healthcheck reboots a pod whose AP stops beaconing (spec 3.10) */
/* EMOSA's own passphrase for the unclaimed APs (SecretStore.generated), NULL when the vault makes
 * none; freed with em_free_secret */
static char *neutral_key(const em_plan_scope *s, const em_plan_intent *i)
{
    em_reason why;
    return s->vault ? em_vault_generated(s->vault, i->neutral_ref, &why) : NULL;
}

/* a legacy pod's WPA2-PSK: the security map ["map", [["encryption","WPA-PSK"],["key",psk]]] */
static cJSON *security_map(const char *psk)
{
    cJSON *map = cJSON_CreateArray(), *pairs = cJSON_CreateArray(), *enc = cJSON_CreateArray(),
          *pair = cJSON_CreateArray();
    cJSON_AddItemToArray(enc, cJSON_CreateString("encryption"));
    cJSON_AddItemToArray(enc, cJSON_CreateString("WPA-PSK"));
    cJSON_AddItemToArray(pair, cJSON_CreateString("key"));
    cJSON_AddItemToArray(pair, cJSON_CreateString(psk));
    cJSON_AddItemToArray(pairs, enc);
    cJSON_AddItemToArray(pairs, pair);
    cJSON_AddItemToArray(map, cJSON_CreateString("map"));
    cJSON_AddItemToArray(map, pairs);
    return map;
}

/* radio_plan.neutral: the AP row already hidden, in no bridge, on `key` (WPA2-PSK in the security
 * map); never with no key */
static bool neutral(const cJSON *row, const char *key)
{
    const char *broadcast = ovs_str(row, "ssid_broadcast"), *bridge = ovs_str(row, "bridge");
    const char *encryption = ovs_map_get(row, "security", "encryption");
    const char *psk = ovs_map_get(row, "security", "key");
    return key && broadcast && !strcmp(broadcast, "disabled") && (!bridge || !*bridge) && encryption &&
           !strcmp(encryption, "WPA-PSK") && psk && !strcmp(psk, key);
}

/* `key`: EMOSA's own passphrase for the unclaimed APs, NULL when the vault has none (each then
 * counts as exposed) */
static cJSON *changes(const em_plan_intent *i, const cJSON *tables, const char *key, plan_changes *c)
{
    memset(c, 0, sizeof(*c));
    const cJSON *r;
    char on[ROWS * 2][16]; /* unclaimed APs on in Config or State (left on) */
    size_t non = 0;
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_VIF_Config"))
    {
        const char *mode = ovs_str(r, "mode"), *name = ovs_str(r, "if_name");
        if (!mode || strcmp(mode, "ap") || !name || claimed(i, name) || c->nall >= ROWS)
            continue;
        (void)em_copy(c->all[c->nall++], 16, name); /* an interface name (15) */
        if (ovs_true(r, "enabled") && non < ROWS * 2)
            (void)em_copy(on[non++], 16, name);
        if (!neutral(r, key) && c->nexposed < ROWS) {
            (void)em_copy(c->exposed[c->nexposed].name, 16, name);
            c->exposed[c->nexposed++].uuid = r->string;
        }
    }
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_VIF_State"))
    {
        const char *name = ovs_str(r, "if_name");
        if (name && unclaimed_ap(c, name) && ovs_true(r, "enabled") && non < ROWS * 2)
            (void)em_copy(on[non++], 16, name);
    }
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_Inet_Config"))
    {
        const char *name = ovs_str(r, "if_name");
        if (name && unclaimed_ap(c, name) && ovs_true(r, "enabled") && c->ninets < ROWS) {
            (void)em_copy(c->inets[c->ninets].name, 16, name);
            c->inets[c->ninets++].uuid = r->string;
        }
    }
    qsort(c->exposed, c->nexposed, sizeof(c->exposed[0]), named_order);
    qsort(c->inets, c->ninets, sizeof(c->inets[0]), named_order);
    qsort(c->all, c->nall, sizeof(c->all[0]), by_name);
    qsort(on, non, sizeof(on[0]), by_name);
    /* the radio of the profile's band, when exactly one */
    size_t radios = 0;
    const cJSON *radio = NULL;
    const char *radio_uuid = NULL;
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_Radio_Config"))
    {
        const char *band = ovs_str(r, "freq_band");
        if (band && !strcmp(band, i->band)) {
            radios++;
            radio = r;
            radio_uuid = r->string;
        }
    }
    if (radios != 1)
        radio = NULL;
    /* wired: exactly one uplink in use, an Ethernet port */
    size_t used = 0;
    bool eth = false;
    cJSON_ArrayForEach(r, em_table(tables, "Connection_Manager_Uplink"))
    {
        if (ovs_true(r, "is_used")) {
            used++;
            const char *type = ovs_str(r, "if_type");
            eth = type && !strcmp(type, "eth");
        }
    }
    bool wired = used == 1 && eth;
    long channel = 0;
    bool has_channel = radio && ovs_int(radio, "channel", &channel);
    const char *ht_mode = radio ? ovs_str(radio, "ht_mode") : NULL;
    bool on_plan = has_channel && channel == i->channel && ht_mode && !strcmp(ht_mode, i->ht_mode);
    if (wired && radio && !on_plan) {
        c->radio = radio;
        c->radio_uuid = radio_uuid;
    }
    /* the operator's cloud: the node's MQTT columns, the statistics rows */
    const cJSON *node = cJSON_GetArrayItem(em_table(tables, "AWLAN_Node"), 0);
    bool cloud = false;
    for (size_t k = 0; k < NCLOUD; k++)
        cloud = cloud || cloud_column(node, k);
    c->node = cloud ? node : NULL;
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_Stats_Config"))
    {
        if (c->nstats < STATS)
            c->stats[c->nstats++] = r;
    }
    qsort(c->stats, c->nstats, sizeof(c->stats[0]), uuid_order);
    cJSON *v = cJSON_CreateObject(), *list = cJSON_AddArrayToObject(v, "unclaimed");
    for (size_t k = 0; k < c->nall; k++)
        cJSON_AddItemToArray(list, cJSON_CreateString(c->all[k]));
    list = cJSON_AddArrayToObject(v, "unclaimed_enabled");
    for (size_t k = 0; k < non; k++)
        if (k == 0 || strcmp(on[k], on[k - 1])) /* sorted: each name once */
            cJSON_AddItemToArray(list, cJSON_CreateString(on[k]));
    list = cJSON_AddArrayToObject(v, "unclaimed_inet_on");
    for (size_t k = 0; k < c->ninets; k++)
        cJSON_AddItemToArray(list, cJSON_CreateString(c->inets[k].name));
    list = cJSON_AddArrayToObject(v, "unclaimed_exposed");
    for (size_t k = 0; k < c->nexposed; k++)
        cJSON_AddItemToArray(list, cJSON_CreateString(c->exposed[k].name));
    list = cJSON_AddArrayToObject(v, "operator_cloud");
    for (size_t k = 0; k < NCLOUD; k++)
        if (cloud_column(node, k))
            cJSON_AddItemToArray(list, cJSON_CreateString(CLOUD[k]));
    if (c->nstats)
        cJSON_AddItemToArray(list, cJSON_CreateString("Wifi_Stats_Config"));
    cJSON_AddItemToObject(v, "channel", has_channel ? cJSON_CreateNumber((double)channel) : cJSON_CreateNull());
    cJSON_AddItemToObject(v, "ht_mode", ht_mode ? cJSON_CreateString(ht_mode) : cJSON_CreateNull());
    cJSON_AddBoolToObject(v, "wired", wired);
    cJSON_AddBoolToObject(v, "radio_planned", !wired || on_plan);
    cJSON_AddBoolToObject(v, "pending", c->nexposed || c->ninets || c->radio || c->node || c->nstats);
    return v;
}

static const cJSON *bind(em_plan_scope *s, const char **node_uuid)
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
    em_plan_scope *s = ctx;
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
    plan_changes c;
    char *key = neutral_key(s, &s->intent);
    out->config = changes(&s->intent, em_ovsdb_tables(s->ovs), key, &c);
    em_free_secret(key);
    out->ready = true;
    out->generation = em_ovsdb_generation(s->ovs);
    EM_FORMAT_FIXED(out->schema_fingerprint, 65, "%s", em_ovsdb_schema_fingerprint(s->ovs)); /* SHA-256 hex */
    out->observed = em_observation(s->intent.pod_id, "radio-plan", cJSON_Duplicate(out->config, true), MODE,
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
    em_plan_scope *s = ctx;
    em_plan_intent i;
    if (!from_record(intent, &i)) {
        *why = EM_INVALID_INPUT;
        return NULL;
    }
    if (strcmp(i.pod_id, s->intent.pod_id)) {
        *why = EM_UNSUPPORTED_OPERATION;
        return NULL;
    }
    const char *node_uuid;
    const cJSON *node = em_ovsdb_ready(s->ovs) ? bind(s, &node_uuid) : NULL;
    if (!node) {
        *why = EM_NOT_READY;
        return NULL;
    }
    plan_changes c;
    char *key = neutral_key(s, &i);
    cJSON *view = changes(&i, em_ovsdb_tables(s->ovs), key, &c);
    em_free_secret(key);
    cJSON *p = cJSON_CreateObject();
    cJSON_AddStringToObject(p, "mapping", MODE);
    cJSON_AddStringToObject(p, "action", "radio-plan");
    cJSON *list = cJSON_AddArrayToObject(p, "neutralize");
    for (size_t k = 0; k < c.nexposed; k++)
        cJSON_AddItemToArray(list, cJSON_CreateString(c.exposed[k].name));
    list = cJSON_AddArrayToObject(p, "inet_off");
    for (size_t k = 0; k < c.ninets; k++)
        cJSON_AddItemToArray(list, cJSON_CreateString(c.inets[k].name));
    cJSON_AddItemToObject(p, "channel", c.radio ? cJSON_CreateNumber(i.channel) : cJSON_CreateNull());
    cJSON_AddItemToObject(p, "cloud_cleared", cJSON_DetachItemFromObjectCaseSensitive(view, "operator_cloud"));
    cJSON_Delete(view);
    cJSON_AddStringToObject(p, "instance", s->instance);
    const char *fields[] = {"Wifi_VIF_Config.security",  "Wifi_VIF_Config.ssid_broadcast", "Wifi_VIF_Config.bridge",
                            "Wifi_Inet_Config.enabled",  "Wifi_Radio_Config.channel",      "Wifi_Radio_Config.ht_mode",
                            "AWLAN_Node.mqtt_headers",   "AWLAN_Node.mqtt_settings",       "AWLAN_Node.mqtt_topics",
                            "Wifi_Stats_Config"};
    cJSON_AddItemToObject(p, "fields", em_ovs_strings(fields, 10));
    cJSON_AddStringToObject(p, "guard", "pod serial and each changed row as it is");
    return p;
}

/* a wait on one row as it is: {column: value} rows of one */
static cJSON *wait_row(const char *table, const char *uuid, const char *const *columns, size_t n, cJSON *row)
{
    cJSON *wait = em_ovs_op("wait", table, em_ovs_where_uuid(uuid));
    cJSON_AddItemToObject(wait, "columns", em_ovs_strings(columns, n));
    cJSON_AddStringToObject(wait, "until", "==");
    cJSON_AddItemToArray(cJSON_AddArrayToObject(wait, "rows"), row);
    cJSON_AddNumberToObject(wait, "timeout", 0);
    return wait;
}

static cJSON *update_row(const char *table, const char *uuid, cJSON *row)
{
    cJSON *update = em_ovs_op("update", table, em_ovs_where_uuid(uuid));
    cJSON_AddItemToObject(update, "row", row);
    return update;
}

/* an OVSDB optional in a wait row: the value, or the empty set */
static cJSON *empty_set(void)
{
    cJSON *set = cJSON_CreateArray();
    cJSON_AddItemToArray(set, cJSON_CreateString("set"));
    cJSON_AddItemToArray(set, cJSON_CreateArray());
    return set;
}

/* OVSDB's empty map, ["map", []] */
static cJSON *empty_map(void)
{
    cJSON *map = cJSON_CreateArray();
    cJSON_AddItemToArray(map, cJSON_CreateString("map"));
    cJSON_AddItemToArray(map, cJSON_CreateArray());
    return map;
}

static void submit(void *ctx, const cJSON *intent, const cJSON *attempt, em_submit_out *out)
{
    em_plan_scope *s = ctx;
    memset(out, 0, sizeof(*out));
    em_plan_intent i;
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
    const char *node_uuid;
    const cJSON *node = bind(s, &node_uuid);
    plan_changes c = {0};
    char *key = neutral_key(s, &i);
    if (node)
        cJSON_Delete(changes(&i, em_ovsdb_tables(s->ovs), key, &c));
    if (!node || em_ovsdb_generation(s->ovs) != generation ||
        !(c.nexposed || c.ninets || c.radio || c.node || c.nstats) || (c.nexposed && !key)) {
        em_free_secret(key);
        strcpy(out->status, "rejected"); /* not bound, another session, nothing differs, or no key */
        out->reason = EM_NOT_READY;
        return;
    }
    cJSON *ops = cJSON_CreateArray(), *row;
    int counts[1 + 2 * (ROWS + ROWS + 1 + 1 + STATS)];
    size_t n = 0;
    const char *node_cols[] = {"serial_number"};
    row = cJSON_CreateObject();
    cJSON_AddStringToObject(row, "serial_number", s->serial);
    cJSON_AddItemToArray(ops, wait_row("AWLAN_Node", node_uuid, node_cols, 1, row));
    counts[n++] = -1;
    const char *vif_cols[] = {"if_name", "mode"};
    for (size_t k = 0; k < c.nexposed; k++) { /* radio_plan.neutral_row: still enabled, so it beacons */
        row = cJSON_CreateObject();
        cJSON_AddStringToObject(row, "if_name", c.exposed[k].name);
        cJSON_AddStringToObject(row, "mode", "ap");
        cJSON_AddItemToArray(ops, wait_row("Wifi_VIF_Config", c.exposed[k].uuid, vif_cols, 2, row));
        row = cJSON_CreateObject();
        cJSON_AddItemToObject(row, "security", security_map(key));
        cJSON_AddStringToObject(row, "ssid_broadcast", "disabled");
        cJSON_AddStringToObject(row, "bridge", "");
        cJSON_AddItemToArray(ops, update_row("Wifi_VIF_Config", c.exposed[k].uuid, row));
        counts[n++] = -1;
        counts[n++] = 1;
    }
    em_free_secret(key);
    const char *inet_cols[] = {"if_name", "enabled"};
    for (size_t k = 0; k < c.ninets; k++) {
        row = cJSON_CreateObject();
        cJSON_AddStringToObject(row, "if_name", c.inets[k].name);
        cJSON_AddTrueToObject(row, "enabled");
        cJSON_AddItemToArray(ops, wait_row("Wifi_Inet_Config", c.inets[k].uuid, inet_cols, 2, row));
        row = cJSON_CreateObject();
        cJSON_AddFalseToObject(row, "enabled");
        cJSON_AddItemToArray(ops, update_row("Wifi_Inet_Config", c.inets[k].uuid, row));
        counts[n++] = -1;
        counts[n++] = 1;
    }
    if (c.radio) {
        const char *radio_cols[] = {"if_name", "channel", "ht_mode"}, *name = ovs_str(c.radio, "if_name");
        const char *ht_mode = ovs_str(c.radio, "ht_mode");
        long channel;
        row = cJSON_CreateObject();
        cJSON_AddItemToObject(row, "if_name", name ? cJSON_CreateString(name) : empty_set());
        cJSON_AddItemToObject(row, "channel",
                              ovs_int(c.radio, "channel", &channel) ? cJSON_CreateNumber((double)channel) : empty_set());
        cJSON_AddItemToObject(row, "ht_mode", ht_mode ? cJSON_CreateString(ht_mode) : empty_set());
        cJSON_AddItemToArray(ops, wait_row("Wifi_Radio_Config", c.radio_uuid, radio_cols, 3, row));
        row = cJSON_CreateObject();
        cJSON_AddNumberToObject(row, "channel", i.channel);
        cJSON_AddStringToObject(row, "ht_mode", i.ht_mode);
        cJSON_AddItemToArray(ops, update_row("Wifi_Radio_Config", c.radio_uuid, row));
        counts[n++] = -1;
        counts[n++] = 1;
    }
    if (c.node) { /* qm's broker, headers and topics emptied: no cloud but EMOSA */
        row = cJSON_CreateObject();
        cJSON *empty = cJSON_CreateObject();
        for (size_t k = 0; k < NCLOUD; k++) {
            const cJSON *v = cJSON_GetObjectItemCaseSensitive(c.node, CLOUD[k]);
            cJSON_AddItemToObject(row, CLOUD[k], v ? cJSON_Duplicate(v, 1) : empty_map());
            cJSON_AddItemToObject(empty, CLOUD[k], empty_map());
        }
        cJSON_AddItemToArray(ops, wait_row("AWLAN_Node", node_uuid, CLOUD, NCLOUD, row));
        cJSON_AddItemToArray(ops, update_row("AWLAN_Node", node_uuid, empty));
        counts[n++] = -1;
        counts[n++] = 1;
    }
    const char *stats_cols[] = {"stats_type", "radio_type"};
    for (size_t k = 0; k < c.nstats; k++) { /* each statistics row as it is, removed */
        const char *type = ovs_str(c.stats[k], "stats_type"), *radio_type = ovs_str(c.stats[k], "radio_type");
        row = cJSON_CreateObject();
        cJSON_AddItemToObject(row, "stats_type", type ? cJSON_CreateString(type) : empty_set());
        cJSON_AddItemToObject(row, "radio_type", radio_type ? cJSON_CreateString(radio_type) : empty_set());
        cJSON_AddItemToArray(ops, wait_row("Wifi_Stats_Config", c.stats[k]->string, stats_cols, 2, row));
        cJSON_AddItemToArray(ops, em_ovs_op("delete", "Wifi_Stats_Config", em_ovs_where_uuid(c.stats[k]->string)));
        counts[n++] = -1;
        counts[n++] = 1;
    }
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
    cJSON_AddStringToObject(out->evidence, "action", "radio-plan");
    cJSON_AddStringToObject(out->evidence, "instance", s->instance);
    cJSON_AddItemToObject(out->evidence, "results", results);
}

/* -- RadioPlan ---------------------------------------------------------------------------- */

em_reason em_plan_open(em_plan_scope *s, const char *state_dir, const em_journal_schemas *schemas,
                       const em_vault *vault, double (*monotonic)(void))
{
    char dir[600];
    EM_FORMAT_FIXED(dir, sizeof(dir), "%s/radio-plan", state_dir); /* EM_STATE_DIR_MAX */
    s->vault = vault;
    em_reason why;
    s->journal = em_journal_open(dir, schemas, &why);
    if (!s->journal)
        return why;
    em_engine_init(&s->engine, s->journal, vault, s->intent.pod_id, (em_backend){MODE, target, snapshot, plan, submit},
                   s, monotonic);
    em_engine_recover(&s->engine);
    return EM_OK;
}

void em_plan_close(em_plan_scope *s)
{
    em_engine_free(&s->engine);
    em_journal_close(s->journal);
    em_snapshot_clear(&s->last);
}

static void wait_for(em_plan_scope *s, const char *why)
{
    s->has_waiting = why != NULL;
    (void)em_copy(s->waiting, sizeof(s->waiting), why ? why : ""); /* a message */
}

static void settle(em_plan_scope *s, cJSON *op, const em_snapshot *snap, bool have_snap)
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
            em_log(EM_LOG_INFO, "emosa.agent.radio_plan", "the pod's radio plan is applied");
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

void em_plan_tick(em_plan_scope *s)
{
    em_snapshot snap = {0};
    bool have = s->engine.backend.snapshot(s, &snap);
    cJSON *op = em_journal_latest(s->journal);
    if (op)
        settle(s, op, &snap, have);
    cJSON_Delete(op);
    if (!have) {
        em_snapshot_clear(&snap);
        return;
    }
    /* RadioPlan._wanted */
    op = em_journal_latest(s->journal);
    cJSON *owned = em_journal_ownership(s->journal, s->intent.pod_id);
    char key[32];
    EM_FORMAT_FIXED(key, sizeof(key), "%s:radio-plan", s->instance); /* 16+11 */
    cJSON *done = NULL;
    bool wanted = false;
    if (!snap.ready) {
        wait_for(s, "pod not bound");
    } else if (owned) {
        wait_for(s, "another manager changed the plan: admit the pod anew");
    } else if (op && em_state_active(em_state_of(op))) {
        wait_for(s, "write in progress");
    } else if ((done = em_journal_lookup(s->journal, SOURCE, s->intent.pod_id, key))) {
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
    cJSON *intent = record(&s->intent);
    em_reason why;
    op = em_engine_request(&s->engine, intent, SOURCE, key, s->run_id, DEADLINE, "semantic", NULL, &why);
    cJSON_Delete(intent);
    if (op && !strcmp(em_state_of(op), "REQUESTED")) {
        cJSON *result = em_engine_execute(&s->engine,
                                          cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(op, "operation_id")));
        const char *reason = str(result, "reason");
        em_log(EM_LOG_INFO, "emosa.agent.radio_plan", "radio plan %s: %s %s", str(result, "operation_id"),
               em_state_of(result), reason ? reason : "");
        cJSON_Delete(result);
    }
    cJSON_Delete(op);
}

cJSON *em_plan_status(em_plan_scope *s)
{
    cJSON *o = cJSON_CreateObject(), *op = em_journal_latest(s->journal);
    const cJSON *config = s->has_last ? s->last.config : NULL;
    const char *keys[] = {"unclaimed",      "unclaimed_enabled", "unclaimed_inet_on", "unclaimed_exposed",
                          "operator_cloud", "channel",           "wired"};
    for (size_t k = 0; k < 7; k++) {
        const cJSON *v = cJSON_GetObjectItemCaseSensitive(config, keys[k]);
        cJSON_AddItemToObject(o, keys[k], v ? cJSON_Duplicate(v, 1) : cJSON_CreateNull());
    }
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
