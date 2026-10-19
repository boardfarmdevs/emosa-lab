/* SPDX-License-Identifier: Apache-2.0 */
/* The GTP station scope (emosa.opensync.gtp_station, emosa.agent.gtp_station; spec 3.10). */
#include "scope_gtp.h"

#include <ctype.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "canon.h"
#include "common.h"
#include "log.h"
#include "ovs.h"
#include "scope.h"
#include "southbound.h"

#define MODE "opensync-2.0-gtp-station"
#define PROVENANCE "opensync-wm:Wifi_VIF_Config+Wifi_VIF_State"
#define SOURCE "gtp-station-policy"
#define DEADLINE 30

static const char *str(const cJSON *o, const char *k)
{
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, k));
}

/* an interface name: 1 to 15 of [A-Za-z0-9_.-] (the reference's INTERFACE) */
static int by_uuid(const void *a, const void *b) { return strcmp(*(const char *const *)a, *(const char *const *)b); }

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

/* GtpStationIntent.validate */
static em_reason validate(const em_gtp_intent *i)
{
    if (!*i->pod_id || !*i->secret_ref || !interface_name(i->station))
        return EM_INVALID_INPUT;
    size_t n = strlen(i->ssid); /* 1-32 UTF-8 bytes; a C string has no NUL */
    if (n < 1 || n > 32)
        return EM_INVALID_INPUT;
    return !*i->bssid || em_mac_text_ok(i->bssid) ? EM_OK : EM_INVALID_INPUT;
}

bool em_gtp_intent_from(const char *pod_id, const char *station, const cJSON *bootstrap, em_gtp_intent *out,
                        em_reason *why)
{
    memset(out, 0, sizeof(*out));
    *why = EM_INVALID_INPUT;
    const char *ssid = str(bootstrap, "ssid"), *ref = str(bootstrap, "secret_ref"), *bssid = str(bootstrap, "bssid");
    /* a value that does not fit is refused, not cut: validate() would accept the cut one */
    if (!ssid || !ref || !em_copy(out->pod_id, sizeof(out->pod_id), pod_id) ||
        !em_copy(out->station, sizeof(out->station), station) || !em_copy(out->ssid, sizeof(out->ssid), ssid) ||
        !em_copy(out->secret_ref, sizeof(out->secret_ref), ref) ||
        (bssid && !em_copy(out->bssid, sizeof(out->bssid), bssid)))
        return false;
    for (char *c = out->bssid; *c; c++) /* the reference's lower_mac */
        *c = (char)tolower((unsigned char)*c);
    *why = validate(out);
    return *why == EM_OK;
}

/* GtpStationIntent.record() */
static cJSON *record(const em_gtp_intent *i)
{
    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "pod_id", i->pod_id);
    cJSON_AddStringToObject(r, "station", i->station);
    cJSON_AddStringToObject(r, "ssid", i->ssid);
    cJSON_AddStringToObject(r, "secret_ref", i->secret_ref);
    cJSON_AddItemToObject(r, "bssid", *i->bssid ? cJSON_CreateString(i->bssid) : cJSON_CreateNull());
    return r;
}

static bool from_record(const cJSON *r, em_gtp_intent *i)
{
    memset(i, 0, sizeof(*i));
    const char *bssid = str(r, "bssid");
    if (!str(r, "pod_id") || !str(r, "station") || !str(r, "ssid") || !str(r, "secret_ref"))
        return false;
    if (!em_copy(i->pod_id, sizeof(i->pod_id), str(r, "pod_id")) ||
        !em_copy(i->station, sizeof(i->station), str(r, "station")) ||
        !em_copy(i->ssid, sizeof(i->ssid), str(r, "ssid")) ||
        !em_copy(i->secret_ref, sizeof(i->secret_ref), str(r, "secret_ref")) ||
        (bssid && !em_copy(i->bssid, sizeof(i->bssid), bssid)))
        return false;
    return validate(i) == EM_OK;
}

/* GtpStationIntent.target: its passphrase by fingerprint only */
static cJSON *target(void *ctx, const cJSON *intent, em_reason *why)
{
    em_gtp_scope *g = ctx;
    em_gtp_intent i;
    char fp[65];
    if (!from_record(intent, &i)) {
        *why = EM_INVALID_INPUT;
        return NULL;
    }
    if (!em_vault_fingerprint_ref(g->vault, i.secret_ref, fp, why))
        return NULL;
    cJSON *t = cJSON_CreateObject();
    cJSON_AddStringToObject(t, "station", i.station);
    cJSON_AddStringToObject(t, "ssid", i.ssid);
    cJSON_AddStringToObject(t, "credential_fingerprint", fp);
    cJSON_AddItemToObject(t, "bssid", *i.bssid ? cJSON_CreateString(i.bssid) : cJSON_CreateNull());
    cJSON_AddTrueToObject(t, "joined");
    return t;
}

/* -- the pod's rows -------------------------------------------------------------------- */

/* the rows of `table` named `name`: how many, the last one and its uuid */
static size_t named_rows(const cJSON *tables, const char *table, const char *name, const cJSON **row,
                         const char **uuid)
{
    size_t n = 0;
    const cJSON *r;
    cJSON_ArrayForEach(r, em_table(tables, table))
    {
        const char *rn = ovs_str(r, "if_name");
        if (rn && !strcmp(rn, name)) {
            n++;
            if (row)
                *row = r;
            if (uuid)
                *uuid = r->string; /* the row's key in {uuid: row} */
        }
    }
    return n;
}

/* the reference's credential_key: a WPA-PSK security map's key, else NULL */
static const char *psk_of(const cJSON *row)
{
    const char *enc = row ? ovs_map_get(row, "security", "encryption") : NULL;
    return enc && !strcmp(enc, "WPA-PSK") ? ovs_map_get(row, "security", "key") : NULL;
}

/* GtpStationBackend._configured: the station in the scope's vconf form with one linked credential
 * (onboard_type gre) of the same SSID and passphrase, and wm's State of it; present: a row at all */
static int station_order(const void *a, const void *b)
{
    const cJSON *x = *(const cJSON *const *)a, *y = *(const cJSON *const *)b;
    const char *nx = ovs_str(x, "if_name"), *ny = ovs_str(y, "if_name");
    int k = strcmp(nx ? nx : "", ny ? ny : "");
    return k ? k : strcmp(x->string, y->string);
}

static cJSON *configured(const em_gtp_scope *g, const cJSON *tables)
{
    const cJSON *vif = NULL, *sole = NULL, *r;
    size_t rows = named_rows(tables, "Wifi_VIF_Config", g->intent.station, &vif, NULL);
    if (rows != 1)
        vif = NULL;
    const char *links[8];
    size_t n = vif ? ovs_uuids(vif, "credential_configs", links, 8) : 0, found = 0;
    for (size_t i = 0; i < n; i++) {
        const cJSON *c = cJSON_GetObjectItemCaseSensitive(em_table(tables, "Wifi_Credential_Config"), links[i]);
        if (c) {
            found++;
            sole = c;
        }
    }
    if (found != 1)
        sole = NULL;
    const char *mode = vif ? ovs_str(vif, "mode") : NULL, *ssid = vif ? ovs_str(vif, "ssid") : NULL;
    const char *key = psk_of(vif), *onboard = sole ? ovs_str(sole, "onboard_type") : NULL;
    const char *sole_ssid = sole ? ovs_str(sole, "ssid") : NULL, *sole_key = psk_of(sole);
    bool written = mode && !strcmp(mode, "sta") && ovs_true(vif, "enabled") && ssid && *ssid && key && onboard &&
                   !strcmp(onboard, "gre") && sole_ssid && !strcmp(sole_ssid, ssid) && sole_key &&
                   !strcmp(sole_key, key);
    bool joined = false, associated = false;
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_VIF_State"))
    {
        const char *rn = ovs_str(r, "if_name"), *rm = ovs_str(r, "mode");
        char parent[18];
        if (rn && !strcmp(rn, g->intent.station) && rm && !strcmp(rm, "sta")) {
            joined = true;
            /* on a BSS (its State's parent), as the vendor healthcheck's `wl bssid` sees it */
            associated = associated || em_row_mac(r, "parent", parent);
        }
    }
    cJSON *c = cJSON_CreateObject();
    cJSON_AddStringToObject(c, "station", g->intent.station);
    cJSON_AddBoolToObject(c, "present", rows > 0);
    cJSON_AddItemToObject(c, "ssid", written ? cJSON_CreateString(ssid) : cJSON_CreateNull());
    char fp[65], mac[18];
    if (written && em_vault_fingerprint_text(g->vault, key, fp))
        cJSON_AddStringToObject(c, "credential_fingerprint", fp);
    else
        cJSON_AddNullToObject(c, "credential_fingerprint");
    bool pinned = written && em_row_mac(vif, "parent", mac);
    cJSON_AddItemToObject(c, "bssid", pinned ? cJSON_CreateString(mac) : cJSON_CreateNull());
    cJSON_AddBoolToObject(c, "joined", joined);
    cJSON_AddBoolToObject(c, "associated", associated);
    /* every station row of the pod, the scope's own marked (all_stations: the guard's) */
    const cJSON *stations[32];
    size_t nstations = 0;
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_VIF_Config"))
    {
        const char *rm = ovs_str(r, "mode");
        if (rm && !strcmp(rm, "sta") && nstations < 32)
            stations[nstations++] = r;
    }
    qsort(stations, nstations, sizeof(*stations), station_order);
    cJSON *list = cJSON_AddArrayToObject(c, "stations");
    for (size_t k = 0; k < nstations; k++) {
        const char *name = ovs_str(stations[k], "if_name");
        bool on_bss = false;
        cJSON_ArrayForEach(r, em_table(tables, "Wifi_VIF_State"))
        {
            const char *rn = ovs_str(r, "if_name"), *rm = ovs_str(r, "mode");
            char parent[18];
            if (name && rn && !strcmp(rn, name) && rm && !strcmp(rm, "sta"))
                on_bss = on_bss || em_row_mac(r, "parent", parent);
        }
        cJSON *one = cJSON_CreateObject();
        cJSON_AddStringToObject(one, "uuid", stations[k]->string);
        cJSON_AddItemToObject(one, "if_name", name ? cJSON_CreateString(name) : cJSON_CreateNull());
        cJSON_AddBoolToObject(one, "own", written && stations[k] == vif);
        cJSON_AddBoolToObject(one, "associated", on_bss);
        cJSON_AddItemToArray(list, one);
    }
    return c;
}

static const cJSON *bind(em_gtp_scope *g, const char **node_uuid)
{
    const cJSON *tables = em_ovsdb_tables(g->ovs);
    const cJSON *node = em_bound_node(tables, g->serial, node_uuid);
    if (!node)
        return NULL;
    g->has_instance = em_start_instance(tables, g->instance);
    return g->has_instance ? node : NULL;
}

static bool snapshot(void *ctx, em_snapshot *out)
{
    em_gtp_scope *g = ctx;
    memset(out, 0, sizeof(*out));
    const char *node_uuid;
    const cJSON *node = em_ovsdb_ready(g->ovs) ? bind(g, &node_uuid) : NULL;
    if (!node) {
        if (!g->has_last)
            return false;
        out->config = cJSON_Duplicate(g->last.config, true);
        out->observed = cJSON_Duplicate(g->last.observed, true);
        cJSON_ReplaceItemInObjectCaseSensitive(out->observed, "fresh", cJSON_CreateFalse());
        out->generation = g->last.generation;
        memcpy(out->schema_fingerprint, g->last.schema_fingerprint, 65);
        return true;
    }
    out->config = configured(g, em_ovsdb_tables(g->ovs));
    out->ready = true;
    out->generation = em_ovsdb_generation(g->ovs);
    EM_FORMAT_FIXED(out->schema_fingerprint, 65, "%s", em_ovsdb_schema_fingerprint(g->ovs)); /* SHA-256 hex */
    out->observed = em_observation(g->intent.pod_id, "gtp-station", cJSON_Duplicate(out->config, true), MODE,
                                   out->generation, true, PROVENANCE, em_ovsdb_revision(g->ovs));
    em_snapshot_clear(&g->last);
    g->last.config = cJSON_Duplicate(out->config, true);
    g->last.observed = cJSON_Duplicate(out->observed, true);
    g->last.generation = out->generation;
    memcpy(g->last.schema_fingerprint, out->schema_fingerprint, 65);
    g->has_last = true;
    return true;
}

/* GtpStationBackend._check with the pod's rows: no station row, the one radio of the station's
 * band, at most one Inet row of the station (*inet_uuid NULL when none) */
static em_reason check(em_gtp_scope *g, const em_gtp_intent *i, const char **radio_uuid, const char **inet_uuid)
{
    if (strcmp(i->pod_id, g->intent.pod_id) || strcmp(i->station, g->intent.station))
        return EM_UNSUPPORTED_OPERATION;
    const cJSON *tables = em_ovsdb_tables(g->ovs), *r;
    if (named_rows(tables, "Wifi_VIF_Config", i->station, NULL, NULL))
        return EM_OWNERSHIP_CONFLICT; /* the pod's, an operator's or a start-up hook's station */
    size_t radios = 0;
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_Radio_Config"))
    {
        const char *band = ovs_str(r, "freq_band");
        if (band && !strcmp(band, g->band)) {
            radios++;
            *radio_uuid = r->string;
        }
    }
    if (radios != 1)
        return EM_NOT_READY; /* "no single radio of the station's band" */
    *inet_uuid = NULL;
    if (named_rows(tables, "Wifi_Inet_Config", i->station, NULL, inet_uuid) > 1)
        return EM_NOT_READY; /* "more than one Inet row of the station" */
    return EM_OK;
}

static cJSON *plan(void *ctx, const cJSON *intent, em_reason *why)
{
    em_gtp_scope *g = ctx;
    em_gtp_intent i;
    cJSON *t = target(ctx, intent, why); /* the reference's intent.target(vault): the secret resolves */
    if (!t)
        return NULL;
    cJSON_Delete(t);
    if (!from_record(intent, &i)) {
        *why = EM_INVALID_INPUT;
        return NULL;
    }
    if (strcmp(i.pod_id, g->intent.pod_id) || strcmp(i.station, g->intent.station)) {
        *why = EM_UNSUPPORTED_OPERATION;
        return NULL;
    }
    const char *node_uuid, *radio_uuid, *inet_uuid;
    const cJSON *node = em_ovsdb_ready(g->ovs) ? bind(g, &node_uuid) : NULL;
    if (!node) {
        *why = EM_NOT_READY;
        return NULL;
    }
    if ((*why = check(g, &i, &radio_uuid, &inet_uuid)) != EM_OK)
        return NULL;
    cJSON *p = cJSON_CreateObject();
    cJSON_AddStringToObject(p, "mapping", MODE);
    cJSON_AddStringToObject(p, "action", "gtp-station");
    cJSON_AddStringToObject(p, "station", i.station);
    cJSON_AddStringToObject(p, "ssid", i.ssid);
    cJSON_AddBoolToObject(p, "pinned", *i.bssid != 0);
    cJSON_AddStringToObject(p, "instance", g->instance);
    const char *fields[] = {"Wifi_Credential_Config", "Wifi_VIF_Config", "Wifi_Radio_Config.vif_configs",
                            "Wifi_Inet_Config.enabled"};
    cJSON_AddItemToObject(p, "fields", em_ovs_strings(fields, 4));
    cJSON_AddStringToObject(p, "guard", "pod serial, no station row");
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

/* security_map: a legacy pod's WPA2-PSK (spec 3.10) */
static cJSON *security_map(const char *psk)
{
    cJSON *map = cJSON_CreateArray(), *pairs = cJSON_CreateArray(), *enc = cJSON_CreateArray(),
          *key = cJSON_CreateArray();
    cJSON_AddItemToArray(enc, cJSON_CreateString("encryption"));
    cJSON_AddItemToArray(enc, cJSON_CreateString("WPA-PSK"));
    cJSON_AddItemToArray(key, cJSON_CreateString("key"));
    cJSON_AddItemToArray(key, cJSON_CreateString(psk));
    cJSON_AddItemToArray(pairs, enc);
    cJSON_AddItemToArray(pairs, key);
    cJSON_AddItemToArray(map, cJSON_CreateString("map"));
    cJSON_AddItemToArray(map, pairs);
    return map;
}

/* GtpStationBackend.transaction: the guards, the credential, the station, its radio reference and
 * its Inet row */
static cJSON *transaction(const em_gtp_scope *g, const em_gtp_intent *i, const char *psk, const char *node_uuid,
                          const char *radio_uuid, const char *inet_uuid)
{
    cJSON *ops = cJSON_CreateArray();
    cJSON *wait = em_ovs_op("wait", "AWLAN_Node", em_ovs_where_uuid(node_uuid));
    const char *node_cols[] = {"serial_number"};
    cJSON_AddItemToObject(wait, "columns", em_ovs_strings(node_cols, 1));
    cJSON_AddStringToObject(wait, "until", "==");
    cJSON *rows = cJSON_AddArrayToObject(wait, "rows"), *guard = cJSON_CreateObject();
    cJSON_AddStringToObject(guard, "serial_number", g->serial);
    cJSON_AddItemToArray(rows, guard);
    cJSON_AddNumberToObject(wait, "timeout", 0);
    cJSON_AddItemToArray(ops, wait);
    /* wait: no station row */
    wait = em_ovs_op("wait", "Wifi_VIF_Config", em_ovs_where_eq("if_name", g->intent.station));
    const char *vif_cols[] = {"if_name"};
    cJSON_AddItemToObject(wait, "columns", em_ovs_strings(vif_cols, 1));
    cJSON_AddStringToObject(wait, "until", "==");
    cJSON_AddArrayToObject(wait, "rows");
    cJSON_AddNumberToObject(wait, "timeout", 0);
    cJSON_AddItemToArray(ops, wait);
    /* the credential */
    cJSON *ins = em_ovs_op("insert", "Wifi_Credential_Config", NULL), *row = cJSON_CreateObject();
    cJSON_AddStringToObject(ins, "uuid-name", "gtp_credential");
    cJSON_AddStringToObject(row, "ssid", i->ssid);
    cJSON_AddItemToObject(row, "security", security_map(psk));
    cJSON_AddStringToObject(row, "onboard_type", "gre");
    cJSON_AddItemToObject(ins, "row", row);
    cJSON_AddItemToArray(ops, ins);
    /* the station, in vconf form, linked to the credential */
    ins = em_ovs_op("insert", "Wifi_VIF_Config", NULL);
    row = cJSON_CreateObject();
    cJSON_AddStringToObject(ins, "uuid-name", "gtp_station");
    cJSON_AddStringToObject(row, "if_name", g->intent.station);
    cJSON_AddStringToObject(row, "mode", "sta");
    cJSON_AddTrueToObject(row, "enabled");
    cJSON_AddNumberToObject(row, "vif_radio_idx", 0);
    cJSON_AddStringToObject(row, "ssid", i->ssid);
    cJSON_AddItemToObject(row, "security", security_map(psk));
    cJSON_AddItemToObject(row, "credential_configs", named_set("gtp_credential"));
    if (*i->bssid)
        cJSON_AddStringToObject(row, "parent", i->bssid);
    cJSON_AddItemToObject(ins, "row", row);
    cJSON_AddItemToArray(ops, ins);
    /* its reference in its radio's vif_configs */
    cJSON *mut = em_ovs_op("mutate", "Wifi_Radio_Config", em_ovs_where_uuid(radio_uuid));
    cJSON *mutations = cJSON_AddArrayToObject(mut, "mutations"), *m = cJSON_CreateArray();
    cJSON_AddItemToArray(m, cJSON_CreateString("vif_configs"));
    cJSON_AddItemToArray(m, cJSON_CreateString("insert"));
    cJSON_AddItemToArray(m, named_set("gtp_station"));
    cJSON_AddItemToArray(mutations, m);
    cJSON_AddItemToArray(ops, mut);
    /* its Inet row enabled: the seed's, or one of its own */
    if (inet_uuid) {
        cJSON *update = em_ovs_op("update", "Wifi_Inet_Config", em_ovs_where_uuid(inet_uuid));
        row = cJSON_CreateObject();
        cJSON_AddTrueToObject(row, "enabled");
        cJSON_AddTrueToObject(row, "network");
        cJSON_AddItemToObject(update, "row", row);
        cJSON_AddItemToArray(ops, update);
    } else {
        ins = em_ovs_op("insert", "Wifi_Inet_Config", NULL);
        row = cJSON_CreateObject();
        cJSON_AddStringToObject(row, "if_name", g->intent.station);
        cJSON_AddStringToObject(row, "if_type", "vif");
        cJSON_AddTrueToObject(row, "enabled");
        cJSON_AddTrueToObject(row, "network");
        cJSON_AddStringToObject(row, "ip_assign_scheme", "dhcp");
        cJSON_AddItemToObject(ins, "row", row);
        cJSON_AddItemToArray(ops, ins);
    }
    return ops;
}

static void submit(void *ctx, const cJSON *intent, const cJSON *attempt, em_submit_out *out)
{
    em_gtp_scope *g = ctx;
    memset(out, 0, sizeof(*out));
    em_gtp_intent i;
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
    const char *node_uuid, *radio_uuid, *inet_uuid;
    const cJSON *node = bind(g, &node_uuid);
    if (!node || em_ovsdb_generation(g->ovs) != generation || check(g, &i, &radio_uuid, &inet_uuid) != EM_OK) {
        strcpy(out->status, "rejected");
        out->reason = EM_NOT_READY;
        return;
    }
    char *psk = em_vault_resolve(g->vault, i.secret_ref, &why);
    if (!psk) {
        strcpy(out->status, "rejected");
        out->reason = why;
        return;
    }
    cJSON *ops = transaction(g, &i, psk, node_uuid, radio_uuid, inet_uuid);
    em_free_secret(psk);
    const int counts[] = {-1, -1, -1, -1, 1, inet_uuid ? 1 : -1};
    size_t nops = (size_t)cJSON_GetArraySize(ops);
    cJSON *results = g->transact(g->transact_ctx, ops);
    cJSON_Delete(ops);
    if (!results) {
        strcpy(out->status, "unknown");
        out->reason = EM_OUTCOME_UNKNOWN;
        out->evidence = cJSON_CreateObject();
        cJSON_AddStringToObject(out->evidence, "attribution", "unknown");
        return;
    }
    em_reason checked = em_check_results(results, counts, nops);
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
    cJSON_AddStringToObject(out->evidence, "action", "gtp-station");
    cJSON_AddStringToObject(out->evidence, "instance", g->instance);
    cJSON_AddItemToObject(out->evidence, "results", results);
}

/* GtpStationBackend.remove: the station in the scope's form (the intent's SSID), deleted in one
 * guarded transaction: its row, its linked credentials and its radio's reference. EM_OK when it
 * committed; *deleted the rows deleted */
static em_reason remove_station(em_gtp_scope *g, size_t *deleted)
{
    const char *node_uuid, *uuid = NULL;
    const cJSON *vif = NULL, *r;
    if (!em_ovsdb_ready(g->ovs) || !bind(g, &node_uuid))
        return EM_NOT_READY;
    const cJSON *tables = em_ovsdb_tables(g->ovs);
    if (named_rows(tables, "Wifi_VIF_Config", g->intent.station, &vif, &uuid) != 1)
        return EM_NOT_READY; /* "no single station row" */
    const char *mode = ovs_str(vif, "mode"), *ssid = ovs_str(vif, "ssid");
    if (!mode || strcmp(mode, "sta") || !ssid || strcmp(ssid, g->intent.ssid))
        return EM_OWNERSHIP_CONFLICT; /* "not the GTP station" */
    cJSON *ops = cJSON_CreateArray(), *row;
    int counts[2 + 4 + 1 + 8];
    size_t n = 0;
    cJSON *wait = em_ovs_op("wait", "AWLAN_Node", em_ovs_where_uuid(node_uuid));
    const char *node_cols[] = {"serial_number"}, *vif_cols[] = {"if_name", "mode", "ssid"};
    cJSON_AddItemToObject(wait, "columns", em_ovs_strings(node_cols, 1));
    cJSON_AddStringToObject(wait, "until", "==");
    row = cJSON_CreateObject();
    cJSON_AddStringToObject(row, "serial_number", g->serial);
    cJSON_AddItemToArray(cJSON_AddArrayToObject(wait, "rows"), row);
    cJSON_AddNumberToObject(wait, "timeout", 0);
    cJSON_AddItemToArray(ops, wait);
    counts[n++] = -1;
    wait = em_ovs_op("wait", "Wifi_VIF_Config", em_ovs_where_uuid(uuid));
    cJSON_AddItemToObject(wait, "columns", em_ovs_strings(vif_cols, 3));
    cJSON_AddStringToObject(wait, "until", "==");
    row = cJSON_CreateObject();
    cJSON_AddStringToObject(row, "if_name", g->intent.station);
    cJSON_AddStringToObject(row, "mode", "sta");
    cJSON_AddStringToObject(row, "ssid", g->intent.ssid);
    cJSON_AddItemToArray(cJSON_AddArrayToObject(wait, "rows"), row);
    cJSON_AddNumberToObject(wait, "timeout", 0);
    cJSON_AddItemToArray(ops, wait);
    counts[n++] = -1;
    /* the radios that list it (one), sorted by UUID as the reference */
    const char *radios[4];
    size_t nradios = 0;
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_Radio_Config"))
    {
        const char *refs[64];
        size_t nrefs = ovs_uuids(r, "vif_configs", refs, 64);
        for (size_t k = 0; k < nrefs && nradios < 4; k++)
            if (!strcmp(refs[k], uuid)) {
                radios[nradios++] = r->string;
                break;
            }
    }
    em_sort(radios, nradios, sizeof(*radios), by_uuid);
    for (size_t k = 0; k < nradios; k++) {
        cJSON *mutate = em_ovs_op("mutate", "Wifi_Radio_Config", em_ovs_where_uuid(radios[k]));
        cJSON *mutation = cJSON_CreateArray(), *set = cJSON_CreateArray(), *items = cJSON_CreateArray(),
              *ref = cJSON_CreateArray();
        cJSON_AddItemToArray(ref, cJSON_CreateString("uuid"));
        cJSON_AddItemToArray(ref, cJSON_CreateString(uuid));
        cJSON_AddItemToArray(items, ref);
        cJSON_AddItemToArray(set, cJSON_CreateString("set"));
        cJSON_AddItemToArray(set, items);
        cJSON_AddItemToArray(mutation, cJSON_CreateString("vif_configs"));
        cJSON_AddItemToArray(mutation, cJSON_CreateString("delete"));
        cJSON_AddItemToArray(mutation, set);
        cJSON_AddItemToArray(cJSON_AddArrayToObject(mutate, "mutations"), mutation);
        cJSON_AddItemToArray(ops, mutate);
        counts[n++] = 1;
    }
    cJSON_AddItemToArray(ops, em_ovs_op("delete", "Wifi_VIF_Config", em_ovs_where_uuid(uuid)));
    counts[n++] = 1;
    const char *links[8], *credentials[8];
    size_t nlinks = ovs_uuids(vif, "credential_configs", links, 8), ncredentials = 0;
    for (size_t k = 0; k < nlinks; k++)
        if (cJSON_GetObjectItemCaseSensitive(em_table(tables, "Wifi_Credential_Config"), links[k]))
            credentials[ncredentials++] = links[k];
    em_sort(credentials, ncredentials, sizeof(*credentials), by_uuid);
    for (size_t k = 0; k < ncredentials; k++) {
        cJSON_AddItemToArray(ops, em_ovs_op("delete", "Wifi_Credential_Config", em_ovs_where_uuid(credentials[k])));
        counts[n++] = 1;
    }
    cJSON *results = g->transact(g->transact_ctx, ops);
    cJSON_Delete(ops);
    if (!results)
        return EM_OUTCOME_UNKNOWN;
    em_reason checked = em_check_results(results, counts, n);
    cJSON_Delete(results);
    *deleted = 1 + ncredentials;
    return checked;
}

/* GtpStationBackend.remove_station: another station row than the scope's own, deleted with what
 * only it uses (em_station_deletion) */
static em_reason remove_other(em_gtp_scope *g, const char *uuid)
{
    const char *node_uuid;
    if (!em_ovsdb_ready(g->ovs) || !bind(g, &node_uuid))
        return EM_NOT_READY;
    const cJSON *tables = em_ovsdb_tables(g->ovs), *row;
    row = cJSON_GetObjectItemCaseSensitive(em_table(tables, "Wifi_VIF_Config"), uuid);
    const char *mode = ovs_str(row, "mode");
    if (!mode || strcmp(mode, "sta"))
        return EM_NOT_READY; /* "no such station row" */
    cJSON *ops = cJSON_CreateArray();
    int counts[1 + 5 * EM_STATION_DELETION_MAX];
    size_t n = 0;
    cJSON *wait = em_ovs_op("wait", "AWLAN_Node", em_ovs_where_uuid(node_uuid));
    const char *node_cols[] = {"serial_number"};
    cJSON_AddItemToObject(wait, "columns", em_ovs_strings(node_cols, 1));
    cJSON_AddStringToObject(wait, "until", "==");
    cJSON *as_it_is = cJSON_CreateObject();
    cJSON_AddStringToObject(as_it_is, "serial_number", g->serial);
    cJSON_AddItemToArray(cJSON_AddArrayToObject(wait, "rows"), as_it_is);
    cJSON_AddNumberToObject(wait, "timeout", 0);
    cJSON_AddItemToArray(ops, wait);
    counts[n++] = -1;
    const char *uuids[] = {uuid};
    size_t added = em_station_deletion(tables, uuids, 1, ops, counts + n, sizeof(counts) / sizeof(*counts) - n);
    if (!added) {
        cJSON_Delete(ops);
        return EM_NOT_READY;
    }
    n += added;
    cJSON *results = g->transact(g->transact_ctx, ops);
    cJSON_Delete(ops);
    if (!results)
        return EM_OUTCOME_UNKNOWN;
    em_reason checked = em_check_results(results, counts, n);
    cJSON_Delete(results);
    return checked;
}

/* GtpStation._guard_others: on a converted pod, every other station row guarded the same way;
 * true once it removed one (nothing else on this tick) */
static bool guard_others(em_gtp_scope *g, const em_snapshot *snap)
{
    if (!g->guard_all || !snap->ready)
        return false;
    double now = g->monotonic();
    const cJSON *stations = cJSON_GetObjectItemCaseSensitive(snap->config, "stations"), *st;
    /* the tracked ones still there and unassociated, the new unassociated ones added */
    size_t kept = 0;
    for (size_t k = 0; k < g->nothers; k++) {
        bool still = false;
        cJSON_ArrayForEach(st, stations)
        {
            if (!strcmp(str(st, "uuid") ? str(st, "uuid") : "", g->others[k].uuid) &&
                !cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(st, "own")) &&
                !cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(st, "associated")))
                still = true;
        }
        if (still)
            g->others[kept++] = g->others[k];
    }
    g->nothers = kept;
    cJSON_ArrayForEach(st, stations)
    {
        const char *uuid = str(st, "uuid");
        if (!uuid || cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(st, "own")) ||
            cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(st, "associated")))
            continue;
        bool tracked = false;
        for (size_t k = 0; k < g->nothers; k++)
            tracked = tracked || !strcmp(g->others[k].uuid, uuid);
        if (!tracked && g->nothers < sizeof(g->others) / sizeof(*g->others) &&
            em_copy(g->others[g->nothers].uuid, sizeof(g->others[0].uuid), uuid)) /* a UUID: 36 */
            g->others[g->nothers++].since = now;
    }
    for (size_t k = 0; k < g->nothers; k++) {
        if (now - g->others[k].since < g->unassociated)
            continue;
        const char *name = NULL;
        cJSON_ArrayForEach(st, stations)
        {
            if (str(st, "uuid") && !strcmp(str(st, "uuid"), g->others[k].uuid))
                name = str(st, "if_name");
        }
        char station[16];
        EM_FORMAT_FIXED(station, sizeof(station), "%s", name ? name : "?"); /* an interface name: 15 */
        em_reason why = remove_other(g, g->others[k].uuid);
        if (why != EM_OK) {
            em_log(EM_LOG_WARNING, "emosa.agent.gtp_station", "the pod's station %s: not removed: %s", station,
                   em_reason_name(why));
            return true;
        }
        g->others[k] = g->others[--g->nothers];
        g->others_removed++;
        cJSON *payload = cJSON_CreateObject();
        cJSON_AddStringToObject(payload, "station", station);
        cJSON_AddNumberToObject(payload, "unassociated_s", g->unassociated);
        (void)em_journal_event(g->journal, g->run_id, "STATION_REMOVED", payload, g->intent.pod_id);
        cJSON_Delete(payload);
        em_log(EM_LOG_WARNING, "emosa.agent.gtp_station",
               "the pod's station %s (not EMOSA's) was not associated for %d s: removed", station, g->unassociated);
        return true;
    }
    return false;
}

/* GtpStation._guard: true once it removed the station (nothing else on this tick) */
static bool guard(em_gtp_scope *g, const em_snapshot *snap)
{
    const cJSON *config = snap->ready ? snap->config : NULL;
    const char *ssid = str(config, "ssid");
    bool ours = ssid && !strcmp(ssid, g->intent.ssid); /* the station in the scope's form */
    bool associated = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(config, "associated"));
    if (!ours || associated) {
        if (ours)
            g->backoff = 0; /* a write that associated */
        g->unassociated_seen = false;
        return false;
    }
    double now = g->monotonic();
    if (!g->unassociated_seen) {
        g->unassociated_seen = true;
        g->since = now;
    }
    if (now - g->since < g->unassociated)
        return false;
    size_t deleted = 0;
    em_reason why = remove_station(g, &deleted);
    if (why != EM_OK) {
        em_log(EM_LOG_WARNING, "emosa.agent.gtp_station", "the pod's station %s: not removed: %s", g->intent.station,
               em_reason_name(why));
        return true;
    }
    int delay = g->retry[g->backoff < g->nretry ? g->backoff : g->nretry - 1];
    g->attempt++;
    g->backoff++;
    g->removed++;
    g->unassociated_seen = false;
    g->retry_due = true;
    g->retry_at = now + delay;
    cJSON *payload = cJSON_CreateObject();
    cJSON_AddStringToObject(payload, "station", g->intent.station);
    cJSON_AddNumberToObject(payload, "unassociated_s", g->unassociated);
    cJSON_AddNumberToObject(payload, "retry_s", delay);
    (void)em_journal_event(g->journal, g->run_id, "GTP_STATION_REMOVED", payload, g->intent.pod_id);
    cJSON_Delete(payload);
    em_log(EM_LOG_WARNING, "emosa.agent.gtp_station",
           "the pod's station %s was not associated for %d s: removed (%zu rows), written again in %d s",
           g->intent.station, g->unassociated, deleted, delay);
    return true;
}

/* -- GtpStation ---------------------------------------------------------------------------- */

em_reason em_gtp_open(em_gtp_scope *g, const char *state_dir, const em_journal_schemas *schemas,
                      double (*monotonic)(void))
{
    g->monotonic = monotonic;
    if (!g->nretry) { /* GtpStation's defaults: UNASSOCIATED, RETRY */
        g->unassociated = 300;
        g->retry[0] = 900;
        g->retry[1] = 1800;
        g->retry[2] = 3600;
        g->nretry = 3;
    }
    char dir[600];
    EM_FORMAT_FIXED(dir, sizeof(dir), "%s/gtp-station", state_dir); /* EM_STATE_DIR_MAX */
    em_reason why;
    g->journal = em_journal_open(dir, schemas, &why);
    if (!g->journal)
        return why;
    em_engine_init(&g->engine, g->journal, g->vault, g->intent.pod_id, (em_backend){MODE, target, snapshot, plan, submit},
                   g, monotonic);
    em_engine_recover(&g->engine);
    return EM_OK;
}

void em_gtp_close(em_gtp_scope *g)
{
    em_engine_free(&g->engine);
    em_journal_close(g->journal);
    em_snapshot_clear(&g->last);
}

static void wait_for(em_gtp_scope *g, const char *why)
{
    g->has_waiting = why != NULL;
    (void)em_copy(g->waiting, sizeof(g->waiting), why ? why : ""); /* a message */
}

static void settle(em_gtp_scope *g, cJSON *op, const em_snapshot *snap, bool have_snap)
{
    const char *state = em_state_of(op);
    if (strcmp(state, "CONFIG_COMMITTED") && strcmp(state, "INDETERMINATE"))
        return;
    em_reason why;
    cJSON *target = g->engine.backend.target(g, cJSON_GetObjectItemCaseSensitive(op, "intent"), &why);
    const char *planned = str(cJSON_GetObjectItemCaseSensitive(op, "plan"), "instance");
    bool same_start = have_snap && g->has_instance && planned && !strcmp(planned, g->instance);
    if (target && same_start && snap->ready && em_snapshot_satisfies(snap, target) && em_matches(snap->config, target)) {
        cJSON *evidence = em_engine_evidence(snap, false, g->instance);
        if (!strcmp(state, "INDETERMINATE"))
            cJSON_AddStringToObject(evidence, "attribution", "current_condition_only");
        if (em_transition(op, "OBSERVED_APPLIED", evidence)) {
            cJSON_ReplaceItemInObjectCaseSensitive(op, "application_evidence", evidence);
            em_set_reason(op, NULL);
            em_engine_save(&g->engine, op, NULL);
            em_log(EM_LOG_INFO, "emosa.agent.gtp_station", "the pod's station %s is on the GTP's SSID", g->intent.station);
        } else {
            cJSON_Delete(evidence);
        }
    } else if (em_engine_expired(&g->engine, op)) {
        cJSON_ReplaceItemInObjectCaseSensitive(op, "original_outcome", cJSON_CreateString(state));
        cJSON_ReplaceItemInObjectCaseSensitive(op, "deadline_elapsed", cJSON_CreateTrue());
        em_transition(op, "TIMED_OUT", NULL);
        em_set_reason(op, "APPLY_TIMEOUT");
        em_engine_save(&g->engine, op, NULL);
    }
    cJSON_Delete(target);
}

/* GtpStation.start_write's same: the latest write on this start has the credential (fp, its 16
 * characters), at this removal count or later */
typedef struct {
    const em_gtp_scope *g;
    const char *fp;
    size_t prefix;
} gtp_written;

static bool written_at(const char *base, size_t len, const void *ctx)
{
    const gtp_written *w = ctx;
    size_t n = strlen(w->fp);
    if (len < w->prefix + n || strncmp(base + w->prefix, w->fp, n))
        return false;
    const char *rest = base + w->prefix + n, *end = base + len;
    if (rest == end)
        return w->g->attempt == 0;
    if (*rest != ':' || ++rest == end)
        return false;
    unsigned long attempt = 0;
    for (; rest < end; rest++) {
        if (*rest < '0' || *rest > '9')
            return false;
        attempt = attempt * 10 + (unsigned long)(*rest - '0');
        if (attempt > UINT_MAX)
            return true; /* more removals than the count holds: at or past it */
    }
    return attempt >= w->g->attempt;
}

void em_gtp_tick(em_gtp_scope *g)
{
    em_snapshot snap = {0};
    bool have = g->engine.backend.snapshot(g, &snap);
    cJSON *op = em_journal_latest(g->journal);
    if (op)
        settle(g, op, &snap, have);
    cJSON_Delete(op);
    if (!have) {
        em_snapshot_clear(&snap);
        return;
    }
    if (snap.ready && g->has_instance && strcmp(g->start, g->instance)) { /* a new start: its own attempts */
        EM_FORMAT_FIXED(g->start, sizeof(g->start), "%s", g->instance);
        g->attempt = 0;
        g->nothers = 0;
        g->unassociated_seen = g->retry_due = false;
    }
    if (guard(g, &snap) || guard_others(g, &snap)) {
        em_snapshot_clear(&snap);
        return;
    }
    /* GtpStation._wanted */
    op = em_journal_latest(g->journal);
    cJSON *owned = em_journal_ownership(g->journal, g->intent.pod_id);
    /* one write per start, station and credential (GtpStation.start_write: a start EMOSA continues
     * gets one more when the credential changed, and so does going back to an earlier one on that
     * start), one more after each removal */
    char prefix[40], tag[72], key[96] = "", fp[65];
    em_reason fp_why;
    cJSON *rec = record(&g->intent), *t = target(g, rec, &fp_why); /* NULL: an unusable credential */
    if (!t)
        t = cJSON_CreateNull(); /* fingerprinted as the Python agent's "target": None */
    if (!em_engine_fingerprint(&g->engine, rec, t, fp))
        fp[0] = '\0';
    cJSON_Delete(t);
    cJSON_Delete(rec);
    fp[16] = '\0';
    EM_FORMAT_FIXED(prefix, sizeof(prefix), "%s:%s:", g->instance, g->intent.station); /* 16+1+15+1 */
    if (g->attempt)
        EM_FORMAT_FIXED(tag, sizeof(tag), "%s%s:%u", prefix, fp, g->attempt);
    else
        EM_FORMAT_FIXED(tag, sizeof(tag), "%s%s", prefix, fp);
    gtp_written ctx = {g, fp, strlen(prefix)};
    /* this start's latest write, done when it stands for the credential (borrowed: the journal is
     * not written before its last use below) */
    const cJSON *done = em_journal_start_latest(g->journal, SOURCE, g->intent.pod_id, prefix);
    bool wanted = false;
    double now = g->monotonic();
    if (!snap.ready) {
        wait_for(g, "pod not bound");
    } else if (owned) {
        wait_for(g, "another manager changed the station: admit the pod anew");
    } else if (op && em_state_active(em_state_of(op))) {
        wait_for(g, "write in progress");
    } else if (g->retry_due && now < g->retry_at) {
        char text[128];
        EM_FORMAT_FIXED(text, sizeof(text), "removed, not associated: written again in %d s", (int)(g->retry_at - now));
        wait_for(g, text);
    } else if (em_start_write(done, tag, written_at, &ctx, key, sizeof(key))) {
        if (!strcmp(em_state_of(done), "OBSERVED_APPLIED")) {
            wait_for(g, NULL);
        } else {
            char text[128];
            const char *reason = str(done, "reason");
            (void)em_format(text, sizeof(text), "not written on this start: %s%s%s", em_state_of(done), /* a message */
                            reason ? " " : "", reason ? reason : "");
            wait_for(g, text);
        }
    } else if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(snap.config, "present"))) {
        char text[128]; /* the pod's, an operator's or a start-up hook's station (spec 3.10) */
        EM_FORMAT_FIXED(text, sizeof(text), "%s has a row: left alone", g->intent.station);
        wait_for(g, text);
    } else {
        wait_for(g, NULL);
        wanted = true;
    }
    cJSON_Delete(owned);
    cJSON_Delete(op);
    em_snapshot_clear(&snap);
    if (!wanted)
        return;
    cJSON *intent = record(&g->intent);
    em_reason why;
    op = em_engine_request(&g->engine, intent, SOURCE, key, g->run_id, DEADLINE, "semantic", NULL, &why);
    cJSON_Delete(intent);
    if (op && !strcmp(em_state_of(op), "REQUESTED")) {
        cJSON *result = em_engine_execute(&g->engine,
                                          cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(op, "operation_id")));
        const char *reason = str(result, "reason");
        em_log(EM_LOG_INFO, "emosa.agent.gtp_station", "GTP station %s: %s %s", str(result, "operation_id"),
               em_state_of(result), reason ? reason : "");
        cJSON_Delete(result);
    }
    cJSON_Delete(op);
}

cJSON *em_gtp_status(em_gtp_scope *g)
{
    cJSON *o = cJSON_CreateObject(), *op = em_journal_latest(g->journal);
    cJSON_AddStringToObject(o, "station", g->intent.station);
    cJSON_AddStringToObject(o, "ssid", g->intent.ssid);
    cJSON_AddBoolToObject(o, "pinned", *g->intent.bssid != 0);
    if (g->has_last) {
        cJSON_AddBoolToObject(o, "present", cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(g->last.config, "present")));
        cJSON_AddBoolToObject(o, "joined", cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(g->last.config, "joined")));
        cJSON_AddBoolToObject(o, "associated",
                              cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(g->last.config, "associated")));
    } else {
        cJSON_AddNullToObject(o, "present");
        cJSON_AddNullToObject(o, "joined");
        cJSON_AddNullToObject(o, "associated");
    }
    cJSON_AddNumberToObject(o, "removed_unassociated", g->removed);
    cJSON_AddNumberToObject(o, "others_removed_unassociated", g->others_removed);
    cJSON_AddItemToObject(o, "waiting", g->has_waiting ? cJSON_CreateString(g->waiting) : cJSON_CreateNull());
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
