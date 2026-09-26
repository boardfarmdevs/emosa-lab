/* The fleet (spec §4): AL MAC derivation, the registry, the agent configuration and
 * the handover, as emosa.agent.fleet. */
#ifndef EMOSA_FLEET_H
#define EMOSA_FLEET_H

#include <cjson/cJSON.h>

#include "common.h"

/* A locally administered unicast AL MAC from the serial, avoiding `taken` (spec §2.2). */
bool em_derive_al(const char *serial, const char *const *taken, size_t ntaken, char out[18]);

typedef struct {
    char pod_id[65], al_mac[18], interface[16];
    int port;
    unsigned handovers;
    char node_id[65], model[65], firmware[65];
    double first_seen, last_seen;
} em_fleet_entry;

typedef struct {
    int port_low, port_high;
    char reserved_al[18]; /* the controller's AL */
    size_t count;
    em_fleet_entry entries[256];
} em_registry;

/* The pod's entry, allocated on first sight; NULL when no port is free. */
em_fleet_entry *em_registry_assign(em_registry *r, const char *serial, const char *node_id,
                                   const char *model, const char *firmware, double now);

/* The agent configuration (agent-config schema) for an entry under a fleet config. */
cJSON *em_agent_config(const em_fleet_entry *e, const cJSON *fleet);

/* The handover transaction: AWLAN_Node.manager_addr = tcp:<advertise>:<port>. */
cJSON *em_manager_update(const em_fleet_entry *e, const cJSON *fleet);

#endif
