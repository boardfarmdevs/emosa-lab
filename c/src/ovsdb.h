/* SPDX-License-Identifier: Apache-2.0 */
/* The agent's OVSDB session (spec §3.2 I2): the pod connects to the agent's listener and
 * the agent is the JSON-RPC client (get_schema, monitor, transact), as
 * emosa.opensync.session. One connection at a time; a new one is a new generation. */
#ifndef EMOSA_OVSDB_H
#define EMOSA_OVSDB_H

#include <cjson/cJSON.h>

#include "common.h"

typedef struct em_ovsdb em_ovsdb;

/* listen: "ptcp:PORT:ADDRESS" (the agent configuration's ovsdb). tables: "Table" (all its
 * columns monitored) or "Table:a,b" (only those). */
em_ovsdb *em_ovsdb_open(const char *listen, const char *const *tables, size_t ntables);
void em_ovsdb_close(em_ovsdb *s);
/* A session over recorded rows, always ready, with no pod (vectors and tests): the
 * scopes' writes go to their own transact callback. */
em_ovsdb *em_ovsdb_fixed(const cJSON *tables, int generation, const char *schema_fingerprint);

/* File descriptors to poll (listener and, when connected, the pod); returns the count. */
size_t em_ovsdb_fds(const em_ovsdb *s, int *fds, size_t max);
/* Accept a pod, read what arrived, answer echoes, apply monitor updates; never blocks.
 * Returns false when the pod connection was lost this call. */
bool em_ovsdb_pump(em_ovsdb *s);

/* Connected and monitored: the cache is the pod's current rows. */
bool em_ovsdb_ready(const em_ovsdb *s);
int em_ovsdb_generation(const em_ovsdb *s);
/* The connected pod's schema fingerprint (sha256 of its compact sorted JSON). */
const char *em_ovsdb_schema_fingerprint(const em_ovsdb *s);
unsigned long em_ovsdb_revision(const em_ovsdb *s);
/* The monitor cache, {table: {uuid: row}} in RFC 7047 form. */
const cJSON *em_ovsdb_tables(const em_ovsdb *s);
/* Bytes from the pod, as the connection delivers them: each complete JSON-RPC message
 * handled (echoes answered, monitor updates applied to the rows); false when the stream is
 * not JSON-RPC or more than a message's budget waits. The socket path reads through it. */
bool em_ovsdb_input(em_ovsdb *s, const char *data, size_t len);

/* One transaction, waiting at most `timeout` seconds for its reply (updates keep being
 * applied meanwhile). Returns the results array (caller owns), or NULL on loss/timeout. */
cJSON *em_ovsdb_transact(em_ovsdb *s, const cJSON *operations, double timeout);

#endif
