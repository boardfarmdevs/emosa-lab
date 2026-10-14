/* SPDX-License-Identifier: Apache-2.0 */
/* The GTP station scope, as emosa.opensync.gtp_station.GtpStationBackend and
 * emosa.agent.gtp_station.GtpStation (spec 3.10): a legacy pod's station on the site's GTP BSS,
 * written once per OpenSync start while the station has no row, with its own journal. */
#ifndef EMOSA_SCOPE_GTP_H
#define EMOSA_SCOPE_GTP_H

#include "engine.h"
#include "ovsdb.h"
#include "vault.h"

typedef struct {
    char pod_id[129], station[16], ssid[33], secret_ref[97], bssid[18]; /* bssid "" when unpinned */
} em_gtp_intent;

typedef struct {
    em_ovsdb *ovs;
    const char *serial, *run_id;
    em_vault *vault;
    em_gtp_intent intent;
    char band[8]; /* the station's radio's freq_band */
    cJSON *(*transact)(void *ctx, const cJSON *operations);
    void *transact_ctx;
    em_journal *journal;
    em_engine engine;
    char instance[17]; /* which start of the pod's OpenSync */
    bool has_instance;
    em_snapshot last;
    bool has_last;
    char waiting[128]; /* why nothing is written now, "" when nothing waits */
    bool has_waiting;
} em_gtp_scope;

/* The intent from the agent configuration's uplink.bootstrap (ssid, secret_ref, bssid?) and the
 * pod's uplink station; false with *why. */
bool em_gtp_intent_from(const char *pod_id, const char *station, const cJSON *bootstrap, em_gtp_intent *out,
                        em_reason *why);

/* Opens <state_dir>/gtp-station (the scope's journal) and recovers it. */
em_reason em_gtp_open(em_gtp_scope *g, const char *state_dir, const em_journal_schemas *schemas,
                      double (*monotonic)(void));
void em_gtp_close(em_gtp_scope *g);

/* One step on the agent's refresh cadence. */
void em_gtp_tick(em_gtp_scope *g);

/* GtpStation.status: {"station", "ssid", "pinned", "present", "joined", "waiting", "operation"}; never
 * the passphrase. */
cJSON *em_gtp_status(em_gtp_scope *g);

#endif
