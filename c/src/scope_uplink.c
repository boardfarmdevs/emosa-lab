/* SPDX-License-Identifier: Apache-2.0 */
/* The uplink scope (emosa.opensync.uplink, emosa.agent.uplink). */
#include "scope_uplink.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
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
    *why = EM_INVALID_INPUT;
    if (mode && !strcmp(mode, "bootstrap")) {
        /* a revert to the bootstrap: no credential is written, so no SSID/secret; it carries the
         * bootstrap BSSID (what the station lands on) and the snapshot to restore. Confirmed by
         * observation, not a credential fingerprint EMOSA cannot know (spec 8.6). */
        const cJSON *restore = cJSON_GetObjectItemCaseSensitive(intent, "restore");
        const char *b = cJSON_GetStringValue(bssid);
        if (!b || !bssid_ok(b) || !restore || !cJSON_IsObject(restore) || !str(restore, "ssid"))
            return NULL;
        if (!str(intent, "station") || !*str(intent, "station"))
            return NULL;
        cJSON *bt = cJSON_CreateObject();
        cJSON_AddStringToObject(bt, "uplink", "bootstrap");
        cJSON_AddStringToObject(bt, "station", str(intent, "station"));
        cJSON_AddStringToObject(bt, "bssid", b);
        *why = EM_OK;
        return bt;
    }
    size_t n = ssid ? strlen(ssid) : 0;
    if (n < 1 || n > 32)
        return NULL;
    if (!mode || (strcmp(mode, "multi-ap") && strcmp(mode, "gre"))) {
        *why = EM_UNSUPPORTED_OPERATION; /* only the Multi-AP or GRE-parent backhaul uplink */
        return NULL;
    }
    bool is_gre = !strcmp(mode, "gre");
    if (is_gre && (!bssid || cJSON_IsNull(bssid)))
        return NULL; /* a GRE child joins by its pinned BSSID only (spec 8.6), never by SSID */
    if (!str(intent, "pod_id") || !*str(intent, "pod_id") || !str(intent, "station") || !*str(intent, "station") ||
        !str(intent, "secret_ref") || !*str(intent, "secret_ref"))
        return NULL;
    if (bssid && !cJSON_IsNull(bssid) && !bssid_ok(cJSON_GetStringValue(bssid)))
        return NULL;
    char fp[65];
    if (!em_vault_fingerprint_ref(u->vault, str(intent, "secret_ref"), fp, why))
        return NULL;
    cJSON *t = cJSON_CreateObject();
    cJSON_AddStringToObject(t, "uplink", mode);
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
        if (rows[i].name && !strcmp(rows[i].name, name))
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

/* the station's sole credential in credential-list mode, else NULL; *kind is "multi-ap" or "gre"
 * (spec 8.6) by its onboard_type when it is one (NULL otherwise). A GRE-parent child's credential
 * is listed the same way (onboard_type=gre), only the kind and the applied State differ. */
static const cJSON *listed_credential(em_uplink_scope *u, const cJSON *vif, const char **key, const char **kind)
{
    if (kind)
        *kind = NULL;
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
    /* a legacy pod's credential has no enabled column: a linked one is used (spec 3.10) */
    bool enabled = sole && (ovs_true(sole, "enabled") || (u->legacy && !cJSON_GetObjectItemCaseSensitive(sole, "enabled")));
    bool listed = vif && ovs_true(vif, "enabled") && !(vssid && *vssid) && onboard && enabled && *key && **key;
    const char *k = NULL;
    if (listed && !strcmp(onboard, "multi_ap"))
        k = "multi-ap";
    else if (listed && !strcmp(onboard, "gre"))
        k = "gre";
    if (!k)
        return NULL;
    if (kind)
        *kind = k;
    return sole;
}

/* UplinkBackend._active: the station cm uses (the station itself, or its own gretap g-<station>),
 * else the one in credential-list mode, else the one the pod bootstraps on. cm's first: with two
 * stations in credential-list mode (a GRE move onto another station, spec 8.6) the bootstrap one
 * was taken, and a move onto the other was never seen applied (V54 bench, 17 October). */
static const char *active_of(em_uplink_scope *u, const station_row *rows, size_t n)
{
    const cJSON *row;
    const char *used = NULL;
    size_t nused = 0;
    cJSON_ArrayForEach(row, em_table(em_ovsdb_tables(u->ovs), "Connection_Manager_Uplink"))
    {
        if (ovs_true(row, "is_used")) {
            nused++;
            used = ovs_str(row, "if_name");
        }
    }
    if (nused == 1 && used) {
        const char *on = NULL;
        size_t non = 0;
        for (size_t i = 0; i < n; i++)
            if (rows[i].name && (!strcmp(used, rows[i].name) ||
                                 (!strncmp(used, "g-", 2) && !strcmp(used + 2, rows[i].name)))) {
                non++;
                on = rows[i].name;
            }
        if (non == 1)
            return on;
    }
    const char *found = NULL, *key;
    size_t written = 0;
    for (size_t i = 0; i < n; i++)
        if (listed_credential(u, rows[i].row, &key, NULL)) {
            written++;
            found = rows[i].name;
        }
    return written == 1 ? found : u->station;
}

/* UplinkBackend._configured: the station in Multi-AP credential-list mode, or not */
static cJSON *configured(em_uplink_scope *u, const cJSON *vif, const char *station, const cJSON **credential)
{
    const char *key, *kind;
    const cJSON *sole = listed_credential(u, vif, &key, &kind);
    bool multi_ap = sole != NULL;
    const char *vssid = vif ? ovs_str(vif, "ssid") : NULL;
    *credential = sole;
    cJSON *c = cJSON_CreateObject();
    cJSON_AddStringToObject(c, "uplink", multi_ap ? kind : "other");
    cJSON_AddStringToObject(c, "station", station);
    if (multi_ap) {
        const char *cssid = ovs_str(sole, "ssid");
        cJSON_AddItemToObject(c, "ssid", cssid ? cJSON_CreateString(cssid) : cJSON_CreateNull());
        char fp[65], mac[18];
        em_vault_fingerprint_text(u->vault, key, fp);
        cJSON_AddStringToObject(c, "credential_fingerprint", fp);
        /* the pin: the credential's bssid, or a legacy pod's station's parent */
        bool pinned = u->legacy ? em_row_mac(vif, "parent", mac) : em_row_mac(sole, "bssid", mac);
        cJSON_AddItemToObject(c, "bssid", pinned ? cJSON_CreateString(mac) : cJSON_CreateNull());
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

static bool configured_already(em_uplink_scope *u, const cJSON *intent);
static cJSON *station_config(em_uplink_scope *u, const char *name);

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
    const char *mode = str(intent, "mode");
    bool revert = mode && !strcmp(mode, "bootstrap");
    if (mode && !strcmp(mode, "gre") && configured_already(u, intent)) {
        /* the station already carries this pin and credential, enabled: no write, so nothing
         * moves on the air (a steer to where cm already is, a record that followed cm) */
        strcpy(out->status, "committed");
        out->reason = EM_OK;
        out->evidence = cJSON_CreateObject();
        cJSON_AddStringToObject(out->evidence, "attribution", "reply");
        cJSON_AddTrueToObject(out->evidence, "transaction_validated");
        cJSON_AddStringToObject(out->evidence, "transaction_id", str(attempt, "transaction_id"));
        cJSON_AddNumberToObject(out->evidence, "session_generation", generation);
        cJSON_AddStringToObject(out->evidence, "action", "already-configured");
        cJSON_AddStringToObject(out->evidence, "instance", u->instance);
        cJSON_AddNullToObject(out->evidence, "restore");
        return;
    }
    em_submit_result result = {0};
    const char *refusal;
    cJSON *restore_out = NULL; /* the pin's vconf snapshot, for the revert (NULL unless from bootstrap) */
    em_reason e;
    if (revert) {
        e = em_uplink_revert(u->serial, &session, str(intent, "station"),
                             cJSON_GetObjectItemCaseSensitive(intent, "restore"), &result, &refusal);
    } else {
        const char *names[EM_UPLINK_BANDS + 1];
        size_t nnames = bound_names(u, names);
        em_uplink_intent in = {str(intent, "station"),
                               str(intent, "ssid"),
                               str(intent, "secret_ref"),
                               str(intent, "bssid"),
                               names,
                               nnames,
                               mode && !strcmp(mode, "gre") ? "gre" : "multi_ap",
                               u->legacy};
        resolver r = {u->vault, NULL};
        e = em_uplink_submit(u->serial, &session, &in, resolve, &r, &result, &refusal, &restore_out);
        em_free_secret(r.key);
    }
    if (e != EM_OK) {
        strcpy(out->status, "unknown");
        out->reason = EM_OUTCOME_UNKNOWN;
        cJSON_Delete(restore_out);
        return;
    }
    EM_FORMAT_FIXED(out->status, sizeof(out->status), "%s", result.status);
    out->reason = result.reason;
    if (strcmp(result.status, "committed")) {
        out->evidence = em_refusal_evidence(result.database_error); /* NULL unless refused with one */
        cJSON_Delete(restore_out);
        return;
    }
    out->evidence = cJSON_CreateObject();
    cJSON_AddStringToObject(out->evidence, "attribution", "reply");
    cJSON_AddTrueToObject(out->evidence, "transaction_validated");
    cJSON_AddStringToObject(out->evidence, "transaction_id", str(attempt, "transaction_id"));
    cJSON_AddNumberToObject(out->evidence, "session_generation", generation);
    cJSON_AddStringToObject(out->evidence, "action", revert ? "revert" : "multi-ap-uplink");
    cJSON_AddStringToObject(out->evidence, "instance", u->instance);
    if (restore_out)
        cJSON_AddItemToObject(out->evidence, "restore", restore_out); /* transfers ownership */
    else
        cJSON_AddNullToObject(out->evidence, "restore");
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
    /* a kept target waiting to be tried again, or parked, is still the one kept */
    const char *to = u->has_kept_retry ? u->retry_target : u->has_parked ? u->parked_target : u->bssid;
    const char *station = u->has_kept_retry ? u->retry_station : u->has_parked ? u->parked_station : u->wanted;
    const char *mode = u->has_kept_retry ? u->retry_mode : u->has_parked ? u->parked_mode : u->mode;
    if (!strcmp(to, u->configured) && !strcmp(station, u->configured_station)) {
        unlink(path);
        return;
    }
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "configured", u->configured);
    cJSON_AddStringToObject(o, "target", to);
    cJSON_AddStringToObject(o, "station", station);
    cJSON_AddStringToObject(o, "mode", mode);
    cJSON_AddNumberToObject(o, "moves", u->moves);
    if (u->kept_tries)
        cJSON_AddNumberToObject(o, "tries", u->kept_tries);
    if (u->kept_fleet)
        cJSON_AddTrueToObject(o, "fleet");
    if (u->kept_rearms)
        cJSON_AddNumberToObject(o, "rearms", u->kept_rearms);
    if (u->has_parked)
        cJSON_AddBoolToObject(cJSON_AddObjectToObject(o, "parked"), "armed", u->parked_armed);
    char *text = em_json_dumps(o, EM_JSON_DEFAULT, false);
    cJSON_Delete(o);
    if (!em_write_file(path, text, true))
        em_log(EM_LOG_WARNING, "emosa.agent.uplink", "the target not kept in %s", path);
    free(text);
}

static void lower(const char *in, char *out, size_t n);

static void bootstrap_path(const em_uplink_scope *u, char *out, size_t n)
{
    EM_FORMAT_FIXED(out, n, "%s/bootstrap.json", em_journal_directory(u->journal));
}

/* Spec 8.6: the BSS the station is on before any EMOSA move is its bootstrap upstream (cm's own
 * association to the onboarding/GTP BSS). Recorded once, when first observed on a working uplink
 * with no move made, and persisted, so a later steer back to it is recognised as a revert. */
static void record_bootstrap(em_uplink_scope *u)
{
    if (u->has_bootstrap || u->moves || u->has_move)
        return;
    if (!u->has_facts || !u->facts.kind[0] || !u->facts.parent[0])
        return;
    lower(u->facts.parent, u->bootstrap_bssid, sizeof(u->bootstrap_bssid));
    EM_FORMAT_FIXED(u->bootstrap_ssid, sizeof(u->bootstrap_ssid), "%s", u->facts.ssid);
    u->has_bootstrap = true;
    char path[700];
    bootstrap_path(u, path, sizeof(path));
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "bssid", u->bootstrap_bssid);
    cJSON_AddStringToObject(o, "ssid", u->bootstrap_ssid);
    char *text = em_json_dumps(o, EM_JSON_DEFAULT, false);
    cJSON_Delete(o);
    if (text) {
        em_write_file(path, text, true);
        free(text);
    }
    em_log(EM_LOG_INFO, "emosa.agent.uplink", "bootstrap upstream recorded: %s (%s)",
            u->bootstrap_bssid, u->bootstrap_ssid);
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
    EM_FORMAT_FIXED(u->mode, sizeof(u->mode), "multi-ap"); /* a move to a GRE parent makes it gre */
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
            const char *kept_mode = str(kept, "mode"); /* a move to a GRE parent (spec 8.6) */
            const char *mode = kept_mode && !strcmp(kept_mode, "gre") ? "gre" : "multi-ap";
            const cJSON *moves = cJSON_GetObjectItemCaseSensitive(kept, "moves");
            u->moves = cJSON_IsNumber(moves) ? (unsigned)moves->valueint : 1;
            u->kept_fleet = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(kept, "fleet"));
            const cJSON *rearms = cJSON_GetObjectItemCaseSensitive(kept, "rearms");
            u->kept_rearms = cJSON_IsNumber(rearms) && rearms->valueint > 0 ? (unsigned)rearms->valueint : 0;
            const char *station = str(kept, "station");
            const char *on = bound(u, station) ? station : u->wanted; /* a bound if_name */
            const cJSON *parked = cJSON_GetObjectItemCaseSensitive(kept, "parked");
            if (cJSON_IsObject(parked)) {
                /* given up: the uplink on its configured upstream until the parent's AP is up
                 * again; an EMOSA restart takes it up no sooner */
                u->has_parked = true;
                u->parked_armed = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(parked, "armed"));
                EM_FORMAT_FIXED(u->parked_target, sizeof(u->parked_target), "%s", to);
                EM_FORMAT_FIXED(u->parked_station, sizeof(u->parked_station), "%s", on);
                EM_FORMAT_FIXED(u->parked_mode, sizeof(u->parked_mode), "%s", mode);
            } else {
                EM_FORMAT_FIXED(u->bssid, sizeof(u->bssid), "%s", to);
                EM_FORMAT_FIXED(u->mode, sizeof(u->mode), "%s", mode);
                const cJSON *tries = cJSON_GetObjectItemCaseSensitive(kept, "tries");
                u->kept_tries = cJSON_IsNumber(tries) && tries->valueint > 0 ? (unsigned)tries->valueint : 0;
                char chosen[33];
                EM_FORMAT_FIXED(chosen, sizeof(chosen), "%s", on);
                EM_FORMAT_FIXED(u->wanted, sizeof(u->wanted), "%s", chosen);
            }
        }
        cJSON_Delete(kept);
    }
    /* the bootstrap upstream recorded on an earlier start (spec 8.6), for a revert across restarts */
    char bpath[700];
    bootstrap_path(u, bpath, sizeof(bpath));
    char *btext = em_read_file(bpath, 4096, NULL);
    if (btext) {
        cJSON *saved = cJSON_Parse(btext);
        free(btext);
        const char *saved_bssid = str(saved, "bssid"), *bssid_ssid = str(saved, "ssid");
        uint8_t mac[6];
        if (saved_bssid && em_parse_mac(saved_bssid, mac)) {
            lower(saved_bssid, u->bootstrap_bssid, sizeof(u->bootstrap_bssid));
            EM_FORMAT_FIXED(u->bootstrap_ssid, sizeof(u->bootstrap_ssid), "%s", bssid_ssid ? bssid_ssid : "");
            u->has_bootstrap = true;
        }
        cJSON_Delete(saved);
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

#define OWNERSHIP_HOLD "the station's configuration was changed by another manager"

static double wall(const em_uplink_scope *u) { return u->wall ? u->wall() : (double)time(NULL); }

/* a held switch being made again: its hold's attempt (spec 8.3), beside target.json */
static void hold_path(const em_uplink_scope *u, char *out, size_t n)
{
    EM_FORMAT_FIXED(out, n, "%s/hold.json", em_journal_directory(u->journal)); /* EM_STATE_DIR_MAX */
}

/* UplinkSwitch._retry: the attempt of the hold whose switch is being made again, or 0 */
static int retry_attempt(const em_uplink_scope *u)
{
    char path[700];
    hold_path(u, path, sizeof(path));
    char *text = em_read_file(path, 256, NULL);
    if (!text)
        return 0;
    cJSON *o = cJSON_Parse(text);
    free(text);
    const cJSON *a = cJSON_GetObjectItemCaseSensitive(o, "attempt");
    int attempt = cJSON_IsNumber(a) && a->valueint > 0 ? a->valueint : 0;
    cJSON_Delete(o);
    return attempt;
}

static bool held(em_uplink_scope *u)
{
    cJSON *o = em_journal_ownership(u->journal, u->pod_id);
    bool h = o != NULL;
    cJSON_Delete(o);
    return h;
}

/* UplinkSwitch.hold: a failed switch's hold is bounded, the switch made again after a backoff
 * that doubles with each held retry (spec 8.3); another manager's change holds until a new
 * admission */
static void hold(em_uplink_scope *u, const cJSON *op, const char *reason)
{
    if (held(u))
        return;
    cJSON *e = cJSON_CreateObject();
    cJSON_AddItemToObject(e, "operation_id", op ? cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "operation_id"), 1)
                                                : cJSON_CreateNull());
    cJSON_AddStringToObject(e, "reason", reason);
    if (strcmp(reason, OWNERSHIP_HOLD)) {
        int attempt = retry_attempt(u) + 1;
        double base = u->hold_backoff > 0 ? u->hold_backoff : EM_UPLINK_HOLD_BACKOFF;
        double cap = u->hold_backoff_cap > 0 ? u->hold_backoff_cap : EM_UPLINK_HOLD_BACKOFF_CAP;
        double wait = base;
        for (int i = 1; i < attempt && wait < cap; i++)
            wait *= 2;
        if (wait > cap)
            wait = cap;
        double now = wall(u);
        cJSON_AddNumberToObject(e, "held_at", now);
        cJSON_AddNumberToObject(e, "attempt", attempt);
        cJSON_AddNumberToObject(e, "retry_at", now + wait);
        em_log(EM_LOG_WARNING, "emosa.agent.uplink", "uplink held on option 2: %s; switching again in %.0f s (hold %d)",
               reason, wait, attempt);
    } else {
        em_log(EM_LOG_WARNING, "emosa.agent.uplink", "uplink held on option 2: %s", reason);
    }
    em_journal_conflict(u->journal, u->pod_id, e);
    cJSON_Delete(e);
}

/* UplinkSwitch._hold_over: a bounded hold's backoff over, the hold released and its attempt
 * kept in hold.json, for the next hold's backoff and the retry's switch */
static bool hold_over(em_uplink_scope *u)
{
    cJSON *record = em_journal_ownership(u->journal, u->pod_id);
    const char *reason = str(record, "reason");
    bool over = false;
    if (!record) {
        over = true;
    } else if (reason && !strcmp(reason, OWNERSHIP_HOLD)) {
        over = false;
    } else if (!cJSON_GetObjectItemCaseSensitive(record, "retry_at")) {
        /* held before holds were bounded: its backoff starts now */
        double now = wall(u);
        cJSON_AddNumberToObject(record, "held_at", now);
        cJSON_AddNumberToObject(record, "attempt", 1);
        cJSON_AddNumberToObject(record, "retry_at", now + (u->hold_backoff > 0 ? u->hold_backoff : EM_UPLINK_HOLD_BACKOFF));
        em_journal_conflict(u->journal, u->pod_id, record);
    } else if (wall(u) >= cJSON_GetNumberValue(cJSON_GetObjectItemCaseSensitive(record, "retry_at"))) {
        const cJSON *a = cJSON_GetObjectItemCaseSensitive(record, "attempt");
        int attempt = cJSON_IsNumber(a) && a->valueint > 0 ? a->valueint : 1;
        char path[700], text[64];
        hold_path(u, path, sizeof(path));
        EM_FORMAT_FIXED(text, sizeof(text), "{\"attempt\": %d}\n", attempt);
        if (!em_write_file(path, text, true))
            em_log(EM_LOG_WARNING, "emosa.agent.uplink", "the hold's attempt not kept in %s", path);
        em_journal_release(u->journal, u->pod_id);
        em_log(EM_LOG_INFO, "emosa.agent.uplink", "uplink hold %d over: switching again", attempt);
        over = true;
    }
    cJSON_Delete(record);
    return over;
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

static void enable_taken_off(em_uplink_scope *u, const cJSON *op);

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
    enable_taken_off(u, op);
    EM_FORMAT_FIXED(u->bssid, sizeof(u->bssid), "%s", u->move.previous);
    EM_FORMAT_FIXED(u->wanted, sizeof(u->wanted), "%s", u->move.previous_station);
    EM_FORMAT_FIXED(u->mode, sizeof(u->mode), "%s", u->move.previous_mode[0] ? u->move.previous_mode : "multi-ap");
    u->moves++;
    keep_target(u);
    const cJSON *intent = cJSON_GetObjectItemCaseSensitive(op, "intent");
    const char *imode = str(intent, "mode"), *on = str(intent, "station");
    bool other = imode && !strcmp(imode, "gre") && on && strcmp(on, u->wanted);
    if (!u->bssid[0] || other) {
        /* a GRE child's move from its bootstrap: no upstream to go back to; or a GRE move onto
         * another station than the one the uplink goes back to, which the switch back leaves
         * enabled to join its target late and take cm (V54 bench, 17 October; a Multi-AP switch
         * back disables it): the agent undoes its own write (undo_intent) while the station still
         * carries it on this start */
        cJSON_Delete(u->fallback_after);
        u->fallback_after = cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "intent"), true);
        const char *planned = str(cJSON_GetObjectItemCaseSensitive(op, "plan"), "instance");
        (void)em_copy(u->fallback_instance, sizeof(u->fallback_instance), planned ? planned : "");
        u->undo_logged = false;
    }
    return true;
}

/* a switch to the kept target failed on a later start: back to the configured upstream once the
 * pod is off the failed switch, no hold (a switch to the configured upstream holds). Kept, the target held the
 * pod on option 2 for good once its BSS was gone (rdk-1004, 5 Oct 2026). */
/* a new upstream (the controller's move, a kept target applied or given up): no tries counted,
 * no retry, wait for a parent, parked target or re-arm pending */
static void kept_reset(em_uplink_scope *u)
{
    u->kept_tries = 0;
    u->has_kept_retry = u->has_parent_wait = u->has_parked = false;
    u->kept_rearms = 0;
}

static int ap_state(const em_uplink_scope *u, const char *bssid)
{
    uint8_t mac[6];
    return u->ap_state && bssid && em_parse_mac(bssid, mac) ? u->ap_state(u->ap_state_ctx, mac) : -1;
}

static unsigned kept_tries_max(const em_uplink_scope *u)
{
    return u->kept_tries_max > 0 ? (unsigned)u->kept_tries_max : EM_UPLINK_KEPT_TRIES;
}

static unsigned kept_rearms_max(const em_uplink_scope *u)
{
    return u->kept_rearms_max > 0 ? (unsigned)u->kept_rearms_max : EM_UPLINK_KEPT_REARMS;
}

/* UplinkSwitch._fleet (spec 8.3): the kept target bssid is a fleet pod's AP: a fleet agent names it
 * (in its ap_up), it is a GRE parent of the fleet, or the move onto it is a GRE one. Once known it
 * is remembered (target.json): an agent that has not said yet (its pod and agents starting again
 * together) is no proof that it left. */
static bool fleet_target(em_uplink_scope *u, const char *bssid, const char *mode)
{
    uint8_t mac[6];
    if (!u->kept_fleet && bssid &&
        ((mode && !strcmp(mode, "gre")) ||
         (u->is_parent && em_parse_mac(bssid, mac) && u->is_parent(u->is_parent_ctx, mac)) || ap_state(u, bssid) >= 0))
        u->kept_fleet = true;
    return u->kept_fleet;
}

/* UplinkSwitch._parent_up: whether the kept target's AP is up (ap_state), not saying counted as down
 * for a fleet pod's: 1, 0, or -1 for a target no fleet agent has named */
static int parent_up(em_uplink_scope *u, const char *bssid, const char *mode)
{
    int up = ap_state(u, bssid);
    return up < 0 && fleet_target(u, bssid, mode) ? 0 : up;
}

/* UplinkSwitch._park (spec 8.3): a fleet pod's kept target given up is parked, not forgotten: the
 * uplink back on its configured upstream, the target kept (target.json) and taken up again with its
 * tries anew once its parent's AP is up after it was not (armed: seen not up already), at most
 * kept_rearms_max times; then it is forgotten. */
static void park(em_uplink_scope *u, const char *bssid, const char *station, const char *mode, bool armed,
                 const char *why)
{
    unsigned rearms = u->kept_rearms;
    char target[18], on[33];
    const char *kind = mode && !strcmp(mode, "gre") ? "gre" : "multi-ap";
    /* the intent's, from the journal: one that does not fit is no target to keep */
    bool fits = em_copy(target, sizeof(target), bssid) && em_copy(on, sizeof(on), station ? station : "");
    kept_reset(u);
    if (!fits || rearms >= kept_rearms_max(u)) {
        u->kept_fleet = false;
        em_log(EM_LOG_WARNING, "emosa.agent.uplink",
               "kept backhaul target %s given up (%s), taken up again %u times already: forgotten, back to the configured %s",
               target, why, rearms, u->configured);
        return;
    }
    u->kept_rearms = rearms;
    u->has_parked = true;
    u->parked_armed = armed;
    EM_FORMAT_FIXED(u->parked_target, sizeof(u->parked_target), "%s", target);
    EM_FORMAT_FIXED(u->parked_station, sizeof(u->parked_station), "%s", on);
    EM_FORMAT_FIXED(u->parked_mode, sizeof(u->parked_mode), "%s", kind);
    em_log(EM_LOG_WARNING, "emosa.agent.uplink",
           "kept backhaul target %s given up (%s): parked until its parent's AP is up again (taken up again %u of %u "
           "times so far), back to the configured %s",
           target, why, rearms, kept_rearms_max(u), u->configured);
}

static bool kept_failed(em_uplink_scope *u, const cJSON *op, const char *reason)
{
    const char *bssid = str(cJSON_GetObjectItemCaseSensitive(op, "intent"), "bssid");
    if (!bssid || !strcmp(bssid, u->configured) || strcmp(bssid, u->bssid))
        return false;
    /* spec 8.3: a target on another fleet pod is given up only after kept_tries_max switches made
     * with that pod's AP up failed; a switch its AP went down during counts none, and the target
     * waits for it again. Meanwhile the pod is back on its bootstrap as below. Whether it is a fleet
     * pod's is asked again now: its agent may have spoken since the try. */
    int up = parent_up(u, bssid, u->mode);
    if (up >= 0 && (!up || u->kept_tries + 1 < kept_tries_max(u))) {
        if (up)
            u->kept_tries++;
        double base = u->kept_backoff > 0 ? u->kept_backoff : EM_UPLINK_KEPT_BACKOFF;
        double wait = up ? base * (double)(1u << (u->kept_tries - 1)) : 0;
        u->has_kept_retry = true;
        EM_FORMAT_FIXED(u->retry_target, sizeof(u->retry_target), "%s", bssid);
        EM_FORMAT_FIXED(u->retry_station, sizeof(u->retry_station), "%s", u->wanted);
        EM_FORMAT_FIXED(u->retry_mode, sizeof(u->retry_mode), "%s", u->mode);
        u->retry_at = wall(u) + wait;
        em_log(EM_LOG_WARNING, "emosa.agent.uplink",
               "kept backhaul target %s not reached (%s), its parent's AP %s: tried again in %d s (%u of %u tries failed)",
               bssid, reason, up ? "up" : "down or not named yet", (int)wait, u->kept_tries, kept_tries_max(u));
    } else if (up < 0) { /* no fleet agent has ever named it: the one-try rule */
        kept_reset(u);
        u->kept_fleet = false;
        em_log(EM_LOG_WARNING, "emosa.agent.uplink",
               "kept backhaul target %s not reached (%s), no fleet agent names its AP: forgotten, back to the configured %s",
               bssid, reason, u->configured);
    } else {
        char why[200];
        (void)em_format(why, sizeof(why), "%s; %u of %u tries failed", reason, u->kept_tries + 1, kept_tries_max(u));
        park(u, bssid, u->wanted, u->mode, false, why); /* its AP up all along: taken up again only once it was down */
    }
    EM_FORMAT_FIXED(u->bssid, sizeof(u->bssid), "%s", u->configured);
    EM_FORMAT_FIXED(u->wanted, sizeof(u->wanted), "%s", u->configured_station);
    EM_FORMAT_FIXED(u->mode, sizeof(u->mode), "multi-ap"); /* the configured upstream's kind */
    u->moves++; /* a switch of its own */
    keep_target(u);
    cJSON_Delete(u->fallback_after);
    u->fallback_after = cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "intent"), true);
    const char *planned = str(cJSON_GetObjectItemCaseSensitive(op, "plan"), "instance");
    (void)em_copy(u->fallback_instance, sizeof(u->fallback_instance), planned ? planned : "");
    u->undo_logged = false;
    enable_taken_off(u, op);
    return true;
}

static void lower(const char *in, char *out, size_t n)
{
    size_t i = 0;
    for (; in[i] && i + 1 < n; i++)
        out[i] = (char)tolower((unsigned char)in[i]);
    out[i] = 0;
}

/* A revert (spec 8.6): applied when the station is back on the bootstrap BSS in vconf mode (the
 * move's credential-list marker gone). Confirmed by observation, not a credential fingerprint EMOSA
 * cannot know. On a timeout EMOSA gives it up rather than loop; the pod returns to its pin on its
 * next OpenSync restart. */
static void settle_revert(em_uplink_scope *u, cJSON *op, const em_snapshot *snap, bool have)
{
    const char *state = em_state_of(op), *planned = str(cJSON_GetObjectItemCaseSensitive(op, "plan"), "instance");
    if (strcmp(state, "CONFIG_COMMITTED") && strcmp(state, "INDETERMINATE"))
        return;
    bool same_start = have && snap->ready && u->has_instance && planned && !strcmp(planned, u->instance);
    char fp[18];
    lower(u->facts.parent, fp, sizeof(fp));
    const char *cfg = snap ? str(snap->config, "uplink") : NULL;
    /* the controller's steer back is confirmed with the station on the bootstrap BSS; the undo of a
     * failed switch (undo_intent) once EMOSA's credential is off the station, whichever uplink cm
     * then uses (the pod's fallback station may keep it) */
    bool asked = u->has_move && !u->move.done && !strcmp(u->move.mode, "bootstrap");
    /* the undo of a failed move onto another station than the uplink's (spec 8.3): that station's
     * own configuration tells, and the uplink stays on its own station and record */
    const char *undone = str(cJSON_GetObjectItemCaseSensitive(op, "intent"), "station");
    bool other = !asked && undone && strcmp(undone, u->wanted);
    cJSON *own = other ? station_config(u, undone) : NULL;
    if (own)
        cfg = str(own, "uplink");
    bool on_bootstrap = u->has_facts && u->facts.kind[0] && !strcmp(fp, u->bootstrap_bssid);
    bool back = same_start && em_snapshot_fresh(snap) && u->has_bootstrap && (!asked || on_bootstrap) && cfg &&
                !strcmp(cfg, "other");
    cJSON_Delete(own); /* cfg is not used past this */
    bool expired = em_engine_expired(&u->engine, op);
    if (!back && !expired)
        return;
    if (other) {
        if (back) {
            cJSON *evidence = em_engine_evidence(snap, false, u->instance);
            em_transition(op, "OBSERVED_APPLIED", evidence);
            cJSON_ReplaceItemInObjectCaseSensitive(op, "application_evidence", evidence);
            em_set_reason(op, NULL);
            save_with_facts(u, op);
            em_log(EM_LOG_INFO, "emosa.agent.uplink", "the failed move's credential is off %s, back as the pod had it",
                   undone);
        } else {
            cJSON_ReplaceItemInObjectCaseSensitive(op, "original_outcome", cJSON_CreateString(state));
            cJSON_ReplaceItemInObjectCaseSensitive(op, "deadline_elapsed", cJSON_CreateTrue());
            em_transition(op, "TIMED_OUT", NULL);
            em_set_reason(op, "APPLY_TIMEOUT");
            save_with_facts(u, op);
            em_log(EM_LOG_WARNING, "emosa.agent.uplink", "undoing the failed move on %s not confirmed within the deadline",
                   undone);
        }
        cJSON_Delete(u->fallback_after); /* once per failed move: not again on this start */
        u->fallback_after = NULL;
        return;
    }
    if (back) {
        cJSON *evidence = em_engine_evidence(snap, false, u->instance);
        em_transition(op, "OBSERVED_APPLIED", evidence);
        cJSON_ReplaceItemInObjectCaseSensitive(op, "application_evidence", evidence);
        em_set_reason(op, NULL);
        save_with_facts(u, op);
        if (u->has_move && !strcmp(u->move.target, u->bootstrap_bssid)) {
            u->move.done = true;
            u->move.applied = true;
        }
    } else {
        cJSON_ReplaceItemInObjectCaseSensitive(op, "original_outcome", cJSON_CreateString(state));
        cJSON_ReplaceItemInObjectCaseSensitive(op, "deadline_elapsed", cJSON_CreateTrue());
        em_transition(op, "TIMED_OUT", NULL);
        em_set_reason(op, "APPLY_TIMEOUT");
        save_with_facts(u, op);
        if (u->has_move && !strcmp(u->move.target, u->bootstrap_bssid)) {
            u->move.done = true;
            u->move.applied = false;
            EM_FORMAT_FIXED(u->move.result, sizeof(u->move.result), "revert not confirmed within the deadline");
        }
        if (asked) {
            em_log(EM_LOG_WARNING, "emosa.agent.uplink", "backhaul revert not confirmed within the deadline; given up");
        } else {
            cJSON_Delete(u->fallback_after); /* once per failed switch: not again on this start */
            u->fallback_after = NULL;
            em_log(EM_LOG_WARNING, "emosa.agent.uplink",
                   "undoing the failed switch not confirmed within the deadline; given up");
        }
    }
    /* the move is undone: back to no EMOSA pin, so nothing more is written */
    EM_FORMAT_FIXED(u->mode, sizeof(u->mode), "multi-ap");
    EM_FORMAT_FIXED(u->bssid, sizeof(u->bssid), "%s", u->configured);
    EM_FORMAT_FIXED(u->wanted, sizeof(u->wanted), "%s", u->configured_station);
    u->moves++;
    keep_target(u);
    if (back && asked) {
        em_log(EM_LOG_INFO, "emosa.agent.uplink", "backhaul revert applied: the station is back on the bootstrap %s",
               u->bootstrap_bssid);
    } else if (back) {
        cJSON_Delete(u->fallback_after);
        u->fallback_after = NULL;
        const char *station = str(cJSON_GetObjectItemCaseSensitive(op, "intent"), "station");
        em_log(EM_LOG_INFO, "emosa.agent.uplink", "the failed switch's credential is off %s",
               station ? station : "the station");
    }
}

static void settle(em_uplink_scope *u, cJSON *op, const em_snapshot *snap, bool have)
{
    const char *imode = str(cJSON_GetObjectItemCaseSensitive(op, "intent"), "mode");
    if (imode && !strcmp(imode, "bootstrap")) {
        settle_revert(u, op, snap, have);
        return;
    }
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
            const char *to = str(cJSON_GetObjectItemCaseSensitive(op, "intent"), "bssid");
            if ((u->kept_tries || u->kept_rearms) && to && !strcmp(to, u->bssid) && strcmp(to, u->configured)) {
                kept_reset(u); /* spec 8.3: the kept target reached (not while parked) */
                keep_target(u);
            }
            if (retry_attempt(u)) {
                char path[700];
                hold_path(u, path, sizeof(path));
                unlink(path);
                em_log(EM_LOG_INFO, "emosa.agent.uplink", "uplink switch applied on its retry: the hold's backoff cleared");
            }
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
        /* another station's configuration is not this switch's to judge: cm on another station is
         * followed (follow_cm), not held */
        const char *on = str(cJSON_GetObjectItemCaseSensitive(op, "intent"), "station");
        bool mine = on && !strcmp(on, u->active);
        if (t && mine && planned && u->has_instance && !strcmp(planned, u->instance) && !em_matches(snap->config, t)) {
            em_set_reason(op, "OWNERSHIP_CONFLICT");
            save_with_facts(u, op);
            hold(u, op, OWNERSHIP_HOLD);
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
/* the backhaul BSS of one scope's latest applied M2 set */
static bool set_backhaul(em_journal *journal, char ssid[33], char ref[97])
{
    const cJSON *ops = journal ? em_journal_operations_view(journal) : NULL;
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

/* uplink.m2_backhaul: the fronthaul scope's set first, then each further radio's (spec 9) */
static bool m2_backhaul(em_uplink_scope *u, char ssid[33], char ref[97])
{
    if (set_backhaul(u->ap_journal, ssid, ref))
        return true;
    for (size_t k = 0; k < u->nradio_journals; k++)
        if (set_backhaul(u->radio_journals[k], ssid, ref))
            return true;
    return false;
}

static cJSON *intent_of(em_uplink_scope *u, const char *ssid, const char *ref)
{
    cJSON *i = cJSON_CreateObject();
    cJSON_AddStringToObject(i, "pod_id", u->pod_id);
    cJSON_AddStringToObject(i, "station", u->wanted);
    cJSON_AddStringToObject(i, "ssid", ssid);
    cJSON_AddStringToObject(i, "secret_ref", ref);
    cJSON_AddStringToObject(i, "mode", u->mode);
    cJSON_AddStringToObject(i, "bssid", u->bssid);
    return i;
}

/* The vconf snapshot taken at the move from the bootstrap (the most recent operation that captured
 * one in its commit evidence, for `station` when given: each station has its own), for the revert
 * to restore; NULL if there is none to undo. */
static cJSON *move_snapshot(em_uplink_scope *u, const char *station)
{
    const cJSON *ops = em_journal_operations_view(u->journal), *op, *found = NULL;
    cJSON_ArrayForEach(op, ops)
    {
        const cJSON *restore = cJSON_GetObjectItemCaseSensitive(
            cJSON_GetObjectItemCaseSensitive(op, "commit_evidence"), "restore");
        const char *on = str(cJSON_GetObjectItemCaseSensitive(op, "intent"), "station");
        if (restore && !cJSON_IsNull(restore) && (!station || (on && !strcmp(on, station))))
            found = restore;
    }
    return found ? cJSON_Duplicate(found, 1) : NULL;
}

/* A bound station at this read (UplinkBackend.configs and stations_state): its configured uplink
 * (as configured(): uplink, ssid, credential_fingerprint, bssid; the caller frees it), whether its
 * Config is enabled, its State's parent, and its own gretap's Connection_Manager_Uplink row
 * (g-<station>). False when the pod has no row for it. */
typedef struct {
    bool enabled, emosa, gretap, l2, l3; /* emosa: EMOSA's switch wrote it (its ssid "", the marker) */
    char parent[18];
} station_view;

static bool station_view_of(em_uplink_scope *u, const char *name, cJSON **config, station_view *out)
{
    memset(out, 0, sizeof(*out));
    if (config)
        *config = NULL;
    station_row rows[MAX_ROWS];
    size_t nrows;
    const char *node;
    em_reason why;
    const cJSON *vif = bind(u, rows, &nrows, &node, &why) ? row_of(rows, nrows, name) : NULL;
    if (!vif)
        return false;
    const cJSON *credential;
    if (config)
        *config = configured(u, vif, name, &credential);
    out->enabled = ovs_true(vif, "enabled");
    const char *vssid = ovs_str(vif, "ssid");
    out->emosa = vssid && !*vssid;
    em_uplink_state state;
    em_uplink_state_of(em_ovsdb_tables(u->ovs), name, &state);
    EM_FORMAT_FIXED(out->parent, sizeof(out->parent), "%s", state.parent);
    char gretap[40];
    EM_FORMAT_FIXED(gretap, sizeof(gretap), "g-%s", name); /* an if_name */
    const cJSON *row;
    cJSON_ArrayForEach(row, em_table(em_ovsdb_tables(u->ovs), "Connection_Manager_Uplink"))
    {
        const char *n = ovs_str(row, "if_name");
        if (n && !strcmp(n, gretap)) {
            out->gretap = true;
            out->l2 = ovs_true(row, "has_L2");
            out->l3 = ovs_true(row, "has_L3");
        }
    }
    return true;
}

/* the configured uplink of `name` (configured()), NULL when the pod has no row for it */
static cJSON *station_config(em_uplink_scope *u, const char *name)
{
    cJSON *config;
    station_view v;
    return name && station_view_of(u, name, &config, &v) ? config : NULL;
}

/* UplinkSwitch._undo: a GRE child's failed switch (spec 8.3 "Moved", 8.6). With no configured
 * upstream to switch to, the agent undoes its own write as the revert to the bootstrap does, while
 * the station still carries the failed switch's credential on that start. NULL when there is nothing
 * left to undo (a new start, or the credential gone) or no snapshot to restore. */
static cJSON *undo_intent(em_uplink_scope *u, const em_snapshot *snap)
{
    if (!u->fallback_after)
        return NULL;
    em_reason why;
    const char *station = str(u->fallback_after, "station");
    cJSON *failed = target(u, u->fallback_after, &why), *config = station_config(u, station);
    bool still = failed && u->has_instance && !strcmp(u->fallback_instance, u->instance) &&
                 em_matches(config ? config : snap->config, failed);
    cJSON_Delete(failed);
    cJSON_Delete(config);
    if (!still) {
        cJSON_Delete(u->fallback_after);
        u->fallback_after = NULL;
        return NULL;
    }
    cJSON *restore = move_snapshot(u, station);
    if (!restore || !u->has_bootstrap || !station) {
        cJSON_Delete(restore);
        return NULL;
    }
    if (!u->undo_logged) {
        u->undo_logged = true;
        em_log(EM_LOG_INFO, "emosa.agent.uplink", "the failed switch's credential is still on %s: undoing it", station);
    }
    cJSON *intent = cJSON_CreateObject();
    cJSON_AddStringToObject(intent, "pod_id", u->pod_id);
    cJSON_AddStringToObject(intent, "station", station);
    cJSON_AddStringToObject(intent, "ssid", "");
    cJSON_AddStringToObject(intent, "secret_ref", "");
    cJSON_AddStringToObject(intent, "mode", "bootstrap");
    cJSON_AddStringToObject(intent, "bssid", u->bootstrap_bssid);
    cJSON_AddItemToObject(intent, "restore", restore);
    return intent;
}

/* spec 8.3: a failed kept target's retry is due after its backoff, the stations taken off enabled
 * again and, for a GRE child (no configured upstream), its undo of the failed switch done: the
 * target is switched to again, a switch of its own (a Multi-AP pod's waits in wanted for the pod
 * back on its bootstrap, as any switch after a failed one does) */
static void kept_again(em_uplink_scope *u)
{
    if (!u->has_kept_retry || u->nreenable || (!u->bssid[0] && u->fallback_after))
        return;
    cJSON *op = em_journal_latest(u->journal);
    bool active = op && em_state_active(em_state_of(op));
    cJSON_Delete(op);
    if (wall(u) < u->retry_at || active)
        return;
    EM_FORMAT_FIXED(u->bssid, sizeof(u->bssid), "%s", u->retry_target);
    EM_FORMAT_FIXED(u->wanted, sizeof(u->wanted), "%s", u->retry_station);
    EM_FORMAT_FIXED(u->mode, sizeof(u->mode), "%s", u->retry_mode);
    u->has_kept_retry = false;
    u->moves++;
    keep_target(u);
    em_log(EM_LOG_INFO, "emosa.agent.uplink", "kept backhaul target %s: switching to it again (%u of %u tries failed)",
           u->bssid, u->kept_tries, kept_tries_max(u));
}

/* UplinkSwitch._note_fleet: while a target is kept, a fleet agent naming it marks it a fleet pod's
 * (target.json) */
static void note_fleet(em_uplink_scope *u)
{
    const char *target = u->has_kept_retry ? u->retry_target : u->has_parked ? u->parked_target : u->bssid;
    const char *mode = u->has_kept_retry ? u->retry_mode : u->has_parked ? u->parked_mode : u->mode;
    if (target[0] && strcmp(target, u->configured) && !u->kept_fleet && fleet_target(u, target, mode))
        keep_target(u);
}

/* UplinkSwitch._parked_again (spec 8.3): a parked target is taken up again, its tries anew, once its
 * parent's AP is reported up after it was not (an AP up throughout is no reason): a switch of its
 * own, after any undo of the failed one. Each time counts towards kept_rearms_max. */
static void parked_again(em_uplink_scope *u)
{
    if (!u->has_parked || u->nreenable || (!u->bssid[0] && u->fallback_after))
        return;
    cJSON *op = em_journal_latest(u->journal);
    bool active = op && em_state_active(em_state_of(op));
    cJSON_Delete(op);
    if (active)
        return;
    if (ap_state(u, u->parked_target) != 1) {
        if (!u->parked_armed) {
            u->parked_armed = true;
            keep_target(u);
        }
        return;
    }
    if (!u->parked_armed)
        return;
    u->kept_rearms++;
    EM_FORMAT_FIXED(u->bssid, sizeof(u->bssid), "%s", u->parked_target);
    EM_FORMAT_FIXED(u->wanted, sizeof(u->wanted), "%s", u->parked_station);
    EM_FORMAT_FIXED(u->mode, sizeof(u->mode), "%s", u->parked_mode);
    u->has_parked = false;
    u->moves++;
    keep_target(u);
    em_log(EM_LOG_INFO, "emosa.agent.uplink",
           "parked kept backhaul target %s: its parent's AP is up again: switching to it (taken up again %u of %u times)",
           u->bssid, u->kept_rearms, kept_rearms_max(u));
}

/* spec 8.3: a switch to a kept target whose AP another fleet pod has down (or, a fleet pod's, whose
 * AP no fleet agent names yet) is not made: it waits for that AP (no deadline running), at most
 * parent_wait_cap; then the target is parked as a failed one is. True while it waits (the waiting
 * text says so). */
static bool waits_for_parent(em_uplink_scope *u, const cJSON *intent)
{
    const char *bssid = str(intent, "bssid"), *imode = str(intent, "mode");
    bool moving = u->has_move && !u->move.done && bssid && !strcmp(u->move.target, bssid);
    if ((imode && !strcmp(imode, "bootstrap")) || !bssid || !bssid[0] || !strcmp(bssid, u->configured) || moving ||
        parent_up(u, bssid, imode) != 0) {
        u->has_parent_wait = false; /* the controller's move, or a parent up or not in the fleet */
        return false;
    }
    double now = wall(u), cap = u->parent_wait_cap > 0 ? u->parent_wait_cap : EM_UPLINK_PARENT_WAIT_CAP;
    if (!u->has_parent_wait || strcmp(u->wait_bssid, bssid)) {
        u->has_parent_wait = true;
        EM_FORMAT_FIXED(u->wait_bssid, sizeof(u->wait_bssid), "%s", bssid);
        u->wait_since = now;
        em_log(EM_LOG_INFO, "emosa.agent.uplink", "kept backhaul target %s: waiting for its parent's AP", bssid);
    }
    if (now - u->wait_since < cap) {
        wait_for(u, EM_UPLINK_PARENT_WAITING);
        return true;
    }
    char why[64];
    EM_FORMAT_FIXED(why, sizeof(why), "its parent's AP not up in %d s", (int)cap);
    park(u, bssid, str(intent, "station"), imode, true, why); /* not up: taken up again as soon as it is */
    EM_FORMAT_FIXED(u->bssid, sizeof(u->bssid), "%s", u->configured);
    EM_FORMAT_FIXED(u->wanted, sizeof(u->wanted), "%s", u->configured_station);
    EM_FORMAT_FIXED(u->mode, sizeof(u->mode), "multi-ap");
    u->moves++;
    keep_target(u);
    wait_for(u, "kept target given up: its parent not back");
    return true;
}

static cJSON *wanted(em_uplink_scope *u, const cJSON *op, const em_snapshot *snap)
{
    if (held(u) && !hold_over(u)) {
        wait_for(u, "held on option 2");
        return NULL;
    }
    if (!snap->ready) {
        wait_for(u, "pod not bound");
        return NULL;
    }
    cJSON *intent;
    if (!strcmp(u->mode, "bootstrap")) {
        /* a revert in flight: write back the move's snapshot and drop EMOSA's credential, until
         * the station is back on the bootstrap (then settle resets the mode and this stops); the
         * station the uplink is on stays enabled, whatever its snapshot says */
        cJSON *restore = move_snapshot(u, u->wanted);
        if (!restore || !u->has_bootstrap) {
            wait_for(u, "revert without a snapshot");
            cJSON_Delete(restore);
            return NULL;
        }
        if (cJSON_GetObjectItemCaseSensitive(restore, "enabled"))
            cJSON_ReplaceItemInObjectCaseSensitive(restore, "enabled", cJSON_CreateTrue());
        intent = cJSON_CreateObject();
        cJSON_AddStringToObject(intent, "pod_id", u->pod_id);
        cJSON_AddStringToObject(intent, "station", u->wanted);
        cJSON_AddStringToObject(intent, "ssid", "");
        cJSON_AddStringToObject(intent, "secret_ref", "");
        cJSON_AddStringToObject(intent, "mode", "bootstrap");
        cJSON_AddStringToObject(intent, "bssid", u->bootstrap_bssid);
        cJSON_AddItemToObject(intent, "restore", restore);
    } else {
        char ssid[33], ref[97];
        if (u->fixed_credentials) {
            EM_FORMAT_FIXED(ssid, sizeof(ssid), "%s", u->fixed_ssid);
            EM_FORMAT_FIXED(ref, sizeof(ref), "%s", u->fixed_ref);
        } else if (!m2_backhaul(u, ssid, ref)) {
            wait_for(u, "no EasyMesh backhaul credentials");
            return NULL;
        }
        const char *failed_on = u->fallback_after ? str(u->fallback_after, "station") : NULL;
        cJSON *undo = NULL;
        if (!u->bssid[0] || (failed_on && strcmp(failed_on, u->wanted))) {
            /* a failed switch's credential off its station first (undo_intent): a GRE child with
             * no configured upstream (spec 8.6) then writes nothing until the controller steers it
             * onto a parent, its bootstrap uplink (the GTP) standing; after a failed GRE move onto
             * another station, the uplink's own station is pinned again after the undo */
            undo = undo_intent(u, snap);
        }
        if (undo) {
            intent = undo;
        } else if (u->has_kept_retry) {
            /* spec 8.3: the failed kept target is tried again after its backoff (kept_again);
             * meanwhile the pod stays on its bootstrap, no switch to the configured upstream */
            char text[128];
            double left = u->retry_at - wall(u);
            EM_FORMAT_FIXED(text, sizeof(text), "kept target tried again in %d s", left > 0 ? (int)left : 0);
            wait_for(u, text);
            return NULL;
        } else if (!u->bssid[0]) {
            wait_for(u, "no upstream (awaiting a Backhaul Steering Request)");
            return NULL;
        } else {
            intent = intent_of(u, ssid, ref);
        }
    }
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
    const char *imode = str(intent, "mode");
    if (u->fallback_after && !(imode && !strcmp(imode, "bootstrap"))) {
        /* a switch written while cm still acts on the failed one is reverted with it (rdk-1004,
         * 5 Oct 2026): first the station off the failed credential, or a new start. Bounded:
         * OpenSync restarts a pod left without a working uplink. */
        if (u->has_instance && !strcmp(u->fallback_instance, u->instance)) {
            cJSON *failed = target(u, u->fallback_after, &why);
            cJSON *config = station_config(u, str(u->fallback_after, "station"));
            bool still = failed && em_matches(config ? config : snap->config, failed);
            cJSON_Delete(failed);
            cJSON_Delete(config);
            if (still) {
                wait_for(u, "the pod returning to its bootstrap uplink");
                cJSON_Delete(intent);
                return NULL;
            }
        }
        cJSON_Delete(u->fallback_after);
        u->fallback_after = NULL;
    }
    if (waits_for_parent(u, intent)) {
        cJSON_Delete(intent);
        return NULL;
    }
    if (u->settled && !u->settled(u->settled_ctx)) {
        wait_for(u, "fronthaul not settled");
        cJSON_Delete(intent);
        return NULL;
    }
    wait_for(u, NULL);
    return intent;
}

/* UplinkBackend._configured_already: a GRE switch's station already is what it would write:
 * enabled, in credential-list mode with one gre credential of the intent's SSID, key and pin */
static bool configured_already(em_uplink_scope *u, const cJSON *intent)
{
    em_reason why;
    const char *station = str(intent, "station");
    cJSON *t = target(u, intent, &why), *config = NULL;
    station_view v;
    bool same = t && station && station_view_of(u, station, &config, &v) && v.enabled && config;
    static const char *const keys[] = {"uplink", "ssid", "credential_fingerprint", "bssid"};
    for (int i = 0; i < 4 && same; i++) {
        const char *a = str(config, keys[i]), *b = str(t, keys[i]);
        same = a && b && !strcmp(a, b);
    }
    cJSON_Delete(t);
    cJSON_Delete(config);
    return same;
}

/* UplinkSwitch._new_start: a new start of the pod's OpenSync; a finished move's record is the
 * last start's (only the kept target carries over), so the status no longer shows it */
static void new_start(em_uplink_scope *u)
{
    if (!u->has_instance || (u->has_seen_instance && !strcmp(u->seen_instance, u->instance)))
        return;
    if (u->has_seen_instance && u->has_move && u->move.done)
        u->has_move = false;
    EM_FORMAT_FIXED(u->seen_instance, sizeof(u->seen_instance), "%s", u->instance);
    u->has_seen_instance = true;
}

static em_reason take_off(em_uplink_scope *u, const char *const *names, size_t n, bool restore, size_t *off,
                          bool *taken, bool *emosa)
{
    cJSON *restores[EM_UPLINK_STATIONS] = {0};
    if (n > EM_UPLINK_STATIONS)
        n = EM_UPLINK_STATIONS;
    for (size_t i = 0; i < n; i++) {
        restores[i] = restore ? move_snapshot(u, names[i]) : NULL;
        taken[i] = emosa[i] = false;
    }
    em_ovs_session session = {em_ovsdb_tables(u->ovs), em_ovsdb_generation(u->ovs), u->transact, u->transact_ctx};
    em_submit_result result = {0};
    *off = 0;
    em_reason e = u->legacy ? EM_OK
                            : em_uplink_take_off(u->serial, &session, names, (const cJSON *const *)restores, n,
                                                 &result, off, taken, emosa);
    for (size_t i = 0; i < n; i++)
        cJSON_Delete(restores[i]);
    if (e == EM_OK && *off && strcmp(result.status, "committed"))
        e = EM_OUTCOME_UNKNOWN;
    return e;
}

/* UplinkSwitch._make_before_break (spec 8.3, 8.6): a GRE move's station on its target with its
 * gretap up takes the station cm uses and any other EMOSA wrote off (em_uplink_take_off), once
 * per switch.
 * cm keeps a working uplink in use and re-selects among equal ones only at its periodic pass (2
 * minutes on the Pis, 17 October): only with the old station gone does it take the new one at
 * once. A legacy pod (spec 3.10) has one station, and its vendor healthcheck reboots it for a
 * disabled station row: none is taken off there. */
static void make_before_break(em_uplink_scope *u)
{
    cJSON *op = em_journal_latest(u->journal);
    const char *state = op ? em_state_of(op) : "";
    const cJSON *intent = op ? cJSON_GetObjectItemCaseSensitive(op, "intent") : NULL;
    const char *mode = str(intent, "mode"), *station = str(intent, "station"), *bssid = str(intent, "bssid");
    const char *id = op ? str(op, "operation_id") : NULL;
    const char *planned = op ? str(cJSON_GetObjectItemCaseSensitive(op, "plan"), "instance") : NULL;
    station_view own;
    /* the switch this process wrote (written), under way or applied already (cm may have taken the
     * new gretap by itself): not an older one, after the agent started again */
    bool live = !strcmp(state, "CONFIG_COMMITTED") || !strcmp(state, "INDETERMINATE") ||
                !strcmp(state, "OBSERVED_APPLIED");
    bool ready = live && id && !strcmp(id, u->written) && mode && !strcmp(mode, "gre") && station && bssid &&
                 strcmp(id, u->taken_off) && planned &&
                 u->has_instance && !strcmp(planned, u->instance) && station_view_of(u, station, NULL, &own) &&
                 !strcmp(own.parent, bssid) && own.gretap && own.l2 && own.l3;
    if (!ready) {
        cJSON_Delete(op);
        return;
    }
    EM_FORMAT_FIXED(u->taken_off, sizeof(u->taken_off), "%s", id);
    /* the station cm uses (the uplink's until now) and any EMOSA wrote: not the pod's own other
     * stations (a fallback station is the pod's, spec 8.6) */
    const char *names[EM_UPLINK_BANDS + 1], *others[EM_UPLINK_BANDS + 1];
    size_t n = bound_names(u, names), nothers = 0;
    for (size_t i = 0; i < n; i++) {
        station_view v;
        if (strcmp(names[i], station) && station_view_of(u, names[i], NULL, &v) && v.enabled &&
            (!strcmp(names[i], u->active) || v.emosa))
            others[nothers++] = names[i];
    }
    char list[160] = "";
    for (size_t i = 0; i < nothers; i++)
        EM_FORMAT_FIXED(list + strlen(list), sizeof(list) - strlen(list), "%s%s", i ? ", " : "", others[i]);
    size_t off = 0;
    bool taken[EM_UPLINK_STATIONS] = {0}, emosa[EM_UPLINK_STATIONS] = {0};
    em_reason e = nothers ? take_off(u, others, nothers, true, &off, taken, emosa) : EM_OK;
    if (e != EM_OK) {
        u->taken_off[0] = 0; /* again on the next tick, until the deadline */
        em_log(EM_LOG_WARNING, "emosa.agent.uplink", "make-before-break: taking %s off failed: %s", list,
               em_reason_name(e));
    } else if (off) {
        /* all of them for a failed switch to enable again (enable_taken_off): the pod's own as the
         * pod had them, EMOSA's with their snapshot back (on the pod's own network until the
         * re-pin, which waits for a working uplink) */
        EM_FORMAT_FIXED(u->took_op, sizeof(u->took_op), "%s", id);
        u->ntook = 0;
        for (size_t i = 0; i < nothers && i < EM_UPLINK_STATIONS; i++)
            if (taken[i])
                EM_FORMAT_FIXED(u->took[u->ntook++], sizeof(u->took[0]), "%s", others[i]);
        em_log(EM_LOG_INFO, "emosa.agent.uplink", "%s on %s with its gretap up: %s taken off (make-before-break)",
               station, bssid, list);
    }
    cJSON_Delete(op);
}

/* UplinkSwitch._enable_taken_off: a failed switch whose make-before-break took stations off:
 * those are enabled again (once, on this start), so the pod is not left without a backhaul
 * station, whether or not there is an upstream to go back to (its re-pin waits for an uplink) */
static void enable_taken_off(em_uplink_scope *u, const cJSON *op)
{
    const char *id = str(op, "operation_id");
    if (!id || strcmp(id, u->took_op))
        return;
    u->took_op[0] = 0;
    const char *planned = str(cJSON_GetObjectItemCaseSensitive(op, "plan"), "instance");
    u->nreenable = 0;
    for (size_t i = 0; i < u->ntook; i++)
        EM_FORMAT_FIXED(u->reenable[u->nreenable++], sizeof(u->reenable[0]), "%s", u->took[i]);
    u->ntook = 0;
    (void)em_copy(u->reenable_instance, sizeof(u->reenable_instance), planned ? planned : "");
}

/* UplinkSwitch._enable_own: the stations a failed switch's make-before-break took off, enabled
 * again */
static void enable_own(em_uplink_scope *u)
{
    if (!u->nreenable)
        return;
    if (!u->has_instance || strcmp(u->reenable_instance, u->instance)) {
        u->nreenable = 0; /* a new start: the pod's template has them as it wants */
        return;
    }
    const char *names[EM_UPLINK_BANDS + 1];
    char list[160] = "";
    for (size_t i = 0; i < u->nreenable; i++) {
        names[i] = u->reenable[i];
        EM_FORMAT_FIXED(list + strlen(list), sizeof(list) - strlen(list), "%s%s", i ? ", " : "", names[i]);
    }
    em_ovs_session session = {em_ovsdb_tables(u->ovs), em_ovsdb_generation(u->ovs), u->transact, u->transact_ctx};
    em_submit_result result = {0};
    size_t on = 0;
    em_reason e = em_uplink_enable(u->serial, &session, names, u->nreenable, &result, &on);
    if (e == EM_OK && on && strcmp(result.status, "committed")) {
        if (!strcmp(result.status, "unknown")) {
            em_log(EM_LOG_WARNING, "emosa.agent.uplink", "the stations taken off not enabled again yet: %s", list);
            return; /* again on the next tick */
        }
        e = EM_OUTCOME_UNKNOWN;
    }
    u->nreenable = 0;
    if (e != EM_OK)
        em_log(EM_LOG_WARNING, "emosa.agent.uplink", "the stations taken off not enabled again: %s", list);
    else if (on)
        em_log(EM_LOG_INFO, "emosa.agent.uplink", "the failed switch's make-before-break undone: %s enabled again",
               list);
}

/* UplinkSwitch._undo_without_snapshot: a failed GRE move onto another station with no snapshot of
 * it to put back (or no bootstrap recorded): that station taken off instead, once */
static void undo_without_snapshot(em_uplink_scope *u)
{
    if (!u->fallback_after || !u->has_instance || strcmp(u->fallback_instance, u->instance))
        return;
    char station[33];
    if (!em_copy(station, sizeof(station), str(u->fallback_after, "station") ? str(u->fallback_after, "station") : "") ||
        !station[0] || !strcmp(station, u->wanted))
        return;
    cJSON *restore = move_snapshot(u, station);
    bool can = restore && u->has_bootstrap;
    cJSON_Delete(restore);
    if (can)
        return;
    cJSON_Delete(u->fallback_after);
    u->fallback_after = NULL;
    const char *names[1] = {station};
    size_t off = 0;
    bool taken[1], emosa[1];
    em_reason e = take_off(u, names, 1, false, &off, taken, emosa);
    if (e != EM_OK)
        em_log(EM_LOG_WARNING, "emosa.agent.uplink", "the failed move's station %s not taken off: %s", station,
               em_reason_name(e));
    else if (off)
        em_log(EM_LOG_INFO, "emosa.agent.uplink", "the failed move's station %s taken off (no snapshot to restore)",
               station);
}

/* UplinkSwitch._follow_cm (spec 8.3): a GRE child's uplink record follows the station cm uses.
 * cm on another bound station's own gretap (the pod's own fallback, or cm's periodic pass) puts the
 * uplink on that station: on EMOSA's pin there when it carries one, else, on the same start, on
 * its bootstrap (no EMOSA upstream; on a new start the kept target is switched to instead). Nothing
 * is written for it; the reported backhaul already names that station. */
static void follow_cm(em_uplink_scope *u)
{
    cJSON *op = em_journal_latest(u->journal);
    bool active = op && em_state_active(em_state_of(op));
    const char *planned = op ? str(cJSON_GetObjectItemCaseSensitive(op, "plan"), "instance") : NULL;
    bool same_start = planned && u->has_instance && !strcmp(planned, u->instance);
    cJSON_Delete(op);
    if (active || !strcmp(u->mode, "bootstrap") || u->fallback_after)
        return;
    if (u->has_move && !u->move.done)
        return; /* the controller's move under way: the record is its target until it settles */
    if (u->configured[0] && strcmp(u->mode, "gre"))
        return;
    char gretap[40];
    EM_FORMAT_FIXED(gretap, sizeof(gretap), "g-%s", u->active); /* an if_name */
    if (!u->has_facts || !strcmp(u->active, u->wanted) || strcmp(u->facts.in_use, gretap))
        return;
    cJSON *config = station_config(u, u->active);
    const char *kind = str(config, "uplink"), *pin = str(config, "bssid");
    char bssid[18], mode[10];
    if (kind && !strcmp(kind, "gre") && pin && *pin) {
        EM_FORMAT_FIXED(bssid, sizeof(bssid), "%s", pin);
        EM_FORMAT_FIXED(mode, sizeof(mode), "gre");
    } else if (same_start) {
        EM_FORMAT_FIXED(bssid, sizeof(bssid), "%s", u->configured);
        EM_FORMAT_FIXED(mode, sizeof(mode), "multi-ap");
    } else {
        cJSON_Delete(config);
        return;
    }
    cJSON_Delete(config);
    em_log(EM_LOG_WARNING, "emosa.agent.uplink", "cm uses %s (parent %s), not %s: the uplink record follows it",
           u->active, u->facts.parent, u->wanted);
    EM_FORMAT_FIXED(u->wanted, sizeof(u->wanted), "%s", u->active);
    EM_FORMAT_FIXED(u->bssid, sizeof(u->bssid), "%s", bssid);
    EM_FORMAT_FIXED(u->mode, sizeof(u->mode), "%s", mode);
    u->moves++;
    keep_target(u);
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
    new_start(u);
    record_bootstrap(u);
    make_before_break(u);
    enable_own(u);
    undo_without_snapshot(u);
    follow_cm(u);
    note_fleet(u);
    kept_again(u);
    parked_again(u);
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
    int retry = retry_attempt(u);
    if (retry) { /* a held switch made again: one of its own, on the same start too */
        char base[128];
        EM_FORMAT_FIXED(base, sizeof(base), "%s", key);
        EM_FORMAT_FIXED(key, sizeof(key), "%s:retry%d", base, retry);
    }
    cJSON *req = em_engine_request(&u->engine, intent, SOURCE, key, u->run_id, DEADLINE, "semantic", NULL, &why);
    if (req && !strcmp(em_state_of(req), "REQUESTED")) {
        em_log(EM_LOG_INFO, "emosa.agent.uplink", "switching %s to the EasyMesh backhaul '%s'",
                str(intent, "station"), str(intent, "ssid"));
        (void)em_copy(u->written, sizeof(u->written), str(req, "operation_id") ? str(req, "operation_id") : "");
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
    /* steer only from a working EasyMesh backhaul EMOSA manages: a Multi-AP pod on its Multi-AP
     * backhaul, or a GRE child on its GRE path (the GTP, or a parent; spec 8.6). A Multi-AP pod
     * still on its bootstrap GRE is NOT steered: it reaches its own backhaul first. */
    bool on_backhaul = u->has_facts && (!strcmp(u->facts.kind, "multi-ap") ||
                                        (!strcmp(u->facts.kind, "gre") && (!u->configured[0] || !strcmp(u->mode, "gre"))));
    if (!on_backhaul)
        return "not_on_easymesh_backhaul";
    cJSON *op = em_journal_latest(u->journal);
    bool active = op && em_state_active(em_state_of(op));
    cJSON_Delete(op);
    if (active)
        return "switch_in_progress";
    if (u->has_bootstrap && !strcmp(target_bssid, u->bootstrap_bssid) &&
        strcmp(u->bssid, u->bootstrap_bssid)) {
        /* a steer back to the pod's own bootstrap BSS is a revert, not a pin (spec 8.6), but only
         * when EMOSA has moved the station away from it: a steer to the bootstrap while the station
         * is still on it is an ordinary confirm-in-place and falls through to the switch path below
         * (spec 8.3, the re-switch-to-the-BSS-in-use case). EMOSA undoes its move and the pod's
         * parent pin lands the station on the bootstrap. */
        memset(&u->move, 0, sizeof(u->move));
        u->has_move = true;
        EM_FORMAT_FIXED(u->move.target, sizeof(u->move.target), "%s", target_bssid);
        EM_FORMAT_FIXED(u->move.previous, sizeof(u->move.previous), "%s", u->bssid);
        EM_FORMAT_FIXED(u->move.station, sizeof(u->move.station), "%s", u->wanted);
        EM_FORMAT_FIXED(u->move.previous_station, sizeof(u->move.previous_station), "%s", u->wanted);
        EM_FORMAT_FIXED(u->move.mode, sizeof(u->move.mode), "bootstrap");
        EM_FORMAT_FIXED(u->move.previous_mode, sizeof(u->move.previous_mode), "%s", u->mode);
        EM_FORMAT_FIXED(u->mode, sizeof(u->mode), "bootstrap");
        EM_FORMAT_FIXED(u->bssid, sizeof(u->bssid), "%s", u->configured);
        u->moves++;
        kept_reset(u);
        u->kept_fleet = false;
        keep_target(u);
        em_log(EM_LOG_INFO, "emosa.agent.uplink", "backhaul revert requested: %s -> bootstrap %s",
               u->move.previous, target_bssid);
        return NULL;
    }
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
    /* a target in the fleet's GRE parent set is a 3-address GRE join (spec 8.6); any other is a
     * Multi-AP (4-address) join. A child must never try GRE onto a foreign AP (7f, 11 Oct). */
    uint8_t target_mac[6];
    const char *mode = (u->is_parent && em_parse_mac(target_bssid, target_mac) &&
                        u->is_parent(u->is_parent_ctx, target_mac))
                           ? "gre"
                           : "multi-ap";
    char chosen[33];
    EM_FORMAT_FIXED(chosen, sizeof(chosen), "%s", station); /* a bound if_name */
    memset(&u->move, 0, sizeof(u->move));
    u->has_move = true;
    EM_FORMAT_FIXED(u->move.target, sizeof(u->move.target), "%s", target_bssid);
    EM_FORMAT_FIXED(u->move.previous, sizeof(u->move.previous), "%s", u->bssid);
    EM_FORMAT_FIXED(u->move.station, sizeof(u->move.station), "%s", chosen);
    EM_FORMAT_FIXED(u->move.previous_station, sizeof(u->move.previous_station), "%s", u->wanted);
    EM_FORMAT_FIXED(u->move.mode, sizeof(u->move.mode), "%s", mode);
    EM_FORMAT_FIXED(u->move.previous_mode, sizeof(u->move.previous_mode), "%s", u->mode);
    kept_reset(u);
    u->kept_fleet = false;
    if (strcmp(target_bssid, u->bssid) || strcmp(chosen, u->wanted) || strcmp(mode, u->mode)) {
        EM_FORMAT_FIXED(u->bssid, sizeof(u->bssid), "%s", target_bssid);
        EM_FORMAT_FIXED(u->wanted, sizeof(u->wanted), "%s", chosen);
        EM_FORMAT_FIXED(u->mode, sizeof(u->mode), "%s", mode);
        u->moves++;
    }
    (void)fleet_target(u, target_bssid, mode); /* a fleet pod's AP, if its agent names it now */
    keep_target(u);
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
                   on && !strcmp(on, u->move.station) && u->has_facts && !strcmp(u->facts.kind, u->move.mode) &&
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
    if (u->has_bootstrap) {
        cJSON *b = cJSON_AddObjectToObject(o, "bootstrap");
        cJSON_AddStringToObject(b, "bssid", u->bootstrap_bssid);
        cJSON_AddStringToObject(b, "ssid", u->bootstrap_ssid);
    } else {
        cJSON_AddNullToObject(o, "bootstrap");
    }
    if (u->has_move) {
        cJSON *m = cJSON_AddObjectToObject(o, "move");
        cJSON_AddStringToObject(m, "target", u->move.target);
        cJSON_AddStringToObject(m, "previous", u->move.previous);
        cJSON_AddStringToObject(m, "station", u->move.station);
        cJSON_AddStringToObject(m, "previous_station", u->move.previous_station);
        cJSON_AddStringToObject(m, "mode", u->move.mode);
        cJSON_AddStringToObject(m, "previous_mode", u->move.previous_mode);
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
    /* spec 8.3: the kept target's failed tries with its parent's AP up, its retry, and its wait for
     * that AP (wall-clock seconds); whether it is a fleet pod's, and the target given up and parked
     * until its parent's AP is up again, and how often taken up again */
    cJSON *kept = cJSON_AddObjectToObject(o, "kept");
    cJSON_AddNumberToObject(kept, "tries", u->kept_tries);
    cJSON_AddNumberToObject(kept, "tries_max", kept_tries_max(u));
    cJSON_AddItemToObject(kept, "retry_at", u->has_kept_retry ? cJSON_CreateNumber(u->retry_at) : cJSON_CreateNull());
    cJSON_AddItemToObject(kept, "retry_target", u->has_kept_retry ? cJSON_CreateString(u->retry_target) : cJSON_CreateNull());
    cJSON_AddItemToObject(kept, "waiting_for_parent", u->has_parent_wait ? cJSON_CreateString(u->wait_bssid) : cJSON_CreateNull());
    cJSON_AddItemToObject(kept, "waiting_since", u->has_parent_wait ? cJSON_CreateNumber(u->wait_since) : cJSON_CreateNull());
    cJSON_AddBoolToObject(kept, "fleet", u->kept_fleet);
    cJSON_AddItemToObject(kept, "parked", u->has_parked ? cJSON_CreateString(u->parked_target) : cJSON_CreateNull());
    cJSON_AddNumberToObject(kept, "rearms", u->kept_rearms);
    cJSON_AddNumberToObject(kept, "rearms_max", kept_rearms_max(u));
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
