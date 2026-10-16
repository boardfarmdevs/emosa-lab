/* SPDX-License-Identifier: Apache-2.0 */
/* The pod scopes' OVSDB writes (spec §3.2, §3.4, §3.7): the AP scope (M2 → VIF rows,
 * as emosa.opensync.pod_profile) and the steering window (emosa.opensync.steering). */
#ifndef EMOSA_SOUTHBOUND_H
#define EMOSA_SOUTHBOUND_H

#include <cjson/cJSON.h>

#include "common.h"

/* A pod profile (spec §3.5, schemas/pod-profile.schema.json). */
typedef struct {
    cJSON *doc; /* owns the values below */
    const char *id, *band, *ht_mode, *fronthaul_if, *uplink_station;
    int channel;
    const cJSON *fronthaul_vif, *backhaul_vif, *inet;
    size_t nslots;
    struct {
        const char *if_name, *role;
        int vif_radio_idx;
        const char *band; /* the radio its VIF is on (spec 9); NULL: the fronthaul's `band` */
    } slots[8];
    /* radios: the further radios EMOSA binds beyond the fronthaul's (spec 9), by band ("5G",
     * "6G"), each its own write scope configured by its own M2 */
    size_t nradios;
    const char *radio_bands[2];
    /* a radio the profile places (spec 9, a fronthaul with no station): its declared channel (0:
     * none, the radio follows its station) and width (NULL: none), written with its slots */
    int radio_channels[2];
    const char *radio_ht_modes[2];
    bool has_esp_be;
    uint8_t esp_be[3]; /* ap_metrics.esp_be: the declared best-effort ESP */
    /* uplink.stations: the pod's backhaul station on each band ("2.4G", "5G", "6G"), for a move
     * to a BSS on another band (spec 8.3) */
    size_t nuplink_bands;
    const char *uplink_bands[3], *uplink_band_stations[3];
    /* backhaul_mode: "multi-ap" (spec 8.5) or "gre-parent" (spec 8.6), and the parent AP's
     * overrides of its VIF and Inet rows (gre_parent.vif, .inet; NULL when absent) */
    const char *backhaul_mode;
    const cJSON *gre_parent_vif, *gre_parent_inet;
    /* schema "opensync-2.0" (spec 3.10): a legacy pod, its WPA2-PSK in the security map, no
     * multi_ap, the roles the profile's slots */
    bool legacy;
    /* a legacy pod's WAN bridge, its cm's uplink bridge (spec 8.4): wan_bridge, "br-wan" absent */
    const char *wan_bridge;
    /* radio_plan "emosa" (spec 3.10): the Inet rows of the APs the profile does not name off (the
     * APs left on), the fronthaul radio on the profile's channel while wired, once per start;
     * false: the pod's ("pod") */
    bool radio_plan;
    /* idle_radios (with radio_plan): the radios the profile neither binds nor places, by band, each
     * with the channel and width the plan turns it on at when it carries an enabled AP and wm
     * shows it off or with no channel */
    size_t nidle;
    const char *idle_bands[2], *idle_ht_modes[2];
    int idle_channels[2];
} em_profile;

em_reason em_profile_load(const char *path, em_profile *out);
void em_profile_free(em_profile *p);
/* The slot VIF `name` is the pod's GRE parent AP: a gre-parent profile's backhaul slot, on any
 * of the pod's radios. */
bool em_profile_parent_slot(const em_profile *p, const char *name);
/* A view of the profile whose slots are those on the radio of `band` (pod_profile.slots_on),
 * none without multi_bss; it shares p's document and is never freed itself. */
em_profile em_profile_slots_on(const em_profile *p, const char *band, bool multi_bss);
/* `band` is one of the profile's further radios (spec 9). */
bool em_profile_has_radio(const em_profile *p, const char *band);

/* The pod's OVSDB, as the scopes use it: a snapshot of raw tables and transact. */
typedef struct {
    const cJSON *tables; /* {table: {uuid: row}} */
    int generation;
    /* Returns the results array (owned by the caller), or NULL when the reply was lost. */
    cJSON *(*transact)(void *ctx, const cJSON *operations);
    void *ctx;
} em_ovs_session;

/* Passphrases by reference (the secret store). */
typedef const char *(*em_secret_resolver)(void *ctx, const char *ref);

typedef struct {
    const char *ssid, *secret_ref;
    size_t nadditional;
    struct {
        const char *role, *ssid, *secret_ref;
    } additional[8];
    bool has_additional;
} em_ap_intent;

/* "committed", "conflict", "rejected" or "unknown", like SubmitResult. */
typedef struct {
    char status[12];
    em_reason reason;
} em_submit_result;

/* Plan and submit one M2 intent on the bound fronthaul (and, multi_bss, the slots). A GRE
 * parent's backhaul BSS (spec 8.6) needs the pod's `underlay` (169.254.N.0/24; NULL: none,
 * and such a set is refused). */
em_reason em_ap_submit(const em_profile *profile, const char *serial, bool multi_bss,
                       const char *underlay, em_ovs_session *session, const em_ap_intent *intent,
                       em_secret_resolver resolve, void *resolve_ctx, em_submit_result *out);

/* spec 3.10 (PodBackend AUTH_RELOAD): a legacy pod's wm restarts its authenticator (nas and eapd)
 * in full only when an AP's security changes in more than its ssid and key (its oftag, say); a new
 * ssid or key alone is a fast reload. EMOSA's write that makes an AP, changes its ssid or key, or
 * puts it into its bridge gives the oftag this suffix, and once the pod's State shows that write
 * (and the AP is in its bridge) the oftag is restored: two full reloads, the last with the port. */
#define EM_AUTH_RELOAD "~reload"

/* PodBackend.auth_reload: the guarded operations giving each legacy AP whose oftag carries
 * EM_AUTH_RELOAD its oftag back once its State shows that write (its ssid, key and oftag), it
 * needs no bridge port (em_bridge_port_ops) and the pod lacks none of its bridge mode
 * (em_bridge_mode_ops), the APs' names in names (at most max), *n their count; NULL when there is
 * none. */
cJSON *em_auth_reload_ops(const em_profile *p, bool multi_bss, const cJSON *tables, char (*names)[16], size_t max,
                          size_t *n);

/* PodBackend.bridge_ports (spec 3.10): a legacy pod's wm and authenticator hear an AP's stations
 * on its bridge, whose members are the bridge's Open vSwitch ports, and the pod never adds an AP
 * there (its cloud writes the Port). The guarded operations giving each AP of the profile's (the
 * fronthaul, a slot with multi_bss) that wm has taken (its State), that is on and names the pod's
 * one Bridge of that name, and that has no Port of its name anywhere: an Interface and a Port of
 * its name, the Port into the Bridge's ports, its oftag marked (EM_AUTH_RELOAD) unless it is. The
 * APs' names in names (at most max, EM_BRIDGE_PORTS_MAX), *n their count, the results' expected
 * counts in counts (-1 unchecked; room for EM_BRIDGE_PORTS_MAX * 7), *ncounts theirs; NULL when
 * there is none. */
#define EM_BRIDGE_PORTS_MAX 8

/* PodBackend.bridge_mode (spec 3.10): the bridge mode a converted pod's cloud runs (a legacy
 * profile with radio_plan emosa), when the pod lacks any of it: the home bridge's Inet row up
 * (enabled, network, NAT off; where wm and the authenticator listen), the WAN bridge's NAT off,
 * OpenSync's patch pair when neither of its Ports exists, the home APs' group_rekey and ft_psk as
 * the profile has them, and their oftags marked (EM_AUTH_RELOAD) unless they are, so the
 * authenticator restarts with all of it in place. What it sets in done ("; "-joined), the results'
 * expected counts in counts (-1 unchecked; room for EM_BRIDGE_MODE_COUNTS), *ncounts theirs; NULL
 * when there is nothing to set or the pod has no Inet row for either bridge. */
#define EM_BRIDGE_MODE_COUNTS 48
cJSON *em_bridge_mode_ops(const em_profile *p, bool multi_bss, const cJSON *tables, char *done, size_t size,
                          int *counts, size_t *ncounts);
cJSON *em_bridge_port_ops(const em_profile *p, bool multi_bss, const cJSON *tables, char (*names)[16], size_t max,
                          size_t *n, int *counts, size_t *ncounts);

/* A further radio's scope (spec 9, RadioBackend.submit): make its slot VIFs on the radio of
 * `band` exactly the intent's additional BSSes (the BSSes of that radio's own M2 set fitted to
 * its slots; intent->ssid and secret_ref unused). Guards the pod and the radio's if_name only,
 * writes no channel or ht_mode, and inserts or deletes only its own vif_configs references. */
em_reason em_radio_submit(const em_profile *profile, const char *serial, const char *band, bool multi_bss,
                          const char *underlay, em_ovs_session *session, const em_ap_intent *intent,
                          em_secret_resolver resolve, void *resolve_ctx, em_submit_result *out);

/* One steering mandate on the pod (spec §3.7). */
typedef struct {
    char station[18], source_bssid[18], target_bssid[18];
    int op_class, channel, window;
    bool disassoc_imminent;
} em_steering_intent;

typedef struct {
    char client[37], neighbor[37], group[37]; /* UUIDs the opening inserted ("" if none) */
} em_steering_rows;

em_reason em_steering_open(const char *serial, em_ovs_session *session,
                           const em_steering_intent *intent, em_submit_result *result,
                           em_steering_rows *created);
em_reason em_steering_kick(const char *serial, em_ovs_session *session, const char *station,
                           const char *client_row);
em_reason em_steering_close(em_ovs_session *session, const em_steering_intent *intent,
                            const em_steering_rows *created);

/* The probe watch's row (cs_mode off, cs_params {"emosa": "watch"}): a steering
 * window replaces it. */
bool em_is_watch_row(const cJSON *row);

/* The option 1 uplink switch (spec §8.3): the station joins the EasyMesh backhaul
 * BSS `bssid` with `ssid`; each other of the pod's backhaul stations (`stations`) that is
 * enabled is disabled in the same transaction. On a refusal, *refusal names why (no
 * transaction sent). */
#define EM_UPLINK_STATIONS 4
typedef struct {
    const char *station, *ssid, *secret_ref, *bssid;
    const char *const *stations; /* the pod's backhaul stations (any order; may hold `station`) */
    size_t nstations;
    const char *onboard; /* the credential's onboard_type: NULL/"multi_ap" (default), or "gre" (spec 8.6) */
    /* a legacy pod (spec 3.10): the credential without priority, enabled or bssid; the pin in the
     * station's parent, guarded and snapshotted for the revert */
    bool legacy;
} em_uplink_intent;

/* `restore` (optional out): when moving from the bootstrap (the pre-move station in vconf mode),
 * the pod columns this move overwrites, for the revert to put back; NULL otherwise. The caller owns
 * it. */
em_reason em_uplink_submit(const char *serial, em_ovs_session *session, const em_uplink_intent *in,
                           em_secret_resolver resolve, void *resolve_ctx, em_submit_result *out,
                           const char **refusal, cJSON **restore);

/* The revert (spec 8.6): put the station's snapshotted vconf columns (`restore`) back and delete the
 * credential EMOSA inserted (the one linked now that `restore` does not keep), within EMOSA's own
 * columns; the pod's parent pin stands. */
em_reason em_uplink_revert(const char *serial, em_ovs_session *session, const char *station,
                           const cJSON *restore, em_submit_result *out, const char **refusal);

/* Transaction pieces (emosa.opensync.mapping): where clauses, one operation, a string
 * array, a wait for a row's current values (guard), a wait for no matching row. */
cJSON *em_ovs_where_uuid(const char *uuid);
cJSON *em_ovs_where_eq(const char *column, const char *value);
cJSON *em_ovs_op(const char *kind, const char *table, cJSON *where);
cJSON *em_ovs_strings(const char *const *items, size_t n);
cJSON *em_ovs_guard(const char *table, const char *uuid, const cJSON *row, const char *const *columns, size_t n);
cJSON *em_ovs_absent(const char *table, cJSON *where, const char *column);

/* check_results: every result an object without "error"; counts where >= 0. */
em_reason em_check_results(const cJSON *results, const int *counts, size_t n);

#endif
