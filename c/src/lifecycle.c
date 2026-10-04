/* SPDX-License-Identifier: Apache-2.0 */
/* The agent's onboarding attempts and its own renewals. */
#include "lifecycle.h"

#define DISCOVERY_TIMEOUT 5
#define SEARCHES 3
#define CONTROLLER_TIMEOUT 130
#define UNSERVED_RENEW 60
#define M2_TIMEOUT 30

static void end(em_attempts *a, double now)
{
    unsigned backoff = 1u << (a->failures < 5 ? a->failures : 5);
    a->next_start = now + (backoff < 30 ? backoff : 30);
    a->failures++;
    a->state = EM_SESSION_NONE;
}

em_attempt_step em_attempts_tick(em_attempts *a, double now, bool source_ok, int generation)
{
    em_attempt_step step = {0};
    if (a->state == EM_SESSION_INCOMPATIBLE)
        return step; /* terminal: only a renewal starts again (OnboardingRecovery) */
    if (a->state == EM_SESSION_FAILED || a->state == EM_SESSION_SOURCE_LOST) {
        /* ended between ticks (a refused admission): the back-off begins now */
        step.ended = a->state == EM_SESSION_FAILED ? "failed" : "source_lost";
        end(a, now);
        return step;
    }
    if (a->state == EM_SESSION_NONE) {
        if (now < a->next_start || !source_ok)
            return step;
        a->state = EM_SESSION_DISCOVERING;
        a->generation = generation;
        a->starts++;
        a->searches = 0;
        a->next_search = now;
        a->discovery_deadline = now + DISCOVERY_TIMEOUT;
        step.start = true;
    } else if (!source_ok || generation != a->generation) {
        step.ended = "source_lost";
        end(a, now);
        return step;
    }
    if (a->state == EM_SESSION_DISCOVERING) {
        if (now >= a->discovery_deadline) {
            step.ended = "discovery_timeout";
            end(a, now);
            return step;
        }
        if (now >= a->next_search && a->searches < SEARCHES) {
            step.search = true;
            a->searches++;
            a->next_search = now + 1;
        }
    }
    if (a->state == EM_SESSION_PROVISIONING)
        a->failures = 0;
    return step;
}

void em_attempts_renew(em_attempts *a)
{
    a->state = EM_SESSION_NONE;
    a->failures = 0;
    a->next_start = 0;
}

void em_reannounce_provisioned(em_reannounce *r, unsigned responses)
{
    if (!r->marked) {
        r->marked = true;
        r->mark = responses;
    }
}

bool em_reannounce_due(em_reannounce *r, unsigned responses)
{
    if (r->done || !r->marked || responses <= r->mark)
        return false;
    r->done = true;
    return true;
}

void em_renew_init(em_renew *r, double now)
{
    r->last_contact = now;
    r->has_unserved = r->has_awaiting = false;
}

void em_renew_contact(em_renew *r, double now) { r->last_contact = now; }

size_t em_renew_check(em_renew *r, double now, bool unserved, bool awaiting, const char *reasons[3])
{
    size_t n = 0;
    if (unserved) {
        if (!r->has_unserved) {
            r->unserved_since = now;
            r->has_unserved = true;
        }
    } else {
        r->has_unserved = false;
    }
    if (r->has_unserved && now - r->unserved_since > UNSERVED_RENEW) {
        reasons[n++] = "unserved";
        r->has_unserved = false;
        awaiting = false; /* the renewal ended the session */
    }
    if (awaiting) {
        if (!r->has_awaiting) {
            r->awaiting_since = now;
            r->has_awaiting = true;
        }
    } else {
        r->has_awaiting = false;
    }
    if (r->has_awaiting && now - r->awaiting_since > M2_TIMEOUT) {
        reasons[n++] = "no_m2";
        r->has_awaiting = false;
    }
    if (now - r->last_contact > CONTROLLER_TIMEOUT) {
        reasons[n++] = "controller_silent";
        r->last_contact = now;
    }
    return n;
}
