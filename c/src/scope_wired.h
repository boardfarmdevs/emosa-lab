/* SPDX-License-Identifier: Apache-2.0 */
/* The wired uplink scope, as emosa.opensync.wired.WiredBackend and
 * emosa.agent.wired.WiredUplink (spec §8.4): a wired pod's Ethernet uplink port bridged
 * into the fronthaul's bridge (Connection_Manager_Uplink.bridge), written once per
 * OpenSync start, with its own journal. */
#ifndef EMOSA_SCOPE_WIRED_H
#define EMOSA_SCOPE_WIRED_H

#include "engine.h"
#include "ovsdb.h"

typedef struct {
    char pod_id[129], port[16], bridge[16];
} em_wired_intent;

typedef struct {
    em_ovsdb *ovs;
    const char *serial, *run_id;
    em_wired_intent intent;
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
} em_wired_scope;

/* The intent from the agent configuration's uplink (mode ethernet: its port, eth1 when
 * absent) and the profile's fronthaul bridge; false with *why. */
bool em_wired_intent_from(const char *pod_id, const cJSON *uplink, const char *bridge, em_wired_intent *out,
                          em_reason *why);

/* Opens <state_dir>/wired-uplink (the scope's journal) and recovers it. */
em_reason em_wired_open(em_wired_scope *w, const char *state_dir, const em_journal_schemas *schemas,
                        const em_vault *vault, double (*monotonic)(void));
void em_wired_close(em_wired_scope *w);

/* One step on the agent's refresh cadence. */
void em_wired_tick(em_wired_scope *w);

/* WiredUplink.status: {"station", "mode", "port", "bridge", "in_use", "waiting", "operation"}. */
cJSON *em_wired_status(em_wired_scope *w);

/* The scope's backend (the engine's), for vectors and tests. */
em_backend em_wired_backend(void);

#endif
