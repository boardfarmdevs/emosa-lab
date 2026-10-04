/* SPDX-License-Identifier: Apache-2.0 */
/* The operation lifecycle, as emosa.reconcile.Engine and emosa.operations.transition:
 * one engine and journal per scope (the AP BSS, the uplink, telemetry, client steering,
 * the probe watch). A scope supplies its backend: the intent's target, a snapshot of
 * what the pod has configured and shows, a plan, and one guarded submit. */
#ifndef EMOSA_ENGINE_H
#define EMOSA_ENGINE_H

#include <cjson/cJSON.h>

#include "common.h"
#include "journal.h"
#include "vault.h"

/* A scope's snapshot (emosa.backends.base.Snapshot): owned cJSON members. */
typedef struct {
    cJSON *config;   /* the configured values */
    cJSON *observed; /* the Observation: pod_id, resource_id, values, source, backend_mode,
                        session_generation, received_at, fresh, provenance, source_time,
                        revision */
    bool ready;
    int generation;
    char schema_fingerprint[65];
    /* which start of the pod's OpenSync (em_start_instance), when the scope knows it */
    char instance[17];
    bool has_instance;
} em_snapshot;

void em_snapshot_clear(em_snapshot *s);
bool em_snapshot_fresh(const em_snapshot *s);
/* Observation.satisfies: fresh, and every target value equal to the observed one. */
bool em_snapshot_satisfies(const em_snapshot *s, const cJSON *target);
/* Every target value equal to values' (Engine._matches). */
bool em_matches(const cJSON *values, const cJSON *target);
/* The Observation object for a scope (values owned by the result). */
cJSON *em_observation(const char *pod_id, const char *resource, cJSON *values, const char *mode,
                      int generation, bool fresh, const char *provenance, unsigned long revision);

typedef struct {
    char status[12]; /* committed, conflict, rejected, unknown */
    cJSON *evidence; /* owned */
    em_reason reason;
} em_submit_out;

typedef struct {
    const char *mode;
    /* The intent's target values; NULL (with *why) when the intent is invalid. */
    cJSON *(*target)(void *ctx, const cJSON *intent, em_reason *why);
    /* A fresh snapshot; false when the pod cannot be read and there is no earlier one. */
    bool (*snapshot)(void *ctx, em_snapshot *out);
    /* The plan (owned), or NULL with *why. */
    cJSON *(*plan)(void *ctx, const cJSON *intent, em_reason *why);
    void (*submit)(void *ctx, const cJSON *intent, const cJSON *attempt, em_submit_out *out);
} em_backend;

#define EM_ENGINE_TIMERS 64

typedef struct {
    em_journal *journal;
    const em_vault *vault;
    char pod_id[129];
    em_backend backend;
    void *ctx;
    double (*monotonic)(void);
    bool busy;
    struct {
        char id[37];
        double deadline, start;
        bool has_start;
    } timers[EM_ENGINE_TIMERS];
    size_t ntimers;
    /* the live WSC exchange's guard (process-local authority, never recovered) */
    char wsc_op[37];
    bool (*wsc_guard)(void *ctx);
    void *wsc_ctx;
    struct {
        char id[37];
        char *signature;
    } observed[8];
    /* the start of the pod's OpenSync in the last snapshot taken, "" while unknown */
    char last_instance[17];
} em_engine;

void em_engine_init(em_engine *e, em_journal *journal, const em_vault *vault, const char *pod_id,
                    em_backend backend, void *ctx, double (*monotonic)(void));
void em_engine_free(em_engine *e);

/* Engine.request/_request: an operation for the intent (its record), or the one the
 * key already has; NULL with *why. wsc_receipt only for initiating "wsc-component". */
cJSON *em_engine_request(em_engine *e, const cJSON *intent, const char *source, const char *key,
                         const char *run_id, double deadline, const char *interface,
                         const cJSON *wsc_receipt, em_reason *why);
/* Engine.execute: validate, plan, submit (a REQUESTED operation); the result's record. */
cJSON *em_engine_execute(em_engine *e, const char *operation_id);
/* Engine.reconcile: confirm or time out the pod's latest legitimate operation. */
void em_engine_reconcile(em_engine *e);
/* Engine.recover: at start, unsent operations cancelled, sent ones INDETERMINATE. */
void em_engine_recover(em_engine *e);
bool em_engine_expired(em_engine *e, cJSON *op);
cJSON *em_engine_cancel(em_engine *e, const char *operation_id);
/* Engine._save: updated_at, the state's timing, the journal. */
void em_engine_save(em_engine *e, cJSON *op, const cJSON *payload);
/* Engine._evidence (+ observed_noop when true, + instance when not NULL). */
cJSON *em_engine_evidence(const em_snapshot *s, bool observed_noop, const char *instance);
/* The intent's fingerprint: {**intent, "target": target}. */
bool em_engine_fingerprint(em_engine *e, const cJSON *intent, const cJSON *target, char out[65]);

/* emosa.operations.transition: false (the record unchanged) when not permitted. */
bool em_transition(cJSON *op, const char *target, const cJSON *evidence);
bool em_state_active(const char *state);
const char *em_state_of(const cJSON *op);
void em_set_reason(cJSON *op, const char *reason); /* NULL: null */

#endif
