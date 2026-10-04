/* SPDX-License-Identifier: Apache-2.0 */
/* The fleet (spec §4), as emosa.agent.fleet: AL MAC derivation, the registry, the agent
 * configuration, identifying a pod at the front port and handing it over, and releasing
 * it. The files it keeps are the reference's, byte for byte (STATE_ROOT/fleet.json,
 * CONFIG_DIR/<pod>.json), so either fleet takes over from the other. The front port's
 * sockets and the agents' units are emosa-fleet-c's (fleetd.c). */
#ifndef EMOSA_FLEET_H
#define EMOSA_FLEET_H

#include <cjson/cJSON.h>

#include "common.h"

/* A locally administered unicast AL MAC from the serial, avoiding `taken` (spec §2.2). */
bool em_derive_al(const char *serial, const char *const *taken, size_t ntaken, char out[18]);

/* A usable serial: [A-Za-z0-9][A-Za-z0-9._-]{0,63} (also the agent's name and files). */
bool em_serial_valid(const char *serial);

typedef struct {
    char pod_id[65], al_mac[18], interface[16];
    int port;
    unsigned handovers;
    char *node_id, *model, *firmware; /* the pod's AWLAN_Node values; NULL: null */
    double first_seen, last_seen;
} em_fleet_entry;

typedef struct {
    int port_low, port_high;
    char reserved_al[18]; /* the controller's AL */
    size_t count, cap;
    em_fleet_entry *entries;
} em_registry;

void em_registry_free(em_registry *r);
/* The entries of a registry file (fleet-registry schema); false when it is not one. */
bool em_registry_load(em_registry *r, const char *text);
/* The registry file's text: json.dumps(indent=2, sort_keys=True) and a newline. */
char *em_registry_dump(const em_registry *r);

/* The pod's entry, allocated on first sight; NULL when no port is free. The pointer is
 * valid until the next assignment. */
em_fleet_entry *em_registry_assign(em_registry *r, const char *serial, const char *node_id,
                                   const char *model, const char *firmware, double now);
/* Removes the serial's entry into *out (the caller frees it with em_fleet_entry_clear);
 * false when there is none. */
bool em_registry_forget(em_registry *r, const char *serial, em_fleet_entry *out);
void em_fleet_entry_clear(em_fleet_entry *e);
/* The entry as the registry keeps it (fleet-registry schema). */
cJSON *em_fleet_entry_json(const em_fleet_entry *e);

/* The agent configuration (agent-config schema) for an entry under a fleet config. */
cJSON *em_agent_config(const em_fleet_entry *e, const cJSON *fleet);
/* Its file's text: json.dumps(indent=2) and a newline. */
char *em_agent_config_text(const em_fleet_entry *e, const cJSON *fleet);

/* The handover transaction: AWLAN_Node.manager_addr = tcp:<advertise>:<port>. */
cJSON *em_manager_update(const em_fleet_entry *e, const cJSON *fleet);

/* -- a fleet: its configuration and files ------------------------------------------ */

typedef struct {
    cJSON *config; /* owned; validated by the caller (fleet-config schema) */
    const char *database, *state_root, *config_dir;
    em_registry registry;
    char registry_path[512];
} em_fleet;

/* Takes config. False (*why) when the registry file exists but cannot be read. */
bool em_fleet_open(em_fleet *f, cJSON *config, char *why, size_t size);
void em_fleet_close(em_fleet *f);

/* The front port's first request: transact params selecting AWLAN_Node's identity. */
cJSON *em_fleet_select(const em_fleet *f);

typedef enum {
    EM_FLEET_HANDOVER, /* hand the pod to its agent */
    EM_FLEET_REFUSED,  /* left unchanged: not admitted, or no free port */
    EM_FLEET_INVALID,  /* the pod's identity is unusable: left unchanged */
    EM_FLEET_FAILED,   /* a file could not be written */
} em_fleet_outcome;

typedef struct {
    em_fleet_entry entry; /* HANDOVER: the pod's entry (identity strings not kept) */
    char serial[65];      /* the serial, when usable */
    char node[65];        /* the pod's node ID, for the log (cut to fit) */
    bool changed;         /* its agent configuration changed: restart, not start */
    cJSON *update;        /* the transact params that hand it over (owned) */
    char why[192];        /* otherwise: what to log */
} em_fleet_handover;

/* The pod identified by the select's result: admitted, its entry assigned and saved,
 * its agent configuration written when new or changed (spec §4 steps 1 to 4). */
em_fleet_outcome em_fleet_identify(em_fleet *f, const cJSON *select_result, double now,
                                   em_fleet_handover *out);
void em_fleet_handover_clear(em_fleet_handover *h);
/* At the fleet's start: each admitted entry of the registry, in its order (by serial), its
 * agent configuration written when new or changed and start(ctx, pod_id, changed) called,
 * as step 4 does, without waiting for the pod at the front port (an image upgrade keeps
 * the files, not the agents' enabled units). The number of entries whose configuration
 * could not be written (their agents not started); SIZE_MAX when the registry is
 * unreadable. */
size_t em_fleet_start_registered(em_fleet *f, void (*start)(void *ctx, const char *pod_id, bool changed),
                                 void *ctx);

/* The handover's update succeeded: one row changed, no error. */
bool em_fleet_update_ok(const cJSON *result);

/* forget SERIAL: the entry removed and saved, stop(pod_id) called, the configuration
 * deleted and the state directory renamed to <pod>.released-<stamp>. The entry as JSON
 * (with archived_state when there was one), or NULL when the serial is unknown. */
cJSON *em_fleet_forget(em_fleet *f, const char *serial, const char *stamp,
                       void (*stop)(void *ctx, const char *pod_id), void *ctx);

#endif
