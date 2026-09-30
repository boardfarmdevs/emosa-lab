/* The operation lifecycle (emosa.reconcile.Engine, emosa.operations). */
#include "engine.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "canon.h"

/* -- records ------------------------------------------------------------------------- */

static const char *str(const cJSON *o, const char *k)
{
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, k));
}

static void set(cJSON *o, const char *k, cJSON *v)
{
    if (cJSON_GetObjectItemCaseSensitive(o, k))
        cJSON_ReplaceItemInObjectCaseSensitive(o, k, v);
    else
        cJSON_AddItemToObject(o, k, v);
}

static void set_str(cJSON *o, const char *k, const char *v) { set(o, k, v ? cJSON_CreateString(v) : cJSON_CreateNull()); }

const char *em_state_of(const cJSON *op) { return str(op, "state"); }

void em_set_reason(cJSON *op, const char *reason) { set_str(op, "reason", reason); }

bool em_state_active(const char *s)
{
    return s && (!strcmp(s, "REQUESTED") || !strcmp(s, "VALIDATED") || !strcmp(s, "SUBMITTED") ||
                 !strcmp(s, "CONFIG_COMMITTED") || !strcmp(s, "INDETERMINATE"));
}

static bool truthy(const cJSON *v)
{
    return v && !cJSON_IsNull(v) && !cJSON_IsFalse(v) && !(cJSON_IsNumber(v) && v->valuedouble == 0) &&
           !(cJSON_IsString(v) && !*v->valuestring);
}

bool em_transition(cJSON *op, const char *target, const cJSON *evidence)
{
    static const struct {
        const char *from, *to[5];
    } allowed[] = {
        {"REQUESTED", {"VALIDATED", "REJECTED", "CANCELLED"}},
        {"VALIDATED", {"SUBMITTED", "REJECTED", "OWNERSHIP_CONFLICT", "CANCELLED", "OBSERVED_APPLIED"}},
        {"SUBMITTED", {"CONFIG_COMMITTED", "INDETERMINATE", "FAILED", "OWNERSHIP_CONFLICT"}},
        {"CONFIG_COMMITTED", {"OBSERVED_APPLIED", "FAILED", "TIMED_OUT", "OWNERSHIP_CONFLICT"}},
        {"INDETERMINATE", {"CONFIG_COMMITTED", "OBSERVED_APPLIED", "OWNERSHIP_CONFLICT", "FAILED", "TIMED_OUT"}},
    };
    const char *state = em_state_of(op);
    bool ok = false;
    for (size_t i = 0; i < sizeof(allowed) / sizeof(*allowed); i++)
        if (!strcmp(allowed[i].from, state))
            for (size_t k = 0; k < 5 && allowed[i].to[k]; k++)
                ok = ok || !strcmp(allowed[i].to[k], target);
    if (!ok)
        return false;
    if (!strcmp(target, "SUBMITTED") && !cJSON_GetArraySize(cJSON_GetObjectItemCaseSensitive(op, "attempts")))
        return false;
    if (!strcmp(target, "CONFIG_COMMITTED") &&
        !truthy(cJSON_GetObjectItemCaseSensitive(evidence, "transaction_validated")))
        return false;
    if (!strcmp(target, "OBSERVED_APPLIED")) {
        if (!truthy(cJSON_GetObjectItemCaseSensitive(evidence, "fresh")) ||
            !truthy(cJSON_GetObjectItemCaseSensitive(evidence, "predicate_satisfied")))
            return false;
        if (!strcmp(state, "VALIDATED") && !truthy(cJSON_GetObjectItemCaseSensitive(evidence, "observed_noop")))
            return false;
    }
    if (!strcmp(target, "TIMED_OUT") && !cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(op, "deadline_elapsed")))
        return false;
    set_str(op, "state", target);
    return true;
}

/* -- snapshots --------------------------------------------------------------------------- */

void em_snapshot_clear(em_snapshot *s)
{
    cJSON_Delete(s->config);
    cJSON_Delete(s->observed);
    memset(s, 0, sizeof(*s));
}

bool em_snapshot_fresh(const em_snapshot *s)
{
    return s->observed && cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(s->observed, "fresh"));
}

bool em_matches(const cJSON *values, const cJSON *target)
{
    const cJSON *t;
    cJSON_ArrayForEach(t, target)
    {
        const cJSON *v = cJSON_GetObjectItemCaseSensitive(values, t->string);
        if (!v || !cJSON_Compare(v, t, true))
            return false;
    }
    return true;
}

bool em_snapshot_satisfies(const em_snapshot *s, const cJSON *target)
{
    return em_snapshot_fresh(s) && em_matches(cJSON_GetObjectItemCaseSensitive(s->observed, "values"), target);
}

cJSON *em_observation(const char *pod_id, const char *resource, cJSON *values, const char *mode,
                      int generation, bool fresh, const char *provenance, unsigned long revision)
{
    char now[32];
    em_utc_now(now);
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "pod_id", pod_id);
    cJSON_AddStringToObject(o, "resource_id", resource);
    cJSON_AddItemToObject(o, "values", values);
    cJSON_AddStringToObject(o, "source", "ovsdb");
    cJSON_AddStringToObject(o, "backend_mode", mode);
    cJSON_AddNumberToObject(o, "session_generation", generation);
    cJSON_AddStringToObject(o, "received_at", now);
    cJSON_AddBoolToObject(o, "fresh", fresh);
    cJSON_AddStringToObject(o, "provenance", provenance);
    cJSON_AddNullToObject(o, "source_time");
    cJSON_AddNumberToObject(o, "revision", (double)revision);
    return o;
}

/* -- the engine ---------------------------------------------------------------------------- */

void em_engine_init(em_engine *e, em_journal *journal, const em_vault *vault, const char *pod_id,
                    em_backend backend, void *ctx, double (*monotonic)(void))
{
    memset(e, 0, sizeof(*e));
    e->journal = journal;
    e->vault = vault;
    EM_FORMAT_FIXED(e->pod_id, sizeof(e->pod_id), "%s", pod_id); /* a pod name: 64 at most */
    e->backend = backend;
    e->ctx = ctx;
    e->monotonic = monotonic;
}

void em_engine_free(em_engine *e)
{
    for (size_t i = 0; i < sizeof(e->observed) / sizeof(*e->observed); i++)
        free(e->observed[i].signature);
}

static int timer(em_engine *e, const char *id, bool create)
{
    for (size_t i = 0; i < e->ntimers; i++)
        if (!strcmp(e->timers[i].id, id))
            return (int)i;
    if (!create)
        return -1;
    if (e->ntimers == EM_ENGINE_TIMERS) { /* the oldest goes: its deadline is persisted */
        memmove(&e->timers[0], &e->timers[1], (EM_ENGINE_TIMERS - 1) * sizeof(e->timers[0]));
        e->ntimers--;
    }
    size_t i = e->ntimers++;
    memset(&e->timers[i], 0, sizeof(e->timers[i]));
    /* an operation ID from a record: one that is not a UUID is stored as none, matching nothing */
    if (!em_copy(e->timers[i].id, sizeof(e->timers[i].id), id))
        e->timers[i].id[0] = 0;
    return (int)i;
}

void em_engine_save(em_engine *e, cJSON *op, const cJSON *payload)
{
    char now[32];
    em_utc_now(now);
    set_str(op, "updated_at", now);
    int t = timer(e, str(op, "operation_id"), false);
    cJSON *timings = cJSON_GetObjectItemCaseSensitive(op, "timings");
    if (t >= 0 && e->timers[t].has_start && !cJSON_GetObjectItemCaseSensitive(timings, em_state_of(op))) {
        cJSON *x = cJSON_CreateObject();
        double spent = e->monotonic() - e->timers[t].start;
        cJSON_AddNumberToObject(x, "seconds_since_request", spent < 0 ? 0 : spent);
        cJSON_AddStringToObject(x, "process_clock_id", em_journal_process_id(e->journal));
        cJSON_AddItemToObject(timings, em_state_of(op), x);
    }
    cJSON *empty = payload ? NULL : cJSON_CreateObject();
    em_journal_save(e->journal, op, payload ? payload : empty);
    cJSON_Delete(empty);
}

bool em_engine_fingerprint(em_engine *e, const cJSON *intent, const cJSON *target, char out[65])
{
    cJSON *v = cJSON_Duplicate(intent, true);
    cJSON_AddItemToObject(v, "target", cJSON_Duplicate(target, true));
    bool ok = em_vault_fingerprint(e->vault, v, out);
    cJSON_Delete(v);
    return ok;
}

static cJSON *new_operation(const char *run_id, const char *source, const char *interface, const cJSON *intent,
                            const char *fingerprint, const char *key, const char *now, const char *deadline_at)
{
    char id[37];
    em_uuid4(id);
    cJSON *op = cJSON_CreateObject();
    cJSON_AddStringToObject(op, "operation_id", id);
    cJSON_AddStringToObject(op, "run_id", run_id);
    cJSON_AddStringToObject(op, "request_source", source);
    cJSON_AddStringToObject(op, "initiating_interface", interface);
    cJSON_AddItemToObject(op, "intent", cJSON_Duplicate(intent, true));
    cJSON_AddStringToObject(op, "intent_fingerprint", fingerprint);
    cJSON_AddStringToObject(op, "idempotency_key", key);
    cJSON_AddStringToObject(op, "created_at", now);
    cJSON_AddStringToObject(op, "updated_at", now);
    cJSON_AddStringToObject(op, "deadline_at", deadline_at);
    cJSON_AddNumberToObject(op, "schema_version", 1);
    cJSON_AddStringToObject(op, "state", "REQUESTED");
    cJSON_AddNullToObject(op, "reason");
    cJSON_AddStringToObject(op, "mapping_version", "synthetic-existing-bss-v1");
    cJSON_AddNullToObject(op, "schema_fingerprint");
    cJSON_AddItemToObject(op, "attempts", cJSON_CreateArray());
    cJSON *commit = cJSON_AddObjectToObject(op, "commit_evidence");
    cJSON_AddStringToObject(commit, "attribution", "not_submitted");
    cJSON_AddStringToObject(op, "application_predicate", "fresh-qualified-existing-ap-ssid-security-v1");
    cJSON_AddNullToObject(op, "application_evidence");
    cJSON_AddNullToObject(op, "changed");
    cJSON_AddFalseToObject(op, "deadline_elapsed");
    cJSON_AddNullToObject(op, "original_outcome");
    cJSON_AddNullToObject(op, "late_resolution");
    cJSON *verification = cJSON_AddObjectToObject(op, "client_verification");
    cJSON_AddStringToObject(verification, "verdict", "unknown");
    cJSON_AddFalseToObject(op, "blocked_for_resubmission");
    cJSON_AddNullToObject(op, "plan");
    cJSON_AddItemToObject(op, "timings", cJSON_CreateObject());
    return op;
}

cJSON *em_engine_request(em_engine *e, const cJSON *intent, const char *source, const char *key,
                         const char *run_id, double deadline, const char *interface,
                         const cJSON *wsc_receipt, em_reason *why)
{
    *why = EM_OK;
    cJSON *target = e->backend.target(e->ctx, intent, why);
    if (!target)
        return NULL;
    if (strcmp(interface, "semantic") && (strcmp(interface, "wsc-component") || !wsc_receipt)) {
        cJSON_Delete(target);
        *why = EM_MISSING_PREREQUISITE;
        return NULL;
    }
    if (!source || !*source || !key || !*key || strlen(key) > 128 || !(deadline > 0 && deadline <= 3600)) {
        cJSON_Delete(target);
        *why = EM_INVALID_INPUT;
        return NULL;
    }
    if (strcmp(str(intent, "pod_id") ? str(intent, "pod_id") : "", e->pod_id)) {
        cJSON_Delete(target);
        *why = EM_UNSUPPORTED_OPERATION; /* "pod outside configured allowlist" */
        return NULL;
    }
    char fingerprint[65];
    bool fp = em_engine_fingerprint(e, intent, target, fingerprint);
    cJSON_Delete(target);
    if (!fp) {
        *why = EM_INVALID_INPUT;
        return NULL;
    }
    cJSON *old = em_journal_lookup(e->journal, source, e->pod_id, key);
    if (old) {
        if (strcmp(str(old, "intent_fingerprint"), fingerprint)) {
            cJSON_Delete(old);
            *why = EM_INVALID_INPUT; /* "idempotency key reused for different intent" */
            return NULL;
        }
        return old;
    }
    char now[32], deadline_at[40];
    em_utc_now(now);
    em_utc_add(now, deadline, deadline_at);
    cJSON *op = new_operation(run_id, source, interface, intent, fingerprint, key, now, deadline_at);
    em_reason r = em_journal_add(e->journal, op, wsc_receipt);
    if (r != EM_OK) {
        cJSON_Delete(op);
        *why = r;
        return NULL;
    }
    const char *id = str(op, "operation_id");
    int t = timer(e, id, true);
    e->timers[t].start = e->monotonic();
    e->timers[t].has_start = true;
    e->timers[t].deadline = e->timers[t].start + deadline;
    bool other_active = e->busy;
    cJSON *ops = em_journal_operations(e->journal, NULL), *p;
    cJSON_ArrayForEach(p, ops)
    {
        const char *pod = str(cJSON_GetObjectItemCaseSensitive(p, "intent"), "pod_id");
        other_active = other_active || (pod && !strcmp(pod, e->pod_id) && em_state_active(em_state_of(p)) &&
                                        strcmp(str(p, "operation_id"), id));
    }
    cJSON_Delete(ops);
    cJSON *owned = other_active ? NULL : em_journal_ownership(e->journal, e->pod_id);
    if (other_active || owned) {
        em_transition(op, "REJECTED", NULL);
        em_set_reason(op, other_active ? "BUSY" : "OWNERSHIP_CONFLICT");
        em_engine_save(e, op, NULL);
    }
    cJSON_Delete(owned);
    return op;
}

static bool check_wsc(em_engine *e, const cJSON *op)
{
    if (strcmp(str(op, "initiating_interface"), "wsc-component"))
        return true;
    cJSON *receipt = em_journal_wsc_receipt(e->journal, str(op, "operation_id"));
    bool ok = receipt && !strcmp(str(receipt, "process_id"), em_journal_process_id(e->journal)) && e->wsc_guard &&
              !strcmp(e->wsc_op, str(op, "operation_id")) && e->wsc_guard(e->wsc_ctx);
    cJSON_Delete(receipt);
    return ok;
}

cJSON *em_engine_evidence(const em_snapshot *s, bool observed_noop, const char *instance)
{
    const cJSON *o = s->observed;
    cJSON *x = cJSON_CreateObject();
    cJSON_AddBoolToObject(x, "fresh", em_snapshot_fresh(s));
    cJSON_AddTrueToObject(x, "predicate_satisfied");
    cJSON_AddNumberToObject(x, "generation", s->generation);
    cJSON_AddItemToObject(x, "revision", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(o, "revision"), true));
    cJSON_AddItemToObject(x, "received_at", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(o, "received_at"), true));
    cJSON_AddItemToObject(x, "source", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(o, "source"), true));
    cJSON_AddItemToObject(x, "provenance", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(o, "provenance"), true));
    if (observed_noop)
        cJSON_AddTrueToObject(x, "observed_noop");
    if (instance)
        cJSON_AddStringToObject(x, "instance", instance);
    return x;
}

static void reject(em_engine *e, cJSON *op, em_reason r)
{
    em_transition(op, "REJECTED", NULL);
    em_set_reason(op, em_reason_name(r));
    em_engine_save(e, op, NULL);
}

cJSON *em_engine_execute(em_engine *e, const char *operation_id)
{
    cJSON *op = em_journal_get(e->journal, operation_id);
    if (!op || strcmp(em_state_of(op), "REQUESTED"))
        return op;
    if (e->busy)
        return op; /* "pod already executing": the caller retries later */
    e->busy = true;
    const cJSON *intent = cJSON_GetObjectItemCaseSensitive(op, "intent");
    em_snapshot snap = {0};
    em_reason why = EM_OK;
    cJSON *plan = NULL;
    if (!check_wsc(e, op)) {
        why = EM_NOT_READY;
    } else if (!(plan = e->backend.plan(e->ctx, intent, &why))) {
        /* why set */
    } else if (!e->backend.snapshot(e->ctx, &snap) || !snap.ready) {
        why = EM_NOT_READY; /* "fresh complete snapshot required" */
    } else if (!check_wsc(e, op)) {
        why = EM_NOT_READY;
    }
    if (why != EM_OK) {
        cJSON_Delete(plan);
        em_snapshot_clear(&snap);
        reject(e, op, why);
        e->busy = false;
        return op;
    }
    set(op, "plan", plan);
    set_str(op, "schema_fingerprint", snap.schema_fingerprint[0] ? snap.schema_fingerprint : NULL);
    em_transition(op, "VALIDATED", NULL);
    cJSON *payload = cJSON_CreateObject();
    cJSON_AddItemToObject(payload, "plan", cJSON_Duplicate(plan, true));
    em_engine_save(e, op, payload);
    cJSON_Delete(payload);
    cJSON *target = e->backend.target(e->ctx, intent, &why);
    if (!target) {
        reject(e, op, why);
        em_snapshot_clear(&snap);
        e->busy = false;
        return op;
    }
    if (em_snapshot_satisfies(&snap, target) && em_matches(snap.config, target)) {
        cJSON *evidence = em_engine_evidence(&snap, true, NULL);
        em_transition(op, "OBSERVED_APPLIED", evidence);
        set(op, "changed", cJSON_CreateFalse());
        set(op, "application_evidence", evidence);
        em_engine_save(e, op, NULL);
        cJSON_Delete(target);
        em_snapshot_clear(&snap);
        e->busy = false;
        return op;
    }
    cJSON_Delete(target);
    char attempt_id[37], transaction_id[37], now[32];
    em_uuid4(attempt_id);
    em_uuid4(transaction_id);
    em_utc_now(now);
    cJSON *attempt = cJSON_CreateObject();
    cJSON_AddStringToObject(attempt, "attempt_id", attempt_id);
    cJSON_AddStringToObject(attempt, "transaction_id", transaction_id);
    cJSON_AddNumberToObject(attempt, "session_generation", snap.generation);
    cJSON_AddStringToObject(attempt, "prepared_at", now);
    cJSON_AddItemToArray(cJSON_GetObjectItemCaseSensitive(op, "attempts"), cJSON_Duplicate(attempt, true));
    em_transition(op, "SUBMITTED", NULL);
    cJSON *unknown = cJSON_CreateObject();
    cJSON_AddStringToObject(unknown, "attribution", "unknown");
    set(op, "commit_evidence", unknown);
    payload = cJSON_CreateObject();
    cJSON_AddItemToObject(payload, "attempt", cJSON_Duplicate(attempt, true));
    em_engine_save(e, op, payload); /* the FULL SQLite commit before the send */
    cJSON_Delete(payload);
    em_submit_out out = {0};
    e->backend.submit(e->ctx, intent, attempt, &out);
    cJSON_Delete(attempt);
    if (!strcmp(out.status, "committed")) {
        if (em_transition(op, "CONFIG_COMMITTED", out.evidence)) {
            set(op, "commit_evidence", out.evidence);
            out.evidence = NULL;
            set(op, "changed", cJSON_CreateTrue());
        }
    } else if (!strcmp(out.status, "conflict")) {
        em_transition(op, "OWNERSHIP_CONFLICT", NULL);
        em_set_reason(op, "OWNERSHIP_CONFLICT");
        cJSON *evidence = cJSON_CreateObject();
        cJSON_AddStringToObject(evidence, "operation_id", str(op, "operation_id"));
        cJSON_AddStringToObject(evidence, "reason", em_reason_name(out.reason));
        em_journal_conflict(e->journal, e->pod_id, evidence);
        cJSON_Delete(evidence);
    } else if (!strcmp(out.status, "rejected")) {
        em_transition(op, "FAILED", NULL);
        em_set_reason(op, em_reason_name(out.reason));
    } else {
        em_transition(op, "INDETERMINATE", NULL);
        em_set_reason(op, "OUTCOME_UNKNOWN");
    }
    cJSON_Delete(out.evidence);
    em_engine_save(e, op, NULL);
    em_snapshot_clear(&snap);
    e->busy = false;
    return op;
}

bool em_engine_expired(em_engine *e, cJSON *op)
{
    int t = timer(e, str(op, "operation_id"), false);
    if (t < 0) { /* a new process: a new monotonic budget from the persisted UTC */
        char now[32];
        double remaining = 0;
        em_utc_now(now);
        if (!em_utc_diff(now, str(op, "deadline_at"), &remaining) || remaining < 0)
            remaining = 0;
        t = timer(e, str(op, "operation_id"), true);
        e->timers[t].deadline = e->monotonic() + remaining;
    }
    return e->monotonic() >= e->timers[t].deadline;
}

cJSON *em_engine_cancel(em_engine *e, const char *operation_id)
{
    cJSON *op = em_journal_get(e->journal, operation_id);
    if (op && em_transition(op, "CANCELLED", NULL))
        em_engine_save(e, op, NULL);
    return op;
}

void em_engine_recover(em_engine *e)
{
    cJSON *ops = em_journal_operations(e->journal, NULL), *op;
    cJSON_ArrayForEach(op, ops)
    {
        const char *state = em_state_of(op);
        cJSON *payload = cJSON_CreateObject();
        if (!strcmp(str(op, "initiating_interface"), "wsc-component") && !strcmp(state, "REQUESTED")) {
            em_transition(op, "CANCELLED", NULL);
            cJSON_AddStringToObject(payload, "recovery", "unsent WSC component request; new exchange required");
            em_engine_save(e, op, payload);
        } else if (!strcmp(state, "SUBMITTED")) {
            em_transition(op, "INDETERMINATE", NULL);
            cJSON *unknown = cJSON_CreateObject();
            cJSON_AddStringToObject(unknown, "attribution", "unknown");
            set(op, "commit_evidence", unknown);
            em_set_reason(op, "OUTCOME_UNKNOWN");
            cJSON_AddStringToObject(payload, "recovery", "possibly sent; observation required before action");
            em_engine_save(e, op, payload);
        } else if (!strcmp(state, "VALIDATED")) {
            em_transition(op, "CANCELLED", NULL);
            cJSON_AddStringToObject(payload, "recovery", "unsent intent; explicit new request required");
            em_engine_save(e, op, payload);
        }
        cJSON_Delete(payload);
    }
    cJSON_Delete(ops);
}

static bool in(const char *s, const char *const *set, size_t n)
{
    for (size_t i = 0; s && i < n; i++)
        if (!strcmp(s, set[i]))
            return true;
    return false;
}

/* the OBSERVATION event, once per change of what is seen */
static void observation_event(em_engine *e, cJSON *op, const em_snapshot *snap, const cJSON *target)
{
    cJSON *sig = cJSON_CreateObject();
    cJSON_AddItemToObject(sig, "values", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(snap->observed, "values"), true));
    cJSON_AddItemToObject(sig, "config", cJSON_Duplicate(snap->config, true));
    cJSON_AddNumberToObject(sig, "generation", snap->generation);
    cJSON_AddBoolToObject(sig, "fresh", em_snapshot_fresh(snap));
    char *text = em_json_dumps(sig, EM_JSON_DEFAULT, true);
    cJSON_Delete(sig);
    const char *id = str(op, "operation_id");
    size_t slot = 0, n = sizeof(e->observed) / sizeof(*e->observed);
    for (size_t i = 0; i < n; i++)
        if (!strcmp(e->observed[i].id, id))
            slot = i + 1;
    if (slot && e->observed[slot - 1].signature && text && !strcmp(e->observed[slot - 1].signature, text)) {
        free(text);
        return;
    }
    if (!slot) { /* the oldest slot */
        free(e->observed[n - 1].signature);
        memmove(&e->observed[1], &e->observed[0], (n - 1) * sizeof(e->observed[0]));
        memset(&e->observed[0], 0, sizeof(e->observed[0]));
        if (!em_copy(e->observed[0].id, sizeof(e->observed[0].id), id)) /* as for the timers */
            e->observed[0].id[0] = 0;
        slot = 1;
    } else {
        free(e->observed[slot - 1].signature);
    }
    e->observed[slot - 1].signature = text;
    const cJSON *values = cJSON_GetObjectItemCaseSensitive(snap->observed, "values");
    cJSON *payload = cJSON_CreateObject(), *diff = cJSON_CreateObject();
    cJSON_AddStringToObject(payload, "operation_id", id);
    cJSON_AddItemToObject(payload, "observation", cJSON_Duplicate(snap->observed, true));
    cJSON_AddItemToObject(payload, "config", cJSON_Duplicate(snap->config, true));
    const cJSON *t;
    cJSON_ArrayForEach(t, target)
    {
        const cJSON *seen = cJSON_GetObjectItemCaseSensitive(values, t->string);
        if (!seen || !cJSON_Compare(seen, t, true)) {
            cJSON *d = cJSON_AddObjectToObject(diff, t->string);
            cJSON_AddItemToObject(d, "desired", cJSON_Duplicate(t, true));
            cJSON_AddItemToObject(d, "observed", seen ? cJSON_Duplicate(seen, true) : cJSON_CreateNull());
        }
    }
    cJSON_AddItemToObject(payload, "desired_observed_diff", diff);
    em_journal_event(e->journal, str(op, "run_id"), "OBSERVATION", payload, e->pod_id);
    cJSON_Delete(payload);
}

void em_engine_reconcile(em_engine *e)
{
    em_snapshot snap = {0};
    if (!e->backend.snapshot(e->ctx, &snap))
        return;
    static const char *const eligible[] = {"CONFIG_COMMITTED", "INDETERMINATE", "TIMED_OUT", "OBSERVED_APPLIED"};
    static const char *const newer_states[] = {"SUBMITTED", "CONFIG_COMMITTED", "OBSERVED_APPLIED",
                                               "INDETERMINATE", "TIMED_OUT", "OWNERSHIP_CONFLICT"};
    cJSON *ops = em_journal_operations(e->journal, NULL);
    int count = cJSON_GetArraySize(ops);
    for (int index = 0; index < count; index++) {
        cJSON *op = cJSON_GetArrayItem(ops, index);
        const char *pod = str(cJSON_GetObjectItemCaseSensitive(op, "intent"), "pod_id");
        if (!pod || strcmp(pod, e->pod_id) || !in(em_state_of(op), eligible, 4))
            continue;
        bool newer = false;
        for (int k = index + 1; k < count && !newer; k++) {
            cJSON *p = cJSON_GetArrayItem(ops, k);
            const char *pp = str(cJSON_GetObjectItemCaseSensitive(p, "intent"), "pod_id");
            newer = pp && !strcmp(pp, e->pod_id) && in(em_state_of(p), newer_states, 6);
        }
        if (newer)
            continue;
        bool expired = em_engine_expired(e, op), changed = false;
        const char *state = em_state_of(op);
        if (expired && (!strcmp(state, "CONFIG_COMMITTED") || !strcmp(state, "INDETERMINATE"))) {
            if (!cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(op, "deadline_elapsed"))) {
                set_str(op, "original_outcome", state);
                set(op, "deadline_elapsed", cJSON_CreateTrue());
                changed = true;
            }
            if (!strcmp(state, "CONFIG_COMMITTED")) {
                em_transition(op, "TIMED_OUT", NULL);
                em_set_reason(op, "APPLY_TIMEOUT");
            }
        }
        em_reason why;
        cJSON *target = e->backend.target(e->ctx, cJSON_GetObjectItemCaseSensitive(op, "intent"), &why);
        if (!target) {
            if (!cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(op, "blocked_for_resubmission"))) {
                set(op, "blocked_for_resubmission", cJSON_CreateTrue());
                changed = true;
            }
        } else {
            observation_event(e, op, &snap, target);
            bool elapsed = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(op, "deadline_elapsed"));
            if (snap.ready && !strcmp(em_state_of(op), "INDETERMINATE") && elapsed && !em_matches(snap.config, target)) {
                em_transition(op, "TIMED_OUT", NULL);
                em_set_reason(op, "APPLY_TIMEOUT");
                changed = true;
            }
            if (snap.ready && em_snapshot_fresh(&snap)) {
                bool config_matches = em_matches(snap.config, target);
                bool applied = em_snapshot_satisfies(&snap, target);
                const char *now_state = em_state_of(op);
                bool evidence_none = cJSON_IsNull(cJSON_GetObjectItemCaseSensitive(op, "application_evidence"));
                if (!config_matches && (!strcmp(now_state, "CONFIG_COMMITTED") || !strcmp(now_state, "OBSERVED_APPLIED") ||
                                        !strcmp(now_state, "TIMED_OUT"))) {
                    cJSON *evidence = cJSON_CreateObject();
                    cJSON_AddStringToObject(evidence, "operation_id", str(op, "operation_id"));
                    cJSON_AddStringToObject(evidence, "reason", "owned configuration changed");
                    em_journal_conflict(e->journal, e->pod_id, evidence);
                    cJSON_Delete(evidence);
                    if (!strcmp(now_state, "CONFIG_COMMITTED"))
                        em_transition(op, "OWNERSHIP_CONFLICT", NULL);
                    const char *reason = str(op, "reason");
                    if (!reason || strcmp(reason, "OWNERSHIP_CONFLICT")) {
                        em_set_reason(op, "OWNERSHIP_CONFLICT");
                        changed = true;
                    }
                } else if (config_matches && applied && evidence_none) {
                    cJSON *evidence = em_engine_evidence(&snap, false, NULL);
                    if (elapsed)
                        set_str(op, "late_resolution", "applied_after_deadline");
                    if (strcmp(now_state, "TIMED_OUT"))
                        em_transition(op, "OBSERVED_APPLIED", evidence);
                    const char *attribution = str(cJSON_GetObjectItemCaseSensitive(op, "commit_evidence"), "attribution");
                    if (attribution && !strcmp(attribution, "unknown"))
                        cJSON_AddStringToObject(evidence, "attribution", "current_condition_only");
                    else
                        em_set_reason(op, elapsed ? "APPLY_TIMEOUT" : NULL);
                    set(op, "application_evidence", evidence);
                    changed = true;
                }
            }
            cJSON_Delete(target);
        }
        if (changed) {
            cJSON *payload = cJSON_CreateObject();
            cJSON_AddItemToObject(payload, "observation", cJSON_Duplicate(snap.observed, true));
            em_engine_save(e, op, payload);
            cJSON_Delete(payload);
        }
    }
    cJSON_Delete(ops);
    em_snapshot_clear(&snap);
}
