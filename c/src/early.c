/* SPDX-License-Identifier: Apache-2.0 */
/* The Early AP Capability Report's delivery (emosa.wire.coordinator.ReportCoordinator). */
#include "early.h"

#include <stdio.h>
#include <string.h>

static const char *record(em_early *e, const char *event)
{
    for (size_t i = 0; i < e->counts.n; i++)
        if (!strcmp(e->counts.items[i].name, event)) {
            e->counts.items[i].count++;
            return event;
        }
    if (e->counts.n < 8) {
        EM_FORMAT_FIXED(e->counts.items[e->counts.n].name, sizeof(e->counts.items[0].name), "%s", event);
        e->counts.items[e->counts.n++].count = 1;
    }
    return event;
}

void em_early_start(em_early *e, double now)
{
    e->pending = true;
    e->transmissions = 0;
    e->deadline = now + 1;
    e->next_retry = now;
}

void em_early_sent(em_early *e, uint16_t mid, double now)
{
    if (e->transmissions < 3)
        e->mids[e->transmissions] = mid;
    e->transmissions++;
    e->next_retry = now + 0.25;
    record(e, "early_sent");
}

bool em_early_tick(em_early *e, double now, bool source_same)
{
    if (!e->pending)
        return false;
    if (!source_same) {
        e->pending = false;
        record(e, "early_source_invalidated");
        return false;
    }
    if (now >= e->deadline) {
        e->pending = false;
        record(e, "early_ack_timeout");
        return false;
    }
    return now >= e->next_retry && e->transmissions < 3;
}

const char *em_early_ack(em_early *e, uint16_t mid, bool error_tlv, double now)
{
    if (e->pending && now >= e->deadline) {
        e->pending = false;
        record(e, "early_ack_timeout");
    }
    bool ours = false;
    for (unsigned i = 0; e->pending && i < e->transmissions && i < 3; i++)
        ours = ours || e->mids[i] == mid;
    if (!ours)
        return record(e, "unmatched_ack");
    /* no Error Code applies to an Early Report: a companion is no clean receipt */
    if (error_tlv)
        return record(e, "ack_error_rejected");
    e->pending = false;
    return record(e, "early_acknowledged");
}

void em_early_close(em_early *e) { e->pending = false; }

cJSON *em_early_counts(const em_early *e)
{
    cJSON *o = cJSON_CreateObject();
    for (size_t i = 0; i < e->counts.n; i++)
        cJSON_AddNumberToObject(o, e->counts.items[i].name, e->counts.items[i].count);
    return o;
}
