/* SPDX-License-Identifier: Apache-2.0 */
/* The node-config scope, as emosa.opensync.node_config.NodeConfigBackend and
 * emosa.agent.node_config.NodeConfig (spec 4, the pods' modules): each configured module's settings
 * written as Node_Config rows at most once per OpenSync start, with its own journal, and the
 * module's Node_State read into the status (its state, boot, version, update) with its events
 * logged once each. */
#ifndef EMOSA_SCOPE_NODE_H
#define EMOSA_SCOPE_NODE_H

#include "engine.h"
#include "ovsdb.h"
#include "southbound.h"

#define EM_NODE_MODULES 4  /* modules at most */
#define EM_NODE_SETTINGS 16 /* settings of one module at most */

typedef struct {
    char module[33];
    char boot[1025]; /* the boot_id whose events were logged, while has_boot */
    bool has_boot;
    long seq; /* the last event logged in that boot */
    bool started;    /* an update reported ok and not running yet: since `since` */
    double since;
} em_node_seen;

typedef struct {
    em_ovsdb *ovs;
    const char *serial, *run_id;
    cJSON *intent; /* the record: {"pod_id", "modules": {module: {key: value}}}, owned */
    cJSON *(*transact)(void *ctx, const cJSON *operations);
    void *transact_ctx;
    em_journal *journal;
    em_engine engine;
    char instance[17]; /* which start of the pod's OpenSync */
    bool has_instance;
    em_snapshot last;
    bool has_last;
    char waiting[128]; /* why nothing is written now, "" when nothing waits */
    bool has_waiting;
    double (*monotonic)(void);
    em_node_seen seen[EM_NODE_MODULES];
    size_t nseen;
} em_node_scope;

/* The intent from the agent configuration's modules ({module: {key: value}}): names, sizes and
 * counts checked (NodeConfigIntent.validate); false with *why. */
bool em_node_intent_from(const char *pod_id, const cJSON *modules, em_node_scope *s, em_reason *why);

/* Opens <state_dir>/node-config (the scope's journal) and recovers it. */
em_reason em_node_open(em_node_scope *s, const char *state_dir, const em_journal_schemas *schemas,
                       const em_vault *vault, double (*monotonic)(void));
void em_node_close(em_node_scope *s);

/* One step on the agent's refresh cadence. */
void em_node_tick(em_node_scope *s);

/* NodeConfig.status: {"modules": {module: {"settings_written", "settings_echoed", "state",
 * "boot_id", "version", "running", "slot", "update", "update_sha256", "flags"}}, "differs",
 * "waiting", "operation"}. */
cJSON *em_node_status(em_node_scope *s);

#endif
