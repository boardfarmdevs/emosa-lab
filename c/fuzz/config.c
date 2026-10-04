/* SPDX-License-Identifier: Apache-2.0 */
/* Fuzz target: the configurations the programs read (QUALITY.md §2: the agent's from the
 * fleet, the fleet's and the GTP's from their files), as JSON text: the schema validator
 * (jschema.c) against agent-config, fleet-config and gtp-config, the GTP's rules beyond its
 * schema, and the agent's telemetry settings. The schemas are found through EMOSA_SCHEMAS,
 * else schemas/ from the working directory. */
#include "gtp.h"
#include "jschema.h"
#include "scope_telemetry.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

static em_schema *agent, *fleet, *gtp;

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    static bool ready;
    if (!ready) {
        em_init();
        const char *dir = getenv("EMOSA_SCHEMAS") ? getenv("EMOSA_SCHEMAS") : "schemas";
        agent = em_schema_load(dir, "agent-config");
        fleet = em_schema_load(dir, "fleet-config");
        gtp = em_schema_load(dir, "gtp-config");
        if (!agent || !fleet || !gtp) {
            fprintf(stderr, "config fuzz target: the schemas in %s unusable\n", dir);
            abort();
        }
        ready = true;
    }
    cJSON *config = cJSON_ParseWithLength((const char *)data, size);
    if (!config)
        return 0;
    char where[256];
    if (em_schema_valid(agent, config, where, sizeof(where))) {
        /* as the agent reads it: the telemetry settings when their mode is mqtt */
        const cJSON *telemetry = cJSON_GetObjectItemCaseSensitive(config, "telemetry");
        const char *mode = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(telemetry, "mode"));
        em_telemetry_intent intent;
        em_reason why;
        if (mode && !strcmp(mode, "mqtt"))
            (void)em_telemetry_intent_from(cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(config, "pod_id")),
                                           cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(config, "serial")),
                                           telemetry, &intent, &why);
    }
    (void)em_schema_valid(fleet, config, where, sizeof(where));
    if (em_schema_valid(gtp, config, where, sizeof(where)))
        (void)em_gtp_check(config, where, sizeof(where));
    cJSON_Delete(config);
    return 0;
}
