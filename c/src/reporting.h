/* SPDX-License-Identifier: Apache-2.0 */
/* The controller's Multi-AP Policy and the AP metrics the agent reports, as
 * emosa.wire.reporting_policy (ReportingPolicyStore, ReportingPolicyCoordinator) and
 * emosa.wire.pod_metrics (PodMetricReporter):
 *
 * - a Multi-AP Policy Config Request (0x8003) is decoded whole, persisted in
 *   <state_dir>/reporting-policy.sqlite (with the OS boot identity: the schedule is
 *   rebased after a reboot) and acknowledged;
 * - when its Metric Reporting Policy sets an interval, a report is due every interval:
 *   reserved in the store first, then sent (one current report, never a burst);
 * - with the pod's statistics (telemetry with the survey, and the profile's declared
 *   best-effort ESP) the report is an AP Metrics Response from them, also the answer to
 *   an AP Metrics Query (0x800B). Without them nothing is sent: the due work is counted. */
#ifndef EMOSA_REPORTING_H
#define EMOSA_REPORTING_H

#include <cjson/cJSON.h>

#include "cmdu.h"
#include "stats.h"
#include "view.h"

typedef struct em_policy_store em_policy_store;

em_policy_store *em_policy_store_open(const char *path, const char *boot_id);
void em_policy_store_close(em_policy_store *s);
const char *em_policy_store_boot_id(const em_policy_store *s);
/* The record as kept on disk (written only when a policy is received), or NULL. */
cJSON *em_policy_store_read(em_policy_store *s);

/* What a report is built from: the represented radio (its BSSes and stations), the
 * pod's statistics and the declared ESP. NULL stats: no pod metrics. */
typedef struct {
    const em_radio_view *radio;
    const em_pod_stats *stats;
    const uint8_t *esp_be; /* 3 octets */
    double freshness;      /* seconds a statistic stays current */
    double (*wall)(void);  /* epoch seconds */
} em_metric_source;

typedef struct {
    em_policy_store *store;
    cJSON *value; /* the stored record, or NULL */
    uint8_t controller[6], local_al[6], ruid[6];
    size_t nrecent;
    struct {
        uint16_t mid;
        char digest[65];
        double until;
    } recent[64];
    struct {
        size_t n;
        struct {
            char name[64];
            unsigned count;
        } items[16];
    } counts, reporter_counts;
    bool closed;
} em_reporting;

/* One per session, over the agent's store (spec §3.8): the kept policy is read again
 * and the schedule starts at now, its first report one interval after. */
void em_reporting_start(em_reporting *r, em_policy_store *store, const uint8_t controller[6],
                        const uint8_t local_al[6], const uint8_t ruid[6], double now);
void em_reporting_close(em_reporting *r);

/* Frames the caller sends; mid is the agent's next MID for its own messages. */
typedef uint16_t (*em_next_mid)(void *ctx);

/* The due-report accounting on every tick: sends a periodic report when one is due
 * and admitted (provisioning) with a source. */
void em_reporting_tick(em_reporting *r, double now, bool admitted, const em_metric_source *source,
                       em_next_mid next_mid, void *mid_ctx, em_frames *out);

/* 0x8003: the result label (policy_receipt_ack_sent), or NULL and *error when refused. */
const char *em_reporting_policy(em_reporting *r, const em_message *m, double now, bool admitted,
                                const em_metric_source *source, em_next_mid next_mid, void *mid_ctx,
                                em_frames *out, em_reason *error);

/* 0x800B: ap_metric_query_answered or ap_measurements_unavailable; NULL and *error when refused. */
const char *em_reporting_query(em_reporting *r, const em_message *m, bool admitted, const em_metric_source *source,
                               em_frames *out, em_reason *error);

/* The session status' reporting_policy and ap_metrics (with_source: pod metrics). */
cJSON *em_reporting_status(const em_reporting *r);
cJSON *em_reporting_metrics_status(const em_reporting *r, const em_metric_source *source);

/* A growable TLV list (owned values). */
typedef struct {
    size_t count, cap;
    em_tlv *tlvs;
} em_tlv_vec;
void em_tlv_vec_free(em_tlv_vec *v);

/* The pod metric TLVs for bssids (NULL: every BSS); false when no fresh survey or no
 * queried BSS (the reference's NOT_READY). */
bool em_pod_metric_tlvs(const em_metric_source *source, const cJSON *policy, const uint8_t (*bssids)[6],
                        size_t nbssids, em_tlv_vec *out);

#endif
