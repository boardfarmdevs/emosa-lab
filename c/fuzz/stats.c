/* SPDX-License-Identifier: Apache-2.0 */
/* Fuzz target: the pod's statistics over MQTT (stats.c: the pinned sts.Report
 * protobuf, as the broker delivers it). Each input is one message on the pod's
 * topic, given twice so a report following another is exercised too. */
#include "stats.h"

#include <stdlib.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

static double fixed_clock(void *ctx)
{
    (void)ctx;
    return 1000.0;
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    static bool ready;
    if (!ready) {
        em_init();
        ready = true;
    }
    em_pod_stats *s = em_malloc(sizeof(*s));
    em_pod_stats_init(s, "emosa/stats/FUZZ", 5, fixed_clock, NULL);
    em_pod_stats_receive(s, "emosa/stats/FUZZ", data, size, false);
    em_pod_stats_receive(s, "emosa/stats/FUZZ", data, size, true);
    cJSON_Delete(em_pod_stats_status(s));
    free(s);
    return 0;
}
