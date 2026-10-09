/* SPDX-License-Identifier: Apache-2.0 */
/* The uplink scope, as emosa.opensync.uplink.UplinkBackend and emosa.agent.uplink.
 * UplinkSwitch: the pod's backhaul station joins the EasyMesh backhaul BSS (data plane
 * option 1), pinned to one upstream BSSID, once per OpenSync start, and its other backhaul
 * stations are disabled; a failed switch holds the pod on option 2; the controller's Backhaul
 * Steering moves it (steer, outcome), to another band with that band's station, the target
 * kept in <state_dir>/uplink/target.json. Its own journal. */
#ifndef EMOSA_SCOPE_UPLINK_H
#define EMOSA_SCOPE_UPLINK_H

#include "engine.h"
#include "ovsdb.h"
#include "view.h"

#define EM_UPLINK_BANDS 3

typedef struct {
    em_ovsdb *ovs;
    const char *serial, *pod_id, *run_id;
    const char *station; /* the backhaul station the pod bootstraps on */
    /* the pod's backhaul station on each band ("2.4G", "5G", "6G"; the profile's): a move to a
     * BSS on another band uses that band's, and the switch disables the others (spec 8.3) */
    const char *bands[EM_UPLINK_BANDS], *band_stations[EM_UPLINK_BANDS];
    size_t nbands;
    /* the band of the radio that carries the pod's BSSes: a station on it keeps its channel */
    const char *fixed_band;
    em_vault *vault;
    cJSON *(*transact)(void *ctx, const cJSON *operations);
    void *transact_ctx;
    double (*monotonic)(void);
    /* credentials: fixed (configuration) or the AP scope's applied M2 set */
    bool fixed_credentials;
    char fixed_ssid[33], fixed_ref[97];
    em_journal *ap_journal;
    /* the fronthaul is served and idle */
    bool (*settled)(void *ctx);
    void *settled_ctx;
    /* spec §8.5: true when the upstream target is a pod whose own upstream chain reaches this one
     * (own: every MAC the pod's own VIFs and radios use); NULL: no other pods known */
    bool (*loops)(void *ctx, const uint8_t target[6], const uint8_t (*own)[6], size_t nown);
    void *loops_ctx;
    em_journal *journal;
    em_engine engine;
    char instance[17];
    bool has_instance;
    em_uplink_state facts;
    bool has_facts;
    em_snapshot last;
    bool has_last;
    char bssid[18], configured[18]; /* the upstream in use, the configured one */
    /* the station the switch puts on that upstream, the configured one (the bootstrap station),
     * and the one the uplink is on at the last read */
    char wanted[33], configured_station[33], active[33];
    unsigned moves;
    bool has_move;
    struct {
        char target[18], previous[18], station[33], previous_station[33], result[96];
        bool done, applied;
    } move;
    char waiting[128];
    bool has_waiting;
    /* after a kept target failed: that switch's intent and start, until the pod is off it */
    cJSON *fallback_after;
    char fallback_instance[17];
} em_uplink_scope;

em_reason em_uplink_open(em_uplink_scope *u, const char *state_dir, const em_journal_schemas *schemas,
                         const char *bssid);
void em_uplink_close(em_uplink_scope *u);
void em_uplink_tick(em_uplink_scope *u);

/* UplinkSwitch.steer: NULL once the move is under way, else why it cannot be made now.
 * band: the target's ("2.4G", "5G", "6G"; NULL: the station in use), channel: its channel (-1:
 * unknown). "no_station_on_band", "channel_not_operable": EasyMesh reason 0x04. */
const char *em_uplink_steer(em_uplink_scope *u, const char *bssid, const char *band, int channel);
/* UplinkSwitch.steering_outcome: 0 under way, 1 applied, -1 failed (*why the reason). */
int em_uplink_steering_outcome(em_uplink_scope *u, const char *bssid, const char **why);

/* The pod's uplink as cm and owm report it (the last read). */
const em_uplink_state *em_uplink_facts(const em_uplink_scope *u);

cJSON *em_uplink_status(em_uplink_scope *u);

/* The scope's backend (the engine's), for vectors and tests. */
em_backend em_uplink_backend(void);

#endif
