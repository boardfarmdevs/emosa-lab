/* SPDX-License-Identifier: Apache-2.0 */
/* The pod scopes' shared reads. */
#include "scope.h"

#include <ctype.h>
#include <stdlib.h>
#include <string.h>

#include "canon.h"
#include "ovs.h"

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
    char *text = em_json_dumps(list, EM_JSON_DEFAULT, false), hex[65];
    cJSON_Delete(list);
    em_sha256_hex(text, strlen(text), hex);
    free(text);
    memcpy(out, hex, 16);
    out[16] = 0;
    return true;
}

bool em_row_wpa2_psk(const cJSON *row)
{
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
