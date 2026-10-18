/* SPDX-License-Identifier: Apache-2.0 */
/* What every pod scope reads the same way (emosa.opensync.mapping, .uplink): the bound
 * pod (one AWLAN_Node with the configured serial), which start of its OpenSync, and the
 * row tests the scopes share. Tables are the monitor cache, {table: {uuid: row}}. */
#ifndef EMOSA_SCOPE_H
#define EMOSA_SCOPE_H

#include <cjson/cJSON.h>

#include "common.h"

const cJSON *em_table(const cJSON *tables, const char *name);
/* The pod's one AWLAN_Node row when its serial is `serial` (*uuid its UUID), else NULL. */
const cJSON *em_bound_node(const cJSON *tables, const char *serial, const char **uuid);
/* start_instance: sha256(json.dumps(sorted Wifi_Radio_Config UUIDs))[:16]; false without radios.
 * A legacy pod's agent adds its start marker's token (false without one: not ready), or without
 * the marker its session's start token (em_new_session_start). */
bool em_start_instance(const cJSON *tables, char out[17]);
/* new_session_start (spec 3.10): from now on, a new start of a legacy pod's OpenSync for every
 * scope (the ownership period, the writes made once per start); false without randomness. */
bool em_new_session_start(void);
/* spec 3.10 (uplink.START_MODULE, use_start_marker): a legacy pod's start marker, a Node_Config
 * row of EMOSA's own module with a random token, never persist; no seed has it and none of the
 * pod's managers reads it. A re-seed or a reboot loses it, an EMOSA restart reads it back. */
#define EM_START_MODULE "emosa"
#define EM_START_KEY "start"
void em_use_start_marker(bool on);
bool em_start_marker_used(void);
/* The marker's token, or NULL (start_marker). */
const char *em_start_marker(const cJSON *tables);
/* The marker in use and none yet: the pod is not ready for any scope (start_pending). */
bool em_start_pending(const cJSON *tables);
/* The marker's guarded insert, only where the pod has none (start_marker_ops): the caller's. */
cJSON *em_start_marker_ops(const char *token);
/* The 6.6 osw encoding of WPA2-PSK/CCMP with one PSK slot, or on a legacy pod (spec 3.10) the
 * security map's encryption WPA-PSK and a key (radio_scope.wpa2_psk). */
bool em_row_wpa2_psk(const cJSON *row);
/* A legacy pod's VIF row (spec 3.10): no wpa_* columns (radio_scope.legacy_row). */
bool em_row_legacy(const cJSON *row);
/* The row's one passphrase: its single wpa_psks slot, or a legacy row's security key (psk). */
const char *em_row_psk(const cJSON *row);
/* The row's UUID set (or single UUID) column contains uuid. */
bool em_row_has_uuid(const cJSON *row, const char *column, const char *uuid);
/* A string column as a JSON value: the string, or null (absent or empty set). */
cJSON *em_row_string(const cJSON *row, const char *column);
/* A boolean column as a JSON value: true/false, or null when absent. */
cJSON *em_row_bool(const cJSON *row, const char *column);
/* The one value of a one-entry map column (wpa_psks), else NULL. */
const char *em_row_sole_map_value(const cJSON *row, const char *column);
/* lower-case "aa:bb:.." copy of a MAC column; false when absent. */
bool em_row_mac(const cJSON *row, const char *column, char out[18]);

/* emosa.opensync.stations.deletion (spec 3.10): the operations deleting the station rows `uuids`
 * (n of them) of `tables`, appended to ops in order: each row waited on as it is (if_name, mode,
 * ssid), its radios' vif_configs references, the rows, the credentials no other VIF links and
 * their Inet rows (no other VIF of the name). Each operation's count goes to counts (-1: none),
 * at most max_counts; the number appended, or 0 when they do not fit. */
#define EM_STATION_DELETION_MAX 64 /* station rows, credentials or references in one deletion */
size_t em_station_deletion(const cJSON *tables, const char *const *uuids, size_t n, cJSON *ops, int *counts,
                           size_t max_counts);

#endif
