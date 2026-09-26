/* The pod's own statistics (spec §3.6): sts.Report client reports summed per counter
 * epoch, as emosa.opensync.stats. */
#ifndef EMOSA_STATS_H
#define EMOSA_STATS_H

#include <cjson/cJSON.h>

#include "common.h"

#define EM_COUNTERS 8 /* tx_bytes rx_bytes tx_frames rx_frames tx_retries rx_retries tx_errors rx_errors */

typedef struct {
    char mac[18], ssid[65], band[8];
    unsigned channel, periods;
    double measured_at, epoch_start;
    bool has_tx_rate, has_rx_rate, has_snr;
    double tx_rate, rx_rate;
    unsigned snr;
    bool known[EM_COUNTERS];
    uint64_t counters[EM_COUNTERS];
} em_station_stats;

/* The last raw on-channel survey sample of a channel: its busy percentage (spec §3.8). */
typedef struct {
    unsigned channel, busy_percent, duration_ms;
    bool has_duration;
    char band[8];
    double measured_at;
} em_survey_stats;

typedef struct {
    char topic[160];
    unsigned interval;
    double lifetime;
    double (*clock)(void *ctx);
    void *clock_ctx;
    bool has_last[7];
    uint64_t last_timestamp[7]; /* per band */
    size_t nstations;
    em_station_stats stations[256];
    size_t nsurveys;
    em_survey_stats surveys[16];
    bool measured[EM_COUNTERS];
    unsigned accepted, rejected, gaps;
    char last_error[96];
    bool has_error, has_report;
    double last_report_at;
} em_pod_stats;

void em_pod_stats_init(em_pod_stats *s, const char *topic, unsigned interval,
                       double (*clock)(void *), void *clock_ctx);
/* One MQTT message; false (and counted) when it is not a usable report. */
bool em_pod_stats_receive(em_pod_stats *s, const char *topic, const uint8_t *payload, size_t len,
                          bool retained);
/* The status as the agent reports it (last_report_at included). */
cJSON *em_pod_stats_status(em_pod_stats *s);

#endif
