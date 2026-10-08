/* SPDX-License-Identifier: Apache-2.0 */
/* The client steering scope, as emosa.opensync.steering.SteeringBackend and
 * emosa.agent.steering.ClientSteering: the controller's steering mandates as windows on
 * the pod (owm's band steering), one at a time with a short queue, each an operation
 * in the scope's own journal; windows a previous process left open are closed. */
#ifndef EMOSA_SCOPE_STEERING_H
#define EMOSA_SCOPE_STEERING_H

#include "control.h"
#include "engine.h"
#include "ovsdb.h"
#include "southbound.h"

#define EM_STEERING_QUEUE 8

typedef struct {
    em_steering_request request;
    uint16_t mid;
    double at;
} em_steering_queued;

typedef struct {
    em_ovsdb *ovs;
    const char *serial, *pod_id, *run_id;
    cJSON *(*transact)(void *ctx, const cJSON *operations);
    void *transact_ctx;
    double (*monotonic)(void);
    em_journal *journal;
    em_engine engine;
    char instance[17];
    em_snapshot last;
    bool has_last;
    /* the window under way */
    bool active;
    char op[37], phase[12];
    cJSON *intent; /* its record */
    double kicked;
    size_t nqueue;
    em_steering_queued queue[EM_STEERING_QUEUE];
    cJSON *leftover; /* operations a previous process left open (array) */
    bool swept;           /* the sweep of windows' rows left in the pod done ... */
    int swept_generation; /* ... for this pod source (OVSDB generation) */
    cJSON *history;  /* the latest eight outcomes */
    struct {
        size_t n;
        struct {
            char name[64];
            unsigned count;
        } items[32];
    } counts;
} em_steering_scope;

em_reason em_steering_scope_open(em_steering_scope *s, const char *state_dir, const em_journal_schemas *schemas,
                                 const em_vault *vault);
void em_steering_scope_close(em_steering_scope *s);

/* ClientSteering.start: NULL when started or queued, else why not (e.g. "busy"). */
const char *em_steering_start(em_steering_scope *s, const em_steering_request *r, uint16_t mid);

/* One step on the agent's refresh cadence. */
void em_steering_tick(em_steering_scope *s);

/* ClientSteering.status: {"counts", "active", "queued", "history"}. */
cJSON *em_steering_status(em_steering_scope *s);

/* The scope's backend (the engine's), for vectors and tests. */
em_backend em_steering_backend(void);

#endif
