/* SPDX-License-Identifier: Apache-2.0 */
/* The radio plan scope, as emosa.opensync.radio_plan.RadioPlanBackend and
 * emosa.agent.radio_plan.RadioPlan (spec 3.10, profile radio_plan emosa): the Inet rows of a legacy
 * pod's APs the profile does not name off (the APs left on, neutral), the operator's cloud cleared
 * (AWLAN_Node's MQTT settings, headers and topics emptied, the Wifi_Stats_Config rows removed), every
 * station not on the GTP's SSID deleted while the pod is wired (with its Inet, credentials and its
 * radio's reference), each idle radio carrying an enabled AP on when it cannot beacon, and
 * its fronthaul radio on the profile's channel while it is wired, written at most once per OpenSync
 * start, with its own journal. */
#ifndef EMOSA_SCOPE_PLAN_H
#define EMOSA_SCOPE_PLAN_H

#include "engine.h"
#include "ovsdb.h"
#include "southbound.h"

#define EM_PLAN_CLAIMED 10

typedef struct {
    char pod_id[129];
    size_t nclaimed;
    char claimed[EM_PLAN_CLAIMED][16]; /* sorted, as the reference's tuple(sorted(...)) */
    char band[8], ht_mode[17];
    int channel;
    char neutral_ref[39]; /* EMOSA's own passphrase for the unclaimed APs: emosa-unclaimed */
    char gtp_ssid[33];    /* the site's GTP SSID: while wired, a station not on it is deleted; "" none */
    /* the radios the profile neither binds nor places: each on at its channel and width when it
     * carries an enabled AP and wm shows it off or with no channel */
    size_t nidle;
    struct {
        char band[8], ht_mode[17];
        int channel;
    } idle[2];
} em_plan_intent;

typedef struct {
    em_ovsdb *ovs;
    const char *serial, *run_id;
    const em_vault *vault; /* EMOSA's own passphrase for the unclaimed APs (em_vault_generated) */
    em_plan_intent intent;
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
} em_plan_scope;

/* The intent from the profile: its fronthaul and slots' VIF names, its radio's band, channel and
 * ht_mode; false with *why. */
bool em_plan_intent_from(const char *pod_id, const em_profile *profile, em_plan_intent *out, em_reason *why);

/* Opens <state_dir>/radio-plan (the scope's journal) and recovers it. */
em_reason em_plan_open(em_plan_scope *s, const char *state_dir, const em_journal_schemas *schemas,
                       const em_vault *vault, double (*monotonic)(void));
void em_plan_close(em_plan_scope *s);

/* One step on the agent's refresh cadence. */
void em_plan_tick(em_plan_scope *s);

/* RadioPlan.status: {"unclaimed", "unclaimed_enabled", "unclaimed_inet_on", "unclaimed_exposed",
 * "operator_cloud", "foreign_stations", "dark_radios", "channel", "wired", "waiting", "operation"}. */
cJSON *em_plan_status(em_plan_scope *s);

#endif
