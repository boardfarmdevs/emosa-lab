/* SPDX-License-Identifier: Apache-2.0 */
/* The Early AP Capability Report's delivery (EasyMesh 6.1), as the reference's
 * ReportCoordinator (emosa.wire.coordinator): sent before M1, retransmitted with a new
 * MID every 250 ms, three transmissions at most, until an Ack names one of its MIDs
 * within one second. The caller sends the report; this keeps the schedule and matches
 * the Acks. */
#ifndef EMOSA_EARLY_H
#define EMOSA_EARLY_H

#include <cjson/cJSON.h>

#include "common.h"

typedef struct {
    bool pending;
    unsigned transmissions;
    uint16_t mids[3];
    double next_retry, deadline;
    struct {
        size_t n;
        struct {
            char name[32];
            unsigned count;
        } items[8];
    } counts;
} em_early;

/* A new report to deliver: the caller transmits it now (and calls em_early_sent). */
void em_early_start(em_early *e, double now);
void em_early_sent(em_early *e, uint16_t mid, double now);
/* On every tick: true when a retransmission is due now (the caller transmits it).
 * source_same: the pod's State is current and unchanged since the report was built;
 * otherwise the report is dropped (early_source_invalidated), as the reference drops one
 * whose source stamp changed. */
bool em_early_tick(em_early *e, double now, bool source_same);
/* A 1905 Ack (with an Error Code or security companion: error_tlv): early_acknowledged,
 * ack_error_rejected or unmatched_ack. */
const char *em_early_ack(em_early *e, uint16_t mid, bool error_tlv, double now);
void em_early_close(em_early *e);
cJSON *em_early_counts(const em_early *e);

#endif
