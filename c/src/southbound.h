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
    } slots[8];
} em_profile;

em_reason em_profile_load(const char *path, em_profile *out);
void em_profile_free(em_profile *p);

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

/* Plan and submit one M2 intent on the bound fronthaul (and, multi_bss, the slots). */
em_reason em_ap_submit(const em_profile *profile, const char *serial, bool multi_bss,
                       em_ovs_session *session, const em_ap_intent *intent,
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

/* The option 1 uplink switch (spec §8.3): the station joins the EasyMesh backhaul
 * BSS `bssid` with `ssid`. On a refusal, *refusal names why (no transaction sent). */
typedef struct {
    const char *station, *ssid, *secret_ref, *bssid;
} em_uplink_intent;

em_reason em_uplink_submit(const char *serial, em_ovs_session *session, const em_uplink_intent *in,
                           em_secret_resolver resolve, void *resolve_ctx, em_submit_result *out,
                           const char **refusal);

/* check_results: every result an object without "error"; counts where >= 0. */
em_reason em_check_results(const cJSON *results, const int *counts, size_t n);

#endif
