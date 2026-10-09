/* SPDX-License-Identifier: Apache-2.0 */
/* The uplink scope (emosa.opensync.uplink, emosa.agent.uplink). */
#include "scope_uplink.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "canon.h"
#include "log.h"
#include "ovs.h"
#include "scope.h"
#include "southbound.h"

#define MODE "opensync-6.6-uplink"
#define PROVENANCE "opensync-cm:Connection_Manager_Uplink+owm:Wifi_VIF_State"
#define SOURCE "uplink-policy"
#define DEADLINE 90 /* observed: adopted in 27 s; OpenSync restarts a failed uplink in 40-120 s */

static const char *str(const cJSON *o, const char *k)
{
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, k));
}

static bool bssid_ok(const char *m)
{
    return em_mac_text_ok(m);
}

/* UplinkIntent.target */
static cJSON *target(void *ctx, const cJSON *intent, em_reason *why)
{
    em_uplink_scope *u = ctx;
    const char *ssid = str(intent, "ssid"), *mode = str(intent, "mode");
    const cJSON *bssid = cJSON_GetObjectItemCaseSensitive(intent, "bssid");
    size_t n = ssid ? strlen(ssid) : 0;
    *why = EM_INVALID_INPUT;
    if (n < 1 || n > 32)
        return NULL;
    if (!mode || strcmp(mode, "multi-ap")) {
        *why = EM_UNSUPPORTED_OPERATION;
        return NULL;
    }
    if (!str(intent, "pod_id") || !*str(intent, "pod_id") || !str(intent, "station") || !*str(intent, "station") ||
        !str(intent, "secret_ref") || !*str(intent, "secret_ref"))
        return NULL;
    if (bssid && !cJSON_IsNull(bssid) && !bssid_ok(cJSON_GetStringValue(bssid)))
        return NULL;
    char fp[65];
    if (!em_vault_fingerprint_ref(u->vault, str(intent, "secret_ref"), fp, why))
        return NULL;
    cJSON *t = cJSON_CreateObject();
    cJSON_AddStringToObject(t, "uplink", "multi-ap");
    cJSON_AddStringToObject(t, "station", str(intent, "station"));
    cJSON_AddStringToObject(t, "ssid", ssid);
    cJSON_AddStringToObject(t, "credential_fingerprint", fp);
    if (bssid && !cJSON_IsNull(bssid))
        cJSON_AddStringToObject(t, "bssid", bssid->valuestring);
    *why = EM_OK;
    return t;
}

/* one of the pod's backhaul stations: the bootstrap one or a band's */
static bool bound(const em_uplink_scope *u, const char *name)
{
    if (!name)
        return false;
    if (u->station && !strcmp(name, u->station))
        return true;
    for (size_t i = 0; i < u->nbands; i++)
        if (u->band_stations[i] && !strcmp(u->band_stations[i], name))
            return true;
    return false;
}

/* the bound stations' names (the bootstrap one first), at most EM_UPLINK_BANDS + 1 */
static size_t bound_names(const em_uplink_scope *u, const char *out[EM_UPLINK_BANDS + 1])
{
    size_t n = 0;
    if (u->station)
        out[n++] = u->station;
    for (size_t i = 0; i < u->nbands; i++) {
        bool seen = false;
        for (size_t k = 0; k < n && !seen; k++)
            seen = u->band_stations[i] && !strcmp(out[k], u->band_stations[i]);
        if (u->band_stations[i] && !seen)
            out[n++] = u->band_stations[i];
    }
    return n;
}

typedef struct {
    const char *name;
    const cJSON *row;
} station_row;

#define MAX_ROWS (EM_UPLINK_BANDS + 1)

static const cJSON *row_of(const station_row *rows, size_t n, const char *name)
{
    for (size_t i = 0; name && i < n; i++)
        if (!strcmp(rows[i].name, name))
            return rows[i].row;
    return NULL;
}

/* every bound station's Config row; false when the binding fails */
static bool bind(em_uplink_scope *u, station_row rows[MAX_ROWS], size_t *nrows, const char **node, em_reason *why)
{
    const cJSON *tables = em_ovsdb_tables(u->ovs);
    *nrows = 0;
    *why = EM_NOT_READY;
    if (!em_ovsdb_ready(u->ovs) || !em_bound_node(tables, u->serial, node))
        return false;
    if (!(u->has_instance = em_start_instance(tables, u->instance)))
        return false;
    const cJSON *row;
    cJSON_ArrayForEach(row, em_table(tables, "Wifi_VIF_Config"))
    {
        const char *name = ovs_str(row, "if_name");
        if (!bound(u, name))
            continue;
        if (row_of(rows, *nrows, name))
            return false; /* "backhaul station ambiguous" */
        const char *mode = ovs_str(row, "mode");
        if (!mode || strcmp(mode, "sta")) {
            *why = EM_UNSUPPORTED_OPERATION; /* "the bound uplink VIF is not a station" */
            return false;
        }
        if (*nrows < MAX_ROWS)
            rows[(*nrows)++] = (station_row){name, row};
    }
    *why = EM_OK;
    return true;
}

static cJSON *text_or_null(const char *s) { return s && *s ? cJSON_CreateString(s) : cJSON_CreateNull(); }

/* the station's sole Multi-AP credential when it is in credential-list mode, else NULL */
static const cJSON *multi_ap_credential(em_uplink_scope *u, const cJSON *vif, const char **key)
{
    const cJSON *tables = em_ovsdb_tables(u->ovs);
    const char *links[8];
    size_t n = vif ? ovs_uuids(vif, "credential_configs", links, 8) : 0, found = 0;
    const cJSON *sole = NULL;
    for (size_t i = 0; i < n; i++) {
        const cJSON *c = cJSON_GetObjectItemCaseSensitive(em_table(tables, "Wifi_Credential_Config"), links[i]);
        if (c) {
            found++;
            sole = c;
        }
    }
    if (found != 1)
        sole = NULL;
    const char *enc = sole ? ovs_map_get(sole, "security", "encryption") : NULL;
    *key = enc && !strcmp(enc, "WPA-PSK") ? ovs_map_get(sole, "security", "key") : NULL;
    const char *vssid = vif ? ovs_str(vif, "ssid") : NULL, *onboard = sole ? ovs_str(sole, "onboard_type") : NULL;
    bool multi_ap = vif && ovs_true(vif, "enabled") && !(vssid && *vssid) && onboard && !strcmp(onboard, "multi_ap") &&
                    ovs_true(sole, "enabled") && *key;
    return multi_ap ? sole : NULL;
}

/* UplinkBackend._active: the station in Multi-AP credential-list mode, else the one cm uses,
 * else the one the pod bootstraps on */
static const char *active_of(em_uplink_scope *u, const station_row *rows, size_t n)
{
    const char *found = NULL, *key;
    size_t written = 0;
    for (size_t i = 0; i < n; i++)
        if (multi_ap_credential(u, rows[i].row, &key)) {
            written++;
            found = rows[i].name;
        }
    if (written == 1)
        return found;
    const cJSON *row;
    const char *used = NULL;
    size_t nused = 0;
    cJSON_ArrayForEach(row, em_table(em_ovsdb_tables(u->ovs), "Connection_Manager_Uplink"))
    {
        const char *name = ovs_str(row, "if_name");
        if (ovs_true(row, "is_used") && row_of(rows, n, name)) {
            nused++;
            used = name;
        }
    }
    return nused == 1 ? used : u->station;
}

/* UplinkBackend._configured: the station in Multi-AP credential-list mode, or not */
static cJSON *configured(em_uplink_scope *u, const cJSON *vif, const char *station, const cJSON **credential)
{
    const char *key;
    const cJSON *sole = multi_ap_credential(u, vif, &key);
    bool multi_ap = sole != NULL;
    const char *vssid = vif ? ovs_str(vif, "ssid") : NULL;
    *credential = sole;
    cJSON *c = cJSON_CreateObject();
    cJSON_AddStringToObject(c, "uplink", multi_ap ? "multi-ap" : "other");
    cJSON_AddStringToObject(c, "station", station);
    if (multi_ap) {
        const char *cssid = ovs_str(sole, "ssid");
        cJSON_AddItemToObject(c, "ssid", cssid ? cJSON_CreateString(cssid) : cJSON_CreateNull());
        char fp[65], mac[18];
        em_vault_fingerprint_text(u->vault, key, fp);
        cJSON_AddStringToObject(c, "credential_fingerprint", fp);
        cJSON_AddItemToObject(c, "bssid", em_row_mac(sole, "bssid", mac) ? cJSON_CreateString(mac) : cJSON_CreateNull());
    } else {
        cJSON_AddItemToObject(c, "ssid", vssid ? cJSON_CreateString(vssid) : cJSON_CreateNull());
        cJSON_AddNullToObject(c, "credential_fingerprint");
        cJSON_AddNullToObject(c, "bssid");
    }
    return c;
}

/* the station's one State row in station mode */
static const cJSON *station_state(em_uplink_scope *u, const char *station)
{
    const cJSON *row, *found = NULL;
    size_t n = 0;
    cJSON_ArrayForEach(row, em_table(em_ovsdb_tables(u->ovs), "Wifi_VIF_State"))
    {
        const char *name = ovs_str(row, "if_name"), *mode = ovs_str(row, "mode");
        if (name && !strcmp(name, station) && mode && !strcmp(mode, "sta")) {
            n++;
            found = row;
        }
    }
    return n == 1 ? found : NULL;
}

static bool snapshot(void *ctx, em_snapshot *out)
{
    em_uplink_scope *u = ctx;
    memset(out, 0, sizeof(*out));
    station_row rows[MAX_ROWS];
    size_t nrows;
    const char *node;
    em_reason why;
    if (!bind(u, rows, &nrows, &node, &why)) {
        if (!u->has_last)
            return false;
        out->config = cJSON_Duplicate(u->last.config, true);
        out->observed = cJSON_Duplicate(u->last.observed, true);
        cJSON_ReplaceItemInObjectCaseSensitive(out->observed, "fresh", cJSON_CreateFalse());
        out->generation = u->last.generation;
        memcpy(out->schema_fingerprint, u->last.schema_fingerprint, 65);
        return true;
    }
    EM_FORMAT_FIXED(u->active, sizeof(u->active), "%s", active_of(u, rows, nrows)); /* an if_name */
    const cJSON *vif = row_of(rows, nrows, u->active), *credential;
    out->config = configured(u, vif, u->active, &credential);
    em_uplink_state_of(em_ovsdb_tables(u->ovs), u->active, &u->facts);
    u->has_facts = true;
    cJSON *values = cJSON_CreateObject();
    cJSON_AddItemToObject(values, "uplink", text_or_null(u->facts.kind));
    cJSON_AddStringToObject(values, "station", u->active);
    cJSON_AddItemToObject(values, "ssid", text_or_null(u->facts.ssid));
    const char *cssid = credential ? ovs_str(credential, "ssid") : NULL;
    bool same_ssid = credential && cssid && u->facts.ssid[0] && !strcmp(cssid, u->facts.ssid);
    cJSON_AddItemToObject(values, "credential_fingerprint",
                          same_ssid ? cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(out->config, "credential_fingerprint"), 1)
                                    : cJSON_CreateNull());
    cJSON_AddItemToObject(values, "bssid", text_or_null(u->facts.parent));
    bool fresh = station_state(u, u->active) != NULL;
    out->ready = vif != NULL;
    out->generation = em_ovsdb_generation(u->ovs);
    EM_FORMAT_FIXED(out->schema_fingerprint, 65, "%s", em_ovsdb_schema_fingerprint(u->ovs)); /* SHA-256 hex */
    out->observed = em_observation(u->pod_id, u->active, values, MODE, out->generation, fresh, PROVENANCE,
                                   em_ovsdb_revision(u->ovs));
    em_snapshot_clear(&u->last);
    u->last.config = cJSON_Duplicate(out->config, true);
    u->last.observed = cJSON_Duplicate(out->observed, true);
    u->last.generation = out->generation;
    memcpy(u->last.schema_fingerprint, out->schema_fingerprint, 65);
    u->has_last = true;
    return true;
}

static bool own_bssid(em_uplink_scope *u, const char *bssid)
{
    static const char *const tables[] = {"Wifi_VIF_State", "Wifi_Radio_State"};
    for (int t = 0; t < 2; t++) {
        const cJSON *row;
        cJSON_ArrayForEach(row, em_table(em_ovsdb_tables(u->ovs), tables[t]))
        {
            char mac[18];
            if (em_row_mac(row, "mac", mac) && !strcmp(mac, bssid))
                return true;
        }
    }
    return false;
}

/* every MAC the pod's own VIFs and radios use (as own_bssid looks for one), at most cap */
static size_t own_macs(em_uplink_scope *u, uint8_t out[][6], size_t cap)
{
    static const char *const tables[] = {"Wifi_VIF_State", "Wifi_Radio_State"};
    size_t n = 0;
    for (int t = 0; t < 2; t++) {
        const cJSON *row;
        cJSON_ArrayForEach(row, em_table(em_ovsdb_tables(u->ovs), tables[t]))
        {
            char mac[18];
            if (n < cap && em_row_mac(row, "mac", mac) && em_parse_mac(mac, out[n]))
                n++;
        }
    }
    return n;
}

static cJSON *plan(void *ctx, const cJSON *intent, em_reason *why)
{
    em_uplink_scope *u = ctx;
    cJSON *t = target(ctx, intent, why);
    if (!t)
        return NULL;
    cJSON_Delete(t);
    const char *bssid = str(intent, "bssid"), *station = str(intent, "station");
    if (strcmp(str(intent, "pod_id"), u->pod_id) || !bound(u, station)) {
        *why = EM_UNSUPPORTED_OPERATION; /* "request exceeds the bound stations" */
        return NULL;
    }
    if (!bssid) {
        *why = EM_INVALID_INPUT; /* "the upstream BSSID is required" */
        return NULL;
    }
    station_row rows[MAX_ROWS];
    size_t nrows;
    const char *node;
    if (!bind(u, rows, &nrows, &node, why))
        return NULL;
    const cJSON *vif = row_of(rows, nrows, station);
    if (own_bssid(u, bssid)) {
        *why = EM_INVALID_INPUT; /* joining itself would bridge the pod's own backhaul BSS */
        return NULL;
    }
    uint8_t target_mac[6], own[64][6];
    if (u->loops && em_parse_mac(bssid, target_mac) &&
        u->loops(u->loops_ctx, target_mac, (const uint8_t(*)[6])own, own_macs(u, own, 64))) {
        *why = EM_INVALID_INPUT; /* a pod downstream of this one: its br-home bridged into its own */
        return NULL;
    }
    if (!vif) {
        *why = EM_NOT_READY; /* "the pod's backhaul station row is required" */
        return NULL;
    }
    em_uplink_state state;
    em_uplink_state_of(em_ovsdb_tables(u->ovs), active_of(u, rows, nrows), &state);
    if (!state.kind[0]) {
        *why = EM_NOT_READY; /* "no working uplink to switch from" */
        return NULL;
    }
    cJSON *p = cJSON_CreateObject();
    cJSON_AddStringToObject(p, "mapping", MODE);
    cJSON_AddStringToObject(p, "action", "multi-ap-uplink");
    cJSON_AddStringToObject(p, "station", station);
    cJSON_AddStringToObject(p, "ssid", str(intent, "ssid"));
    cJSON_AddStringToObject(p, "bssid", bssid);
    cJSON_AddStringToObject(p, "secret_ref", str(intent, "secret_ref"));
    cJSON *from = cJSON_AddObjectToObject(p, "from");
    cJSON_AddStringToObject(from, "kind", state.kind);
    cJSON_AddItemToObject(from, "in_use", text_or_null(state.in_use));
    /* the pod's other backhaul stations that are enabled, in name order: the switch disables them */
    const char *others[MAX_ROWS];
    size_t nothers = 0;
    for (size_t i = 0; i < nrows; i++) {
        if (!strcmp(rows[i].name, station) || !ovs_true(rows[i].row, "enabled"))
            continue;
        size_t at = nothers++;
        while (at > 0 && strcmp(others[at - 1], rows[i].name) > 0) {
            others[at] = others[at - 1];
            at--;
        }
        others[at] = rows[i].name;
    }
    cJSON_AddItemToObject(p, "disables", em_ovs_strings(others, nothers));
    cJSON_AddStringToObject(p, "instance", u->instance);
    const char *fields[] = {"Wifi_Credential_Config", "Wifi_VIF_Config.credential_configs/ssid",
                            "Wifi_VIF_Config.enabled of the other backhaul stations"};
    cJSON_AddItemToObject(p, "fields", em_ovs_strings(fields, 3));
    cJSON_AddStringToObject(p, "guard", "pod serial and the stations' current rows");
    *why = EM_OK;
    return p;
}

typedef struct {
    const em_vault *vault;
    char *key;
} resolver;

static const char *resolve(void *ctx, const char *ref)
{
    resolver *r = ctx;
    em_reason why;
    em_free_secret(r->key);
    r->key = em_vault_resolve(r->vault, ref, &why);
    return r->key;
}

static void submit(void *ctx, const cJSON *intent, const cJSON *attempt, em_submit_out *out)
{
    em_uplink_scope *u = ctx;
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
    if (em_ovsdb_generation(u->ovs) != generation) {
        strcpy(out->status, "rejected");
        out->reason = EM_NOT_READY;
        return;
    }
    em_ovs_session session = {em_ovsdb_tables(u->ovs), generation, u->transact, u->transact_ctx};
    const char *names[EM_UPLINK_BANDS + 1];
    size_t nnames = bound_names(u, names);
    em_uplink_intent in = {str(intent, "station"), str(intent, "ssid"), str(intent, "secret_ref"),
                           str(intent, "bssid"),    names,                nnames};
    resolver r = {u->vault, NULL};
    em_submit_result result;
    const char *refusal;
    em_reason e = em_uplink_submit(u->serial, &session, &in, resolve, &r, &result, &refusal);
    em_free_secret(r.key);
    if (e != EM_OK) {
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
    cJSON_AddStringToObject(out->evidence, "action", "multi-ap-uplink");
    cJSON_AddStringToObject(out->evidence, "instance", u->instance);
}

/* -- UplinkSwitch ------------------------------------------------------------------------- */

static void target_path(const em_uplink_scope *u, char *out, size_t n)
{
    EM_FORMAT_FIXED(out, n, "%s/target.json", em_journal_directory(u->journal)); /* EM_STATE_DIR_MAX */
}

static void keep_target(em_uplink_scope *u)
{
    char path[700];
    target_path(u, path, sizeof(path));
    if (!strcmp(u->bssid, u->configured) && !strcmp(u->wanted, u->configured_station)) {
        unlink(path);
        return;
    }
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "configured", u->configured);
    cJSON_AddStringToObject(o, "target", u->bssid);
    cJSON_AddStringToObject(o, "station", u->wanted);
    cJSON_AddNumberToObject(o, "moves", u->moves);
    char *text = em_json_dumps(o, EM_JSON_DEFAULT, false);
    cJSON_Delete(o);
    if (!em_write_file(path, text, true))
        em_log(EM_LOG_WARNING, "emosa.agent.uplink", "the target not kept in %s", path);
    free(text);
}

em_reason em_uplink_open(em_uplink_scope *u, const char *state_dir, const em_journal_schemas *schemas,
                         const char *bssid)
{
    char dir[600];
    EM_FORMAT_FIXED(dir, sizeof(dir), "%s/uplink", state_dir); /* EM_STATE_DIR_MAX */
    em_reason why;
    u->journal = em_journal_open(dir, schemas, &why);
    if (!u->journal)
        return why;
    em_engine_init(&u->engine, u->journal, u->vault, u->pod_id, (em_backend){MODE, target, snapshot, plan, submit}, u,
                   u->monotonic);
    em_engine_recover(&u->engine);
    for (size_t i = 0; bssid[i] && i < 17; i++)
        u->configured[i] = (char)tolower((unsigned char)bssid[i]);
    EM_FORMAT_FIXED(u->bssid, sizeof(u->bssid), "%s", u->configured);
    /* the configured station: the one the pod bootstraps on */
    if (!em_copy(u->configured_station, sizeof(u->configured_station), u->station))
        return EM_INVALID_INPUT;
    EM_FORMAT_FIXED(u->wanted, sizeof(u->wanted), "%s", u->configured_station);
    EM_FORMAT_FIXED(u->active, sizeof(u->active), "%s", u->configured_station);
    /* a move of the controller's is kept while the configured upstream is the one it replaced */
    char path[700];
    target_path(u, path, sizeof(path));
    char *text = em_read_file(path, 4096, NULL);
    if (text) {
        cJSON *kept = cJSON_Parse(text);
        free(text);
        const char *configured = str(kept, "configured"), *to = str(kept, "target");
        uint8_t mac[6];
        /* a file on the disk: a target that is not a MAC is not a target */
        if (configured && !strcmp(configured, u->configured) && to && em_parse_mac(to, mac)) {
            EM_FORMAT_FIXED(u->bssid, sizeof(u->bssid), "%s", to);
            const cJSON *moves = cJSON_GetObjectItemCaseSensitive(kept, "moves");
            u->moves = cJSON_IsNumber(moves) ? (unsigned)moves->valueint : 1;
            const char *station = str(kept, "station");
            if (bound(u, station))
                EM_FORMAT_FIXED(u->wanted, sizeof(u->wanted), "%s", station); /* a bound if_name */
        }
        cJSON_Delete(kept);
    }
    return EM_OK;
}

void em_uplink_close(em_uplink_scope *u)
{
    cJSON_Delete(u->fallback_after);
    u->fallback_after = NULL;
    em_engine_free(&u->engine);
    em_journal_close(u->journal);
    em_snapshot_clear(&u->last);
}

const em_uplink_state *em_uplink_facts(const em_uplink_scope *u) { return u->has_facts ? &u->facts : NULL; }

static bool held(em_uplink_scope *u)
{
    cJSON *o = em_journal_ownership(u->journal, u->pod_id);
    bool h = o != NULL;
    cJSON_Delete(o);
    return h;
}

static void hold(em_uplink_scope *u, const cJSON *op, const char *reason)
{
    if (held(u))
        return;
    em_log(EM_LOG_WARNING, "emosa.agent.uplink", "uplink held on option 2: %s", reason);
    cJSON *e = cJSON_CreateObject();
    cJSON_AddItemToObject(e, "operation_id", op ? cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "operation_id"), 1)
                                                : cJSON_CreateNull());
    cJSON_AddStringToObject(e, "reason", reason);
    em_journal_conflict(u->journal, u->pod_id, e);
    cJSON_Delete(e);
}

static cJSON *facts_json(const em_uplink_scope *u)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddItemToObject(o, "kind", text_or_null(u->facts.kind));
    cJSON_AddItemToObject(o, "in_use", text_or_null(u->facts.in_use));
    cJSON_AddStringToObject(o, "station", u->facts.station);
    cJSON_AddItemToObject(o, "ssid", text_or_null(u->facts.ssid));
    cJSON_AddItemToObject(o, "parent", text_or_null(u->facts.parent));
    cJSON_AddItemToObject(o, "mac", text_or_null(u->facts.mac));
    return o;
}

static void save_with_facts(em_uplink_scope *u, cJSON *op)
{
    cJSON *payload = cJSON_CreateObject();
    cJSON_AddItemToObject(payload, "observation", u->has_facts ? facts_json(u) : cJSON_CreateNull());
    em_engine_save(&u->engine, op, payload);
    cJSON_Delete(payload);
}

/* a move's switch failed: back to the previous upstream, no hold */
static bool move_failed(em_uplink_scope *u, const cJSON *op, const char *reason)
{
    const char *bssid = str(cJSON_GetObjectItemCaseSensitive(op, "intent"), "bssid");
    if (!u->has_move || u->move.done || !bssid || strcmp(bssid, u->move.target))
        return false;
    em_log(EM_LOG_WARNING, "emosa.agent.uplink", "backhaul move to %s failed (%s): back to %s", u->move.target, reason,
            u->move.previous);
    u->move.done = true;
    u->move.applied = false;
    (void)em_copy(u->move.result, sizeof(u->move.result), reason); /* a reason for the status */
    EM_FORMAT_FIXED(u->bssid, sizeof(u->bssid), "%s", u->move.previous);
    EM_FORMAT_FIXED(u->wanted, sizeof(u->wanted), "%s", u->move.previous_station);
    u->moves++;
    keep_target(u);
    return true;
}

/* a switch to the kept target failed on a later start: back to the configured upstream once the
 * pod is off the failed switch, no hold (a switch to the configured upstream holds). Kept, the target held the
 * pod on option 2 for good once its BSS was gone (rdk-1004, 5 Oct 2026). */
static bool kept_failed(em_uplink_scope *u, const cJSON *op, const char *reason)
{
    const char *bssid = str(cJSON_GetObjectItemCaseSensitive(op, "intent"), "bssid");
    if (!bssid || !strcmp(bssid, u->configured) || strcmp(bssid, u->bssid))
        return false;
    em_log(EM_LOG_WARNING, "emosa.agent.uplink", "kept backhaul target %s not reached (%s): back to the configured %s",
            bssid, reason, u->configured);
    EM_FORMAT_FIXED(u->bssid, sizeof(u->bssid), "%s", u->configured);
    EM_FORMAT_FIXED(u->wanted, sizeof(u->wanted), "%s", u->configured_station);
    u->moves++; /* a switch of its own */
    keep_target(u);
    cJSON_Delete(u->fallback_after);
    u->fallback_after = cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "intent"), true);
    const char *planned = str(cJSON_GetObjectItemCaseSensitive(op, "plan"), "instance");
    (void)em_copy(u->fallback_instance, sizeof(u->fallback_instance), planned ? planned : "");
    return true;
}

static void lower(const char *in, char *out, size_t n)
{
    size_t i = 0;
    for (; in[i] && i + 1 < n; i++)
        out[i] = (char)tolower((unsigned char)in[i]);
    out[i] = 0;
}

static void settle(em_uplink_scope *u, cJSON *op, const em_snapshot *snap, bool have)
{
    const char *state = em_state_of(op), *planned = str(cJSON_GetObjectItemCaseSensitive(op, "plan"), "instance");
    bool same_start = have && snap->ready && u->has_instance && planned && !strcmp(planned, u->instance);
    em_reason why;
    if (!strcmp(state, "CONFIG_COMMITTED") || !strcmp(state, "INDETERMINATE")) {
        cJSON *t = target(u, cJSON_GetObjectItemCaseSensitive(op, "intent"), &why);
        if (t && same_start && em_snapshot_satisfies(snap, t) && em_matches(snap->config, t)) {
            cJSON *evidence = em_engine_evidence(snap, false, u->instance);
            if (!strcmp(state, "INDETERMINATE"))
                cJSON_AddStringToObject(evidence, "attribution", "current_condition_only");
            em_transition(op, "OBSERVED_APPLIED", evidence);
            cJSON_ReplaceItemInObjectCaseSensitive(op, "application_evidence", evidence);
            em_set_reason(op, NULL);
            save_with_facts(u, op);
            em_log(EM_LOG_INFO, "emosa.agent.uplink", "uplink on the EasyMesh backhaul: %s parent %s", u->facts.kind,
                    u->facts.parent);
        } else if (em_engine_expired(&u->engine, op)) {
            cJSON_ReplaceItemInObjectCaseSensitive(op, "original_outcome", cJSON_CreateString(state));
            cJSON_ReplaceItemInObjectCaseSensitive(op, "deadline_elapsed", cJSON_CreateTrue());
            em_transition(op, "TIMED_OUT", NULL);
            em_set_reason(op, "APPLY_TIMEOUT");
            save_with_facts(u, op);
            if (!move_failed(u, op, "not confirmed within the deadline") &&
                !kept_failed(u, op, "not confirmed within the deadline"))
                hold(u, op, "switch not confirmed within the deadline");
        }
        cJSON_Delete(t);
    } else if (!strcmp(state, "OBSERVED_APPLIED") && have && snap->ready && em_snapshot_fresh(snap)) {
        cJSON *t = target(u, cJSON_GetObjectItemCaseSensitive(op, "intent"), &why);
        if (t && planned && u->has_instance && !strcmp(planned, u->instance) && !em_matches(snap->config, t)) {
            em_set_reason(op, "OWNERSHIP_CONFLICT");
            save_with_facts(u, op);
            hold(u, op, "the station's configuration was changed by another manager");
        }
        cJSON_Delete(t);
    } else if (!strcmp(state, "REJECTED") || !strcmp(state, "FAILED") || !strcmp(state, "OWNERSHIP_CONFLICT")) {
        const char *reason = str(op, "reason");
        char text[128], low[32];
        lower(state, low, sizeof(low));
        (void)em_format(text, sizeof(text), "%s: %s", low, reason ? reason : "None"); /* a message */
        if (move_failed(u, op, text))
            return;
        /* only a rejection before anything was sent may be retried, on a later start */
        if ((strcmp(state, "REJECTED") || !reason || (strcmp(reason, "NOT_READY") && strcmp(reason, "BUSY"))) &&
            !kept_failed(u, op, text)) {
            (void)em_format(text, sizeof(text), "switch %s: %s", low, reason ? reason : "None");
            hold(u, op, text);
        }
    }
}

static void wait_for(em_uplink_scope *u, const char *why)
{
    u->has_waiting = why != NULL;
    (void)em_copy(u->waiting, sizeof(u->waiting), why ? why : ""); /* a message */
}

/* the backhaul BSS (SSID, secret reference) of the AP scope's applied M2 set */
static bool m2_backhaul(em_uplink_scope *u, char ssid[33], char ref[97])
{
    const cJSON *ops = em_journal_operations_view(u->ap_journal);
    bool found = false;
    for (int i = cJSON_GetArraySize(ops) - 1; i >= 0; i--) {
        const cJSON *op = cJSON_GetArrayItem(ops, i);
        if (strcmp(em_state_of(op), "OBSERVED_APPLIED"))
            continue;
        const cJSON *b;
        cJSON_ArrayForEach(b, cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(op, "intent"), "additional"))
        {
            const char *role = str(b, "role");
            if (role && !strcmp(role, "backhaul")) {
                /* a journal record (either implementation's): fields checked, not trusted */
                const char *s = str(b, "ssid"), *r = str(b, "secret_ref");
                found = s && r && em_copy(ssid, 33, s) && em_copy(ref, 97, r);
                break;
            }
        }
        break; /* the latest applied set decides */
    }
    return found;
}

static cJSON *intent_of(em_uplink_scope *u, const char *ssid, const char *ref)
{
    cJSON *i = cJSON_CreateObject();
    cJSON_AddStringToObject(i, "pod_id", u->pod_id);
    cJSON_AddStringToObject(i, "station", u->wanted);
    cJSON_AddStringToObject(i, "ssid", ssid);
    cJSON_AddStringToObject(i, "secret_ref", ref);
    cJSON_AddStringToObject(i, "mode", "multi-ap");
    cJSON_AddStringToObject(i, "bssid", u->bssid);
    return i;
}

/* UplinkSwitch._wanted: the intent to request now, or NULL (waiting says why) */
static cJSON *wanted(em_uplink_scope *u, const cJSON *op, const em_snapshot *snap)
{
    if (held(u)) {
        wait_for(u, "held on option 2");
        return NULL;
    }
    if (!snap->ready) {
        wait_for(u, "pod not bound");
        return NULL;
    }
    char ssid[33], ref[97];
    if (u->fixed_credentials) {
        EM_FORMAT_FIXED(ssid, sizeof(ssid), "%s", u->fixed_ssid);
        EM_FORMAT_FIXED(ref, sizeof(ref), "%s", u->fixed_ref);
    } else if (!m2_backhaul(u, ssid, ref)) {
        wait_for(u, "no EasyMesh backhaul credentials");
        return NULL;
    }
    cJSON *intent = intent_of(u, ssid, ref);
    em_reason why;
    cJSON *t = target(u, intent, &why);
    if (!t) {
        char text[128];
        EM_FORMAT_FIXED(text, sizeof(text), "backhaul credentials unusable: %s", em_reason_name(why));
        wait_for(u, text);
        cJSON_Delete(intent);
        return NULL;
    }
    cJSON_Delete(t);
    const char *state = op ? em_state_of(op) : NULL;
    if (op && em_state_active(state)) {
        wait_for(u, "switch in progress");
        cJSON_Delete(intent);
        return NULL;
    }
    const char *planned = op ? str(cJSON_GetObjectItemCaseSensitive(op, "plan"), "instance") : NULL;
    if (planned && u->has_instance && !strcmp(planned, u->instance)) {
        bool same = cJSON_Compare(cJSON_GetObjectItemCaseSensitive(op, "intent"), intent, true);
        if (!strcmp(state, "OBSERVED_APPLIED") && same) {
            wait_for(u, NULL); /* applied on this start */
            cJSON_Delete(intent);
            return NULL;
        }
        if ((!strcmp(state, "REJECTED") || !strcmp(state, "CANCELLED")) && same) {
            char text[128];
            const char *reason = str(op, "reason");
            (void)em_format(text, sizeof(text), "not switched on this start: %s", reason ? reason : "None"); /* a message */
            wait_for(u, text);
            cJSON_Delete(intent);
            return NULL;
        }
    }
    if (!u->has_facts || !u->facts.kind[0]) {
        wait_for(u, "no working uplink yet");
        cJSON_Delete(intent);
        return NULL;
    }
    if (u->fallback_after) {
        /* a switch written while cm still acts on the failed one is reverted with it (rdk-1004,
         * 5 Oct 2026): first the station off the failed credential, or a new start. Bounded:
         * OpenSync restarts a pod left without a working uplink. */
        if (u->has_instance && !strcmp(u->fallback_instance, u->instance)) {
            cJSON *failed = target(u, u->fallback_after, &why);
            bool still = failed && em_matches(snap->config, failed);
            cJSON_Delete(failed);
            if (still) {
                wait_for(u, "the pod returning to its bootstrap uplink");
                cJSON_Delete(intent);
                return NULL;
            }
        }
        cJSON_Delete(u->fallback_after);
        u->fallback_after = NULL;
    }
    if (u->settled && !u->settled(u->settled_ctx)) {
        wait_for(u, "fronthaul not settled");
        cJSON_Delete(intent);
        return NULL;
    }
    wait_for(u, NULL);
    return intent;
}

void em_uplink_tick(em_uplink_scope *u)
{
    em_snapshot snap = {0};
    bool have = snapshot(u, &snap);
    cJSON *op = em_journal_latest(u->journal);
    if (op)
        settle(u, op, &snap, have);
    cJSON_Delete(op);
    if (!have) {
        em_snapshot_clear(&snap);
        return;
    }
    op = em_journal_latest(u->journal);
    cJSON *intent = wanted(u, op, &snap);
    cJSON_Delete(op);
    em_snapshot_clear(&snap);
    if (!intent)
        return;
    em_reason why;
    cJSON *t = target(u, intent, &why);
    char fp[65], key[128];
    em_engine_fingerprint(&u->engine, intent, t, fp);
    cJSON_Delete(t);
    /* one switch per start and credential, and per move of the controller's */
    if (u->moves)
        EM_FORMAT_FIXED(key, sizeof(key), "%s:%.16s:%u", u->instance, fp, u->moves);
    else
        EM_FORMAT_FIXED(key, sizeof(key), "%s:%.16s", u->instance, fp);
    cJSON *req = em_engine_request(&u->engine, intent, SOURCE, key, u->run_id, DEADLINE, "semantic", NULL, &why);
    if (req && !strcmp(em_state_of(req), "REQUESTED")) {
        em_log(EM_LOG_INFO, "emosa.agent.uplink", "switching %s to the EasyMesh backhaul '%s'", u->wanted,
                str(intent, "ssid"));
        cJSON *done = em_engine_execute(&u->engine, str(req, "operation_id"));
        const char *reason = str(done, "reason");
        em_log(EM_LOG_INFO, "emosa.agent.uplink", "uplink switch %s: %s %s", str(done, "operation_id"),
                em_state_of(done), reason ? reason : "");
        cJSON_Delete(done);
    }
    cJSON_Delete(req);
    cJSON_Delete(intent);
}

/* UplinkBackend.station_for: the pod's backhaul station on `band` it has a row for, or NULL */
static const char *station_for(em_uplink_scope *u, const char *band)
{
    station_row rows[MAX_ROWS];
    size_t nrows;
    const char *node;
    em_reason why;
    if (!bind(u, rows, &nrows, &node, &why))
        return NULL;
    for (size_t i = 0; i < u->nbands; i++)
        if (u->bands[i] && !strcmp(u->bands[i], band) && row_of(rows, nrows, u->band_stations[i]))
            return u->band_stations[i];
    return NULL;
}

/* UplinkBackend.fixed_channel: the channel `station` cannot leave, its radio's when that radio
 * carries the pod's BSSes (fixed_band); -1 for a station on a radio of its own */
static int fixed_channel(em_uplink_scope *u, const char *station)
{
    const cJSON *tables = em_ovsdb_tables(u->ovs), *row, *vif = NULL;
    cJSON_ArrayForEach(row, em_table(tables, "Wifi_VIF_Config"))
    {
        const char *name = ovs_str(row, "if_name");
        if (name && !strcmp(name, station))
            vif = row;
    }
    if (!vif || !u->fixed_band)
        return -1;
    cJSON_ArrayForEach(row, em_table(tables, "Wifi_Radio_Config"))
    {
        const char *links[16], *band = ovs_str(row, "freq_band");
        size_t n = ovs_uuids(row, "vif_configs", links, 16);
        for (size_t i = 0; i < n; i++) {
            if (strcmp(links[i], vif->string))
                continue;
            if (!band || strcmp(band, u->fixed_band))
                return -1;
            /* the channel the radio operates on: its State's */
            const char *radio = ovs_str(row, "if_name");
            const cJSON *state;
            cJSON_ArrayForEach(state, em_table(tables, "Wifi_Radio_State"))
            {
                const char *name = ovs_str(state, "if_name");
                const cJSON *channel = cJSON_GetObjectItemCaseSensitive(state, "channel");
                if (radio && name && !strcmp(name, radio))
                    return cJSON_IsNumber(channel) ? channel->valueint : -1;
            }
            return -1;
        }
    }
    return -1;
}

const char *em_uplink_steer(em_uplink_scope *u, const char *bssid, const char *band, int channel)
{
    char target_bssid[18];
    lower(bssid, target_bssid, sizeof(target_bssid));
    if (held(u))
        return "held_on_option_2";
    if (!u->has_facts || strcmp(u->facts.kind, "multi-ap"))
        return "not_on_easymesh_backhaul";
    cJSON *op = em_journal_latest(u->journal);
    bool active = op && em_state_active(em_state_of(op));
    cJSON_Delete(op);
    if (active)
        return "switch_in_progress";
    char own[18];
    lower(u->facts.mac, own, sizeof(own));
    if (!strcmp(target_bssid, own))
        return "own_station";
    /* the pod's station on the target's band, which may be another one than the one in use */
    const char *station = band ? station_for(u, band) : u->wanted;
    if (!station)
        return "no_station_on_band";
    int fixed = fixed_channel(u, station);
    if (fixed >= 0 && channel >= 0 && channel != fixed)
        return "channel_not_operable";
    char chosen[33];
    EM_FORMAT_FIXED(chosen, sizeof(chosen), "%s", station); /* a bound if_name */
    memset(&u->move, 0, sizeof(u->move));
    u->has_move = true;
    EM_FORMAT_FIXED(u->move.target, sizeof(u->move.target), "%s", target_bssid);
    EM_FORMAT_FIXED(u->move.previous, sizeof(u->move.previous), "%s", u->bssid);
    EM_FORMAT_FIXED(u->move.station, sizeof(u->move.station), "%s", chosen);
    EM_FORMAT_FIXED(u->move.previous_station, sizeof(u->move.previous_station), "%s", u->wanted);
    if (strcmp(target_bssid, u->bssid) || strcmp(chosen, u->wanted)) {
        EM_FORMAT_FIXED(u->bssid, sizeof(u->bssid), "%s", target_bssid);
        EM_FORMAT_FIXED(u->wanted, sizeof(u->wanted), "%s", chosen);
        u->moves++;
        keep_target(u);
    }
    em_log(EM_LOG_INFO, "emosa.agent.uplink", "backhaul move requested: %s (%s) -> %s (%s)", u->move.previous,
           u->move.previous_station, target_bssid, chosen);
    return NULL;
}

int em_uplink_steering_outcome(em_uplink_scope *u, const char *bssid, const char **why)
{
    char target_bssid[18];
    lower(bssid, target_bssid, sizeof(target_bssid));
    *why = NULL;
    if (!u->has_move || strcmp(u->move.target, target_bssid)) {
        *why = "no_such_move";
        return -1;
    }
    if (u->move.done) {
        if (u->move.applied)
            return 1;
        *why = u->move.result;
        return -1;
    }
    cJSON *op = em_journal_latest(u->journal);
    const cJSON *intent = op ? cJSON_GetObjectItemCaseSensitive(op, "intent") : NULL;
    const char *intended = str(intent, "bssid"), *on = str(intent, "station");
    bool applied = op && !strcmp(em_state_of(op), "OBSERVED_APPLIED") && intended && !strcmp(intended, u->move.target) &&
                   on && !strcmp(on, u->move.station) && u->has_facts && !strcmp(u->facts.kind, "multi-ap") &&
                   !strcmp(u->facts.station, u->move.station) && !strcmp(u->facts.parent, u->move.target);
    cJSON_Delete(op);
    if (applied) {
        u->move.done = u->move.applied = true;
        return 1;
    }
    return 0;
}

cJSON *em_uplink_status(em_uplink_scope *u)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "station", u->wanted);
    cJSON_AddStringToObject(o, "active_station", u->active);
    cJSON_AddStringToObject(o, "bssid", u->bssid);
    cJSON_AddStringToObject(o, "configured_bssid", u->configured);
    if (u->has_move) {
        cJSON *m = cJSON_AddObjectToObject(o, "move");
        cJSON_AddStringToObject(m, "target", u->move.target);
        cJSON_AddStringToObject(m, "previous", u->move.previous);
        cJSON_AddStringToObject(m, "station", u->move.station);
        cJSON_AddStringToObject(m, "previous_station", u->move.previous_station);
        cJSON_AddItemToObject(m, "result", !u->move.done ? cJSON_CreateNull()
                                           : u->move.applied ? cJSON_CreateTrue()
                                                             : cJSON_CreateString(u->move.result));
    } else {
        cJSON_AddNullToObject(o, "move");
    }
    bool multi_ap = u->has_facts && !strcmp(u->facts.kind, "multi-ap");
    cJSON_AddItemToObject(o, "uplink", u->has_facts ? text_or_null(u->facts.kind) : cJSON_CreateNull());
    cJSON_AddItemToObject(o, "in_use", u->has_facts ? text_or_null(u->facts.in_use) : cJSON_CreateNull());
    cJSON_AddItemToObject(o, "parent", u->has_facts ? text_or_null(u->facts.parent) : cJSON_CreateNull());
    cJSON_AddNumberToObject(o, "option", multi_ap ? 1 : 2);
    cJSON *owned = em_journal_ownership(u->journal, u->pod_id);
    cJSON_AddItemToObject(o, "held", owned ? owned : cJSON_CreateNull());
    cJSON_AddItemToObject(o, "waiting", u->has_waiting ? cJSON_CreateString(u->waiting) : cJSON_CreateNull());
    cJSON *op = em_journal_latest(u->journal);
    if (op) {
        cJSON *x = cJSON_AddObjectToObject(o, "operation");
        cJSON_AddItemToObject(x, "operation_id", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "operation_id"), 1));
        cJSON_AddItemToObject(x, "state", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "state"), 1));
        cJSON_AddItemToObject(x, "reason", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "reason"), 1));
        cJSON_AddItemToObject(x, "ssid", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(op, "intent"), "ssid"), 1));
        const char *instance = str(cJSON_GetObjectItemCaseSensitive(op, "plan"), "instance");
        cJSON_AddItemToObject(x, "instance", instance ? cJSON_CreateString(instance) : cJSON_CreateNull());
    } else {
        cJSON_AddNullToObject(o, "operation");
    }
    cJSON_Delete(op);
    return o;
}

em_backend em_uplink_backend(void) { return (em_backend){MODE, target, snapshot, plan, submit}; }
