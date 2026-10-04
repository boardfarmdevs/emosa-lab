/* SPDX-License-Identifier: Apache-2.0 */
/* The telemetry scope, as emosa.opensync.telemetry.TelemetryBackend and
 * emosa.agent.telemetry.TelemetrySetup: the pod publishes its own statistics to the
 * broker (AWLAN_Node.mqtt_settings and a client report, with the survey when asked),
 * written once per OpenSync start, with its own journal. */
#ifndef EMOSA_SCOPE_TELEMETRY_H
#define EMOSA_SCOPE_TELEMETRY_H

#include "engine.h"
#include "ovsdb.h"

typedef struct {
    char pod_id[129], broker[254], topic[129], radio_type[8];
    int port, reporting_interval, sampling_interval, publish_interval; /* publish 0: OpenSync's default */
    bool survey;
} em_telemetry_intent;

typedef struct {
    em_ovsdb *ovs;
    const char *serial, *run_id;
    em_telemetry_intent intent;
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
} em_telemetry_scope;

/* The intent from the agent configuration's telemetry (mode mqtt); false with *why. */
bool em_telemetry_intent_from(const char *pod_id, const char *serial, const cJSON *config,
                              em_telemetry_intent *out, em_reason *why);

/* Opens <state_dir>/telemetry (the scope's journal) and recovers it. */
em_reason em_telemetry_open(em_telemetry_scope *t, const char *state_dir, const em_journal_schemas *schemas,
                            const em_vault *vault, double (*monotonic)(void));
void em_telemetry_close(em_telemetry_scope *t);

/* One step on the agent's refresh cadence. */
void em_telemetry_tick(em_telemetry_scope *t);

/* TelemetrySetup.status: {"waiting", "operation"}. */
cJSON *em_telemetry_status(em_telemetry_scope *t);

/* The scope's backend (the engine's), for vectors and tests. */
em_backend em_telemetry_backend(void);

#endif
