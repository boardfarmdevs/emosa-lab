/* SPDX-License-Identifier: Apache-2.0 */
/* The AP scope, as emosa.opensync.pod_profile.PodBackend: the pod's bound fronthaul BSS
 * (and, multi-BSS, the managed slot BSSes) as the engine's backend. Its operations come
 * from authenticated M2 sets (the WSC component handoff, agent.c). */
#ifndef EMOSA_SCOPE_AP_H
#define EMOSA_SCOPE_AP_H

#include "engine.h"
#include "ovsdb.h"
#include "southbound.h"

typedef struct {
    em_ovsdb *ovs;
    const em_profile *profile;
    const char *serial, *pod_id;
    bool multi_bss;
    const em_vault *vault;
    cJSON *(*transact)(void *ctx, const cJSON *operations);
    void *transact_ctx;
    /* the last binding (identity: the bound radio's State, with a MAC) */
    bool identity, has_bssid;
    char radio_mac[18], bssid[18], radio_if[33];
    int channel;
    char node_uuid[37], radio_uuid[37];
    em_snapshot last;
    bool has_last;
} em_ap_scope;

em_backend em_ap_backend(void);

/* The scope context (pod_profile.context): the database generation, its schema
 * fingerprint and a binding token over the profile, node, radio and radio MAC. */
bool em_ap_context(em_ap_scope *s, int *generation, char schema_fingerprint[65], char token[65]);

/* Intent.record() of an M2: additional NULL for a single-BSS intent (omitted). */
cJSON *em_ap_intent_record(const char *pod_id, const char *ssid, const char *secret_ref, cJSON *additional);

#endif
