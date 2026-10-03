/* Fuzz target: what a pod sends the fleet's front port (fleetd.c), and the fleet's
 * registry file. The input is a byte stream as it arrives on the connection: JSON-RPC
 * messages back to back (jsonrpc.c framing), each answered if an echo, and a reply to the
 * fleet's select identified as a pod (fleet.c), with admission refusing every serial so
 * nothing is written. The same bytes are then read as a registry file. */
#include "fleet.h"
#include "jsonrpc.h"

#include <stdlib.h>
#include <string.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    static em_fleet fleet;
    static bool ready;
    if (!ready) {
        em_init();
        cJSON *config = cJSON_Parse("{\"listen\": \"ptcp:6650:127.0.0.1\", \"advertise\": \"10.0.0.1\","
                                    " \"ports\": [6651, 6652], \"controller_al\": \"02:00:00:00:00:02\","
                                    " \"state_root\": \"/nonexistent/emosa-fuzz\", \"config_dir\": \"/nonexistent\","
                                    " \"admit\": []}");
        char why[256];
        if (!em_fleet_open(&fleet, config, why, sizeof(why)))
            abort();
        ready = true;
    }
    const char *stream = (const char *)data;
    size_t start = 0;
    while (start < size) {
        size_t at, end;
        bool complete = em_json_next(stream + start, size - start, &at, &end);
        start += at;
        if (!complete)
            break;
        cJSON *m = cJSON_ParseWithLength(stream + start, end - at);
        start += end - at;
        if (!m)
            break;
        cJSON_Delete(em_rpc_echo_reply(m));
        const cJSON *result = NULL;
        if (em_rpc_is_reply(m, 1, &result) && result) {
            em_fleet_handover h;
            em_fleet_identify(&fleet, result, 0, &h); /* refused or invalid: nothing written */
            em_fleet_handover_clear(&h);
            (void)em_fleet_update_ok(result);
        }
        cJSON_Delete(m);
    }
    char *text = malloc(size + 1);
    if (!text)
        return 0;
    memcpy(text, data, size);
    text[size] = 0;
    em_registry registry = {.port_low = 6651, .port_high = 6652};
    if (em_registry_load(&registry, text)) {
        free(em_registry_dump(&registry));
        em_registry_assign(&registry, "MVXPOD023F87E628DD", NULL, NULL, NULL, 1.5);
    }
    em_registry_free(&registry);
    free(text);
    return 0;
}
