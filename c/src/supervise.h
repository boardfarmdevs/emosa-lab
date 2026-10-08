/* SPDX-License-Identifier: Apache-2.0 */
/* The fleet's agents as its own children, for a system without systemd (a router's busybox
 * init): fleet-config "agents": "supervised", or EMOSA_AGENTS=supervised (spec §4). What
 * emosa-agent@.service does under systemd, the fleet does here: each pod's agent gets the
 * fleet's environment with /etc/default/emosa and /etc/default/emosa-POD (the later file
 * wins), its link helper runs first (emosa-agent-link POD), then the agent itself
 * (emosa-agent-c CONFIG_DIR/POD.json). An agent that ends is started again after 3 s while
 * its configuration exists; one whose configuration is removed (the pod forgotten) is
 * stopped; all of them are stopped when the fleet ends, and die with it if it is killed.
 * Their standard output and error go to LOG_ROOT/POD/agent.log (the run root, else the
 * state root), which is moved to agent.log.1 when it passes a size, so at most two files
 * per pod. */
#ifndef EMOSA_SUPERVISE_H
#define EMOSA_SUPERVISE_H

#include <sys/types.h>

#include "common.h"

typedef struct {
    const char *agent;      /* the agent program when the environment has no EMOSA_AGENT */
    const char *link;       /* the link helper when it has no EMOSA_AGENT_LINK */
    const char *config_dir; /* CONFIG_DIR/POD.json, each agent's configuration */
    const char *log_root;   /* LOG_ROOT/POD/agent.log and agent.pid */
    const char *defaults;   /* where the environment files are: /etc/default */
    double restart_delay;   /* seconds from an agent's end to its next start: 3 */
    double stop_timeout;    /* seconds from SIGTERM to SIGKILL: 5 */
    long log_max;           /* bytes of agent.log before it becomes agent.log.1 */
} em_sup_config;

typedef struct em_supervisor em_supervisor;

/* The strings of config are copied. */
em_supervisor *em_sup_new(const em_sup_config *config);
/* Every child stopped (SIGTERM, then SIGKILL after stop_timeout) and reaped; freed. */
void em_sup_free(em_supervisor *s);

/* The pod's agent started, or restarted when restart (its configuration changed). A ticket
 * for em_sup_started, or 0 when the agent already runs and nothing else was asked. */
unsigned em_sup_start(em_supervisor *s, const char *pod, bool restart, double now);
/* The ticket's start: 1 the agent runs (its link helper succeeded), -1 it failed (the
 * helper failed, the agent could not be started or its configuration is gone), 0 not yet. */
int em_sup_started(const em_supervisor *s, const char *pod, unsigned ticket);
/* Children reaped and agents started again, stopped or killed as due, logs bounded.
 * now: monotonic seconds. Never blocks. */
void em_sup_step(em_supervisor *s, double now);
/* The pod's running agent, 0 when none. */
pid_t em_sup_pid(const em_supervisor *s, const char *pod);

/* An environment file's line: KEY=VALUE (an optional "export ", the value's surrounding
 * quotes removed, no escapes). False for a comment, a blank or another line. */
bool em_sup_env_line(const char *line, char *key, size_t key_size, char *value, size_t value_size);
/* KEY's value in an environment file; false when the file or the key is missing. */
bool em_sup_file_value(const char *path, const char *key, char *value, size_t size);
/* base, then each file's lines (a missing file skipped), then extra ("KEY=VALUE"): a later
 * KEY replaces an earlier one. NULL-terminated; freed with em_sup_env_free. */
char **em_sup_environment(char *const *base, const char *const *files, size_t nfiles,
                          const char *const *extra);
void em_sup_env_free(char **env);

/* From another process (forget): the pod's agent recorded in LOG_ROOT/POD/agent.pid sent
 * SIGTERM, waited for up to timeout seconds, then killed. True when none runs any more. */
bool em_sup_stop_recorded(const char *log_root, const char *pod, double timeout);

#endif
