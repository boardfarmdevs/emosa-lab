/* SPDX-License-Identifier: Apache-2.0 */
/* The pod scopes' shared reads. */
#include "scope.h"

#include <ctype.h>
#include <openssl/rand.h>
#include <stdlib.h>
#include <string.h>

#include "canon.h"
#include "ovs.h"
#include "southbound.h"

/* spec 3.10 (uplink._session_start, _start_marker): a legacy pod's start, its start marker's token
 * (start_marker on), else the agent's own token renewed for every fresh session (a schema
 * without Node_Config); empty and off on a 6.6 pod. One agent process serves one pod. */
static char session_start[17];
static bool start_marker_on;

const cJSON *em_table(const cJSON *tables, const char *name)
{
    return cJSON_GetObjectItemCaseSensitive(tables, name);
}

const cJSON *em_bound_node(const cJSON *tables, const char *serial, const char **uuid)
{
    const cJSON *nodes = em_table(tables, "AWLAN_Node");
    if (cJSON_GetArraySize(nodes) != 1)
        return NULL;
    const cJSON *node = nodes->child;
    const char *have = ovs_str(node, "serial_number");
    if (!have || !serial || strcmp(have, serial))
        return NULL;
    if (uuid)
        *uuid = node->string;
    return node;
}

static int by_text(const void *a, const void *b) { return strcmp(*(const char *const *)a, *(const char *const *)b); }

bool em_start_instance(const cJSON *tables, char out[17])
{
    const cJSON *radios = em_table(tables, "Wifi_Radio_Config"), *r;
    size_t n = (size_t)cJSON_GetArraySize(radios), i = 0;
    if (!n)
        return false; /* "the pod's radios are not configured yet" */
    const char **uuids = em_malloc(n * sizeof(*uuids));
    cJSON_ArrayForEach(r, radios) uuids[i++] = r->string;
    em_sort(uuids, n, sizeof(*uuids), by_text);
    cJSON *list = cJSON_CreateArray();
    for (i = 0; i < n; i++)
        cJSON_AddItemToArray(list, cJSON_CreateString(uuids[i]));
    free(uuids);
    if (start_marker_on) { /* a legacy pod's: its start marker's token, none yet: not ready */
        const char *token = em_start_marker(tables);
        if (!token) {
            cJSON_Delete(list);
            return false; /* "the pod's start marker is not written yet" */
        }
        cJSON_AddItemToArray(list, cJSON_CreateString(token));
    } else if (session_start[0]) { /* without the marker: its session's start (spec 3.10) */
        cJSON_AddItemToArray(list, cJSON_CreateString(session_start));
    }
    char *text = em_json_dumps(list, EM_JSON_DEFAULT, false), hex[65];
    cJSON_Delete(list);
    em_sha256_hex(text, strlen(text), hex);
    free(text);
    memcpy(out, hex, 16);
    out[16] = 0;
    return true;
}

bool em_new_session_start(void)
{
    uint8_t token[8];
    if (RAND_bytes(token, sizeof(token)) != 1)
        return false;
    char *hex = em_hex(token, sizeof(token));
    EM_FORMAT_FIXED(session_start, sizeof(session_start), "%s", hex);
    free(hex);
    return true;
}

void em_use_start_marker(bool on) { start_marker_on = on; }

bool em_start_marker_used(void) { return start_marker_on; }

const char *em_start_marker(const cJSON *tables)
{
    const cJSON *row;
    cJSON_ArrayForEach(row, em_table(tables, "Node_Config"))
    {
        const char *module = ovs_str(row, "module"), *key = ovs_str(row, "key");
        if (module && key && !strcmp(module, EM_START_MODULE) && !strcmp(key, EM_START_KEY)) {
            const char *value = ovs_str(row, "value");
            return value && *value ? value : NULL;
        }
    }
    return NULL;
}

bool em_start_pending(const cJSON *tables) { return start_marker_on && !em_start_marker(tables); }

cJSON *em_start_marker_ops(const char *token)
{
    cJSON *ops = cJSON_CreateArray(), *where = cJSON_CreateArray();
    const char *const pairs[2][2] = {{"module", EM_START_MODULE}, {"key", EM_START_KEY}};
    for (int i = 0; i < 2; i++) {
        cJSON *cond = cJSON_CreateArray();
        cJSON_AddItemToArray(cond, cJSON_CreateString(pairs[i][0]));
        cJSON_AddItemToArray(cond, cJSON_CreateString("=="));
        cJSON_AddItemToArray(cond, cJSON_CreateString(pairs[i][1]));
        cJSON_AddItemToArray(where, cond);
    }
    cJSON *wait = cJSON_CreateObject();
    cJSON_AddStringToObject(wait, "op", "wait");
    cJSON_AddStringToObject(wait, "table", "Node_Config");
    cJSON_AddItemToObject(wait, "where", where);
    cJSON_AddItemToArray(cJSON_AddArrayToObject(wait, "columns"), cJSON_CreateString("module"));
    cJSON_AddStringToObject(wait, "until", "==");
    cJSON_AddArrayToObject(wait, "rows");
    cJSON_AddNumberToObject(wait, "timeout", 0);
    cJSON_AddItemToArray(ops, wait);
    cJSON *insert = cJSON_CreateObject(), *row = cJSON_CreateObject();
    cJSON_AddStringToObject(insert, "op", "insert");
    cJSON_AddStringToObject(insert, "table", "Node_Config");
    cJSON_AddStringToObject(row, "module", EM_START_MODULE);
    cJSON_AddStringToObject(row, "key", EM_START_KEY);
    cJSON_AddStringToObject(row, "value", token);
    cJSON_AddItemToObject(insert, "row", row);
    cJSON_AddItemToArray(ops, insert);
    return ops;
}

void em_start_marker_error(em_reason reason, const cJSON *results, char *out, size_t size)
{
    const cJSON *result, *list = cJSON_IsArray(results) ? results : NULL;
    cJSON_ArrayForEach(result, list)
    {
        const cJSON *error = cJSON_GetObjectItemCaseSensitive(result, "error");
        if (cJSON_IsString(error) && *error->valuestring) {
            EM_FORMAT_FIXED(out, size, "%s: %.64s", em_reason_name(reason), error->valuestring);
            return;
        }
    }
    EM_FORMAT_FIXED(out, size, "%s", em_reason_name(reason));
}

bool em_row_legacy(const cJSON *row)
{
    return row && !cJSON_GetObjectItemCaseSensitive(row, "wpa") && !cJSON_GetObjectItemCaseSensitive(row, "wpa_psks");
}

const char *em_row_psk(const cJSON *row)
{
    return em_row_legacy(row) ? ovs_map_get(row, "security", "key") : em_row_sole_map_value(row, "wpa_psks");
}

bool em_row_wpa2_psk(const cJSON *row)
{
    if (em_row_legacy(row)) { /* spec 3.10: the security map's encryption WPA-PSK and a key */
        const char *encryption = ovs_map_get(row, "security", "encryption"), *key = ovs_map_get(row, "security", "key");
        return encryption && !strcmp(encryption, "WPA-PSK") && key && *key;
    }
    const char *akm[8];
    size_t n = ovs_strings(row, "wpa_key_mgmt", akm, 8);
    return ovs_true(row, "wpa") && n == 1 && !strcmp(akm[0], "wpa-psk") && ovs_true(row, "rsn_pairwise_ccmp") &&
           ovs_map_size(row, "security") == 0 && !ovs_true(row, "wpa_pairwise_tkip") &&
           !ovs_true(row, "wpa_pairwise_ccmp") && ovs_map_size(row, "wpa_psks") == 1;
}

bool em_row_has_uuid(const cJSON *row, const char *column, const char *uuid)
{
    const char *uuids[64];
    size_t n = ovs_uuids(row, column, uuids, 64);
    for (size_t i = 0; uuid && i < n; i++)
        if (!strcmp(uuids[i], uuid))
            return true;
    return false;
}

cJSON *em_row_string(const cJSON *row, const char *column)
{
    const char *v = row ? ovs_str(row, column) : NULL;
    return v ? cJSON_CreateString(v) : cJSON_CreateNull();
}

cJSON *em_row_bool(const cJSON *row, const char *column)
{
    const cJSON *v = row ? cJSON_GetObjectItemCaseSensitive(row, column) : NULL;
    return cJSON_IsBool(v) ? cJSON_CreateBool(cJSON_IsTrue(v)) : cJSON_CreateNull();
}

const char *em_row_sole_map_value(const cJSON *row, const char *column)
{
    const char *keys[2];
    if (!row || ovs_map_keys(row, column, keys, 2) != 1)
        return NULL;
    return ovs_map_get(row, column, keys[0]);
}

bool em_row_mac(const cJSON *row, const char *column, char out[18])
{
    const char *v = row ? ovs_str(row, column) : NULL;
    if (!v || !*v || strlen(v) != 17)
        return false;
    for (int i = 0; i < 17; i++)
        out[i] = (char)tolower((unsigned char)v[i]);
    out[17] = 0;
    return true;
}

/* -- deleting station rows (emosa.opensync.stations) ------------------------------------------ */

static int text_order(const void *a, const void *b) { return strcmp(*(const char *const *)a, *(const char *const *)b); }

static bool listed(const char *const *items, size_t n, const char *item)
{
    for (size_t k = 0; k < n; k++)
        if (item && items[k] && !strcmp(items[k], item))
            return true;
    return false;
}

typedef struct {
    const char *name, *uuid;
} named_row;

static int named_order(const void *a, const void *b)
{
    const named_row *x = a, *y = b;
    int k = strcmp(x->name, y->name);
    return k ? k : strcmp(x->uuid, y->uuid);
}

typedef struct {
    const char *radio, *vif;
} reference;

static int reference_order(const void *a, const void *b)
{
    const reference *x = a, *y = b;
    int k = strcmp(x->radio, y->radio);
    return k ? k : strcmp(x->vif, y->vif);
}

static cJSON *empty_set(void)
{
    cJSON *set = cJSON_CreateArray();
    cJSON_AddItemToArray(set, cJSON_CreateString("set"));
    cJSON_AddItemToArray(set, cJSON_CreateArray());
    return set;
}

static cJSON *uuid_set(const char *uuid)
{
    cJSON *set = cJSON_CreateArray(), *members = cJSON_CreateArray(), *ref = cJSON_CreateArray();
    cJSON_AddItemToArray(ref, cJSON_CreateString("uuid"));
    cJSON_AddItemToArray(ref, cJSON_CreateString(uuid));
    cJSON_AddItemToArray(members, ref);
    cJSON_AddItemToArray(set, cJSON_CreateString("set"));
    cJSON_AddItemToArray(set, members);
    return set;
}

size_t em_station_deletion(const cJSON *tables, const char *const *uuids, size_t n, cJSON *ops, int *counts,
                           size_t max_counts)
{
    enum { MAX = EM_STATION_DELETION_MAX };
    const cJSON *vifs = em_table(tables, "Wifi_VIF_Config"), *credentials = em_table(tables, "Wifi_Credential_Config");
    named_row gone[MAX];
    size_t ngone = 0;
    for (size_t k = 0; k < n && ngone < MAX; k++) {
        const cJSON *row = cJSON_GetObjectItemCaseSensitive(vifs, uuids[k]);
        const char *name = ovs_str(row, "if_name");
        bool twice = false;
        for (size_t j = 0; j < ngone; j++)
            twice = twice || !strcmp(gone[j].uuid, uuids[k]);
        if (row && !twice)
            gone[ngone++] = (named_row){name ? name : "", row->string};
    }
    qsort(gone, ngone, sizeof(*gone), named_order);
    const char *gone_uuids[MAX], *gone_names[MAX];
    for (size_t k = 0; k < ngone; k++) {
        gone_uuids[k] = gone[k].uuid;
        gone_names[k] = gone[k].name;
    }
    /* what the kept rows use */
    const char *kept_refs[MAX], *kept_names[MAX];
    size_t nkept_refs = 0, nkept_names = 0;
    const cJSON *r;
    cJSON_ArrayForEach(r, vifs)
    {
        if (listed(gone_uuids, ngone, r->string))
            continue;
        if (nkept_names < MAX)
            kept_names[nkept_names++] = ovs_str(r, "if_name");
        nkept_refs += ovs_uuids(r, "credential_configs", kept_refs + nkept_refs, MAX - nkept_refs);
    }
    const char *creds[MAX], *inets[MAX];
    size_t ncreds = 0, ninets = 0, nrefs = 0;
    for (size_t k = 0; k < ngone; k++) {
        const char *refs[MAX];
        size_t m = ovs_uuids(cJSON_GetObjectItemCaseSensitive(vifs, gone_uuids[k]), "credential_configs", refs, MAX);
        for (size_t j = 0; j < m; j++)
            if (cJSON_GetObjectItemCaseSensitive(credentials, refs[j]) && !listed(kept_refs, nkept_refs, refs[j]) &&
                !listed(creds, ncreds, refs[j]) && ncreds < MAX)
                creds[ncreds++] = refs[j];
    }
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_Inet_Config"))
    {
        const char *name = ovs_str(r, "if_name");
        if (listed(gone_names, ngone, name) && !listed(kept_names, nkept_names, name) && ninets < MAX)
            inets[ninets++] = r->string;
    }
    reference refs[MAX];
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_Radio_Config"))
    {
        const char *members[MAX];
        size_t m = ovs_uuids(r, "vif_configs", members, MAX);
        for (size_t j = 0; j < m; j++)
            if (listed(gone_uuids, ngone, members[j]) && nrefs < MAX)
                refs[nrefs++] = (reference){r->string, members[j]};
    }
    qsort(creds, ncreds, sizeof(*creds), text_order);
    qsort(inets, ninets, sizeof(*inets), text_order);
    qsort(refs, nrefs, sizeof(*refs), reference_order);
    size_t total = ngone * 2 + nrefs + ncreds + ninets;
    if (total > max_counts)
        return 0;
    size_t c = 0;
    const char *cols[] = {"if_name", "mode", "ssid"};
    for (size_t k = 0; k < ngone; k++) {
        const cJSON *row = cJSON_GetObjectItemCaseSensitive(vifs, gone_uuids[k]);
        cJSON *as_it_is = cJSON_CreateObject();
        const char *name = ovs_str(row, "if_name"), *ssid = ovs_str(row, "ssid");
        cJSON_AddItemToObject(as_it_is, "if_name", name ? cJSON_CreateString(name) : empty_set());
        cJSON_AddStringToObject(as_it_is, "mode", "sta");
        cJSON_AddItemToObject(as_it_is, "ssid", ssid ? cJSON_CreateString(ssid) : empty_set());
        cJSON *wait = em_ovs_op("wait", "Wifi_VIF_Config", em_ovs_where_uuid(gone_uuids[k]));
        cJSON_AddItemToObject(wait, "columns", em_ovs_strings(cols, 3));
        cJSON_AddStringToObject(wait, "until", "==");
        cJSON_AddItemToArray(cJSON_AddArrayToObject(wait, "rows"), as_it_is);
        cJSON_AddNumberToObject(wait, "timeout", 0);
        cJSON_AddItemToArray(ops, wait);
        counts[c++] = -1;
    }
    for (size_t k = 0; k < nrefs; k++) {
        cJSON *mutate = em_ovs_op("mutate", "Wifi_Radio_Config", em_ovs_where_uuid(refs[k].radio));
        cJSON *mutation = cJSON_CreateArray();
        cJSON_AddItemToArray(mutation, cJSON_CreateString("vif_configs"));
        cJSON_AddItemToArray(mutation, cJSON_CreateString("delete"));
        cJSON_AddItemToArray(mutation, uuid_set(refs[k].vif));
        cJSON_AddItemToArray(cJSON_AddArrayToObject(mutate, "mutations"), mutation);
        cJSON_AddItemToArray(ops, mutate);
        counts[c++] = 1;
    }
    for (size_t k = 0; k < ngone; k++) {
        cJSON_AddItemToArray(ops, em_ovs_op("delete", "Wifi_VIF_Config", em_ovs_where_uuid(gone_uuids[k])));
        counts[c++] = 1;
    }
    for (size_t k = 0; k < ncreds; k++) {
        cJSON_AddItemToArray(ops, em_ovs_op("delete", "Wifi_Credential_Config", em_ovs_where_uuid(creds[k])));
        counts[c++] = 1;
    }
    for (size_t k = 0; k < ninets; k++) {
        cJSON_AddItemToArray(ops, em_ovs_op("delete", "Wifi_Inet_Config", em_ovs_where_uuid(inets[k])));
        counts[c++] = 1;
    }
    return c;
}
