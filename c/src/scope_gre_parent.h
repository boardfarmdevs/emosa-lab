/* SPDX-License-Identifier: Apache-2.0 */
/* A GRE parent's tunnels to its children, as emosa.opensync.gre_parent.GreParentBackend and
 * emosa.agent.gre_parent.GreTunnels (spec 8.6): per child (a lease in the pod's underlay whose
 * MAC is associated to the parent AP) a gre Wifi_Inet_Config row pgd<b3>_<b4> from the AP's
 * address and a port in the fronthaul's bridge; a tunnel whose child is gone removed with its
 * port. One guarded transaction per change, with its own journal. */
#ifndef EMOSA_SCOPE_GRE_PARENT_H
#define EMOSA_SCOPE_GRE_PARENT_H

#include "engine.h"
#include "ovsdb.h"

#define EM_GRE_CHILDREN 241 /* .10 to .250 of the underlay */

typedef struct {
    char pod_id[129], ap[16], bridge[16];
    size_t nremotes;
    char remotes[EM_GRE_CHILDREN][16]; /* the children's addresses, as the record has them */
} em_gre_intent;

typedef struct {
    em_ovsdb *ovs;
    const char *serial, *run_id;
    char pod_id[129], ap[16], bridge[16], underlay[20];
    uint32_t net, mask; /* the underlay */
    cJSON *(*transact)(void *ctx, const cJSON *operations);
    void *transact_ctx;
    em_journal *journal;
    em_engine engine;
    char instance[17]; /* which start of the pod's OpenSync */
    bool has_instance;
    em_snapshot last;
    bool has_last;
    em_gre_intent children; /* the parent AP's children at the last snapshot, sorted */
    char waiting[128];      /* why nothing is written now, "" when nothing waits */
    bool has_waiting;
} em_gre_scope;

/* Binds the scope to its pod, parent AP, bridge and underlay (a.b.c.d/len); false when one is
 * not usable. */
bool em_gre_bind(em_gre_scope *g, const char *pod_id, const char *ap, const char *bridge, const char *underlay);

/* Opens <state_dir>/gre-parent (the scope's journal) and recovers it. */
em_reason em_gre_open(em_gre_scope *g, const char *state_dir, const em_journal_schemas *schemas,
                      const em_vault *vault, double (*monotonic)(void));
void em_gre_close(em_gre_scope *g);

/* One step on the agent's refresh cadence. */
void em_gre_tick(em_gre_scope *g);

/* GreTunnels.status: {"ap", "underlay", "children", "tunnels", "waiting", "operation"}. */
cJSON *em_gre_status(em_gre_scope *g);

/* The scope's backend (the engine's), for vectors and tests. */
em_backend em_gre_backend(void);

#endif
