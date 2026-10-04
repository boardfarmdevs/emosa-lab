/* SPDX-License-Identifier: Apache-2.0 */
/* Fuzz target: the pod's OVSDB rows (JSON from the pod, over its management
 * connection), as the agent turns them into its view of the pod (view.c) and its
 * uplink state. The input is the tables object as JSON text. */
#include "view.h"

#include <stdlib.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    static bool ready;
    if (!ready) {
        em_init();
        ready = true;
    }
    cJSON *tables = cJSON_ParseWithLength((const char *)data, size);
    if (!tables)
        return 0;
    em_device_view *view = em_malloc(sizeof(*view));
    if (em_device_view_from_rows(tables, view) == EM_OK) {
        cJSON_Delete(em_device_view_json(view));
        em_backhaul bh;
        if (em_view_backhaul(view, "bhaul-sta-50", &bh))
            cJSON_Delete(em_backhaul_json(&bh));
    }
    em_uplink_state uplink;
    em_uplink_state_of(tables, "bhaul-sta-50", &uplink);
    cJSON_Delete(em_uplink_state_json(&uplink));
    free(view);
    cJSON_Delete(tables);
    return 0;
}
