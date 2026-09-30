/* The agent's attempts at onboarding, as emosa.wire.onboarding.OnboardingRecovery with
 * the session's discovery (spec §2.5), and when it asks for a fresh attempt on its own,
 * as emosa.agent.renew.RenewRules:
 *
 * - an attempt starts while the pod's State is available and any back-off has passed;
 * - discovery sends a Search at once and every second, three at most, and fails when
 *   no Response came within five seconds;
 * - a failed attempt, or one whose source was lost, ends and the next waits
 *   min(30, 2^failures) seconds; provisioning resets the failures;
 * - a renewal ends the attempt and the next starts at once. */
#ifndef EMOSA_LIFECYCLE_H
#define EMOSA_LIFECYCLE_H

#include "common.h"

typedef enum {
    EM_SESSION_NONE,
    EM_SESSION_DISCOVERING,
    EM_SESSION_AWAITING_M2,
    EM_SESSION_PROVISIONING,
    EM_SESSION_FAILED,
    EM_SESSION_SOURCE_LOST,
} em_session_state;

typedef struct {
    em_session_state state;
    int generation; /* the pod's database generation the attempt began on */
    unsigned starts, failures, searches;
    double next_start, next_search, discovery_deadline;
} em_attempts;

/* What one tick asks of the agent, in order. */
typedef struct {
    bool start;         /* a new attempt: discovery begins */
    bool search;        /* send one AP-Autoconfiguration Search */
    const char *ended;  /* the attempt ended: "discovery_timeout", "source_lost", "failed" */
} em_attempt_step;

/* One tick. source_ok: the pod's State is current; generation: its database generation. */
em_attempt_step em_attempts_tick(em_attempts *a, double now, bool source_ok, int generation);
/* A renewal (AP-Autoconfiguration Renew or a rule below): a fresh attempt at once. */
void em_attempts_renew(em_attempts *a);

/* emosa.agent.renew.RenewRules */
typedef struct {
    double last_contact, unserved_since, awaiting_since;
    bool has_unserved, has_awaiting;
} em_renew;

void em_renew_init(em_renew *r, double now);
void em_renew_contact(em_renew *r, double now);
/* The reasons to renew now, in order ("unserved", "no_m2", "controller_silent"); returns
 * how many (at most three). */
size_t em_renew_check(em_renew *r, double now, bool unserved, bool awaiting, const char *reasons[3]);

/* emosa.wire.onboarding.ClientReannouncement: a provisioned session announces every
 * current client again, once, after the controller's first Topology Query answered since
 * M2 was accepted (a restarted controller may drop earlier announcements). Counted in
 * Topology Responses sent by the session. */
typedef struct {
    bool marked, done;
    unsigned mark;
} em_reannounce;

/* M2 accepted, with responses Topology Responses sent so far (the first call counts). */
void em_reannounce_provisioned(em_reannounce *r, unsigned responses);
/* True once: announce the clients again now. */
bool em_reannounce_due(em_reannounce *r, unsigned responses);

#endif
