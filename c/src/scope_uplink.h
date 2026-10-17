/* SPDX-License-Identifier: Apache-2.0 */
/* The uplink scope, as emosa.opensync.uplink.UplinkBackend and emosa.agent.uplink.
 * UplinkSwitch: the pod's backhaul station joins the EasyMesh backhaul BSS (data plane
 * option 1), pinned to one upstream BSSID, once per OpenSync start, and its other backhaul
 * stations are disabled (a GRE move's once its station joined: make-before-break); a failed
 * switch holds the pod on option 2; the controller's Backhaul Steering moves it (steer,
 * outcome), to another band with that band's station, the target kept in
 * <state_dir>/uplink/target.json; a GRE child's record follows the station cm uses. Its own
 * journal. */
#ifndef EMOSA_SCOPE_UPLINK_H
#define EMOSA_SCOPE_UPLINK_H

#include "engine.h"
#include "ovsdb.h"
#include "view.h"

#define EM_UPLINK_BANDS 3
/* a failed switch's hold: the first wait before the switch is made again, and the longest */
#define EM_UPLINK_HOLD_BACKOFF 600
#define EM_UPLINK_HOLD_BACKOFF_CAP 9600

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
    /* a legacy pod (spec 3.10): its credential has no bssid, priority or enabled; the station's
     * parent is the pin */
    bool legacy;
    em_vault *vault;
    cJSON *(*transact)(void *ctx, const cJSON *operations);
    void *transact_ctx;
    double (*monotonic)(void);
    /* a failed switch's hold (spec 8.3): seconds before the switch is made again, doubled with
     * each held retry up to the cap (0: EM_UPLINK_HOLD_BACKOFF, EM_UPLINK_HOLD_BACKOFF_CAP); the
     * wall clock it is kept in (NULL: time()) */
    double hold_backoff, hold_backoff_cap;
    double (*wall)(void);
    /* credentials: fixed (configuration) or the backhaul BSS of an applied M2 set: the AP scope's
     * first, then each further radio's scope (spec 9: a pod whose fronthaul radio maps no backhaul
     * BSS has it only in its 5 GHz radio's set) */
    bool fixed_credentials;
    char fixed_ssid[33], fixed_ref[97];
    em_journal *ap_journal;
    size_t nradio_journals;
    em_journal *radio_journals[2];
    /* the fronthaul is served and idle */
    bool (*settled)(void *ctx);
    void *settled_ctx;
    /* spec §8.5: true when the upstream target is a pod whose own upstream chain reaches this one
     * (own: every MAC the pod's own VIFs and radios use); NULL: no other pods known */
    bool (*loops)(void *ctx, const uint8_t target[6], const uint8_t (*own)[6], size_t nown);
    void *loops_ctx;
    /* spec 8.6: true when the target BSSID is a GRE parent AP of the fleet (a 3-address GRE move,
     * not Multi-AP); NULL: no parents known, every target is Multi-AP */
    bool (*is_parent)(void *ctx, const uint8_t bssid[6]);
    void *is_parent_ctx;
    em_journal *journal;
    em_engine engine;
    char instance[17];
    bool has_instance;
    em_uplink_state facts;
    bool has_facts;
    em_snapshot last;
    bool has_last;
    char bssid[18], configured[18]; /* the upstream in use, the configured one */
    /* the current uplink's kind: "multi-ap", or "gre" after a move to a GRE parent (spec 8.6) */
    char mode[10];
    /* the station the switch puts on that upstream, the configured one (the bootstrap station),
     * and the one the uplink is on at the last read */
    char wanted[33], configured_station[33], active[33];
    unsigned moves;
    bool has_move;
    struct {
        char target[18], previous[18], station[33], previous_station[33], result[96];
        char mode[10], previous_mode[10]; /* the move's kind and the one it replaced (spec 8.6) */
        bool done, applied;
    } move;
    char waiting[128];
    bool has_waiting;
    /* spec 8.6: the BSS the station was on before any EMOSA move (its bootstrap upstream, cm's own
     * association), recorded once and persisted, so a steer back to it is a revert */
    char bootstrap_bssid[18], bootstrap_ssid[65];
    bool has_bootstrap;
    /* after a kept target failed, or a GRE child's move from its bootstrap: that switch's intent
     * and start, until the pod is off it (a GRE child undoes it itself, spec 8.3 "Moved") */
    cJSON *fallback_after;
    char fallback_instance[17];
    bool undo_logged; /* the undo of that switch announced once */
    /* the pod's start at the last read (a finished move's record is the last start's) */
    char seen_instance[17];
    bool has_seen_instance;
    /* the GRE switch whose other stations were taken off (make-before-break), and the switch this
     * process wrote last: their operation_ids */
    char taken_off[64], written[64];
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
