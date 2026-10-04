/* SPDX-License-Identifier: Apache-2.0 */
/* RFC 7047 JSON-RPC framing: messages are JSON objects back to back on a stream, with
 * no length prefix. Shared by the agent's OVSDB session (ovsdb.c) and the fleet's front
 * port (fleetd.c); both read from a pod, so this parser is fuzzed (c/fuzz/ovsdb.c,
 * c/fuzz/fleet.c). */
#ifndef EMOSA_JSONRPC_H
#define EMOSA_JSONRPC_H

#include <cjson/cJSON.h>

#include "common.h"

/* The next complete JSON object or array in buf[0..len): where it starts (after
 * whitespace) and ends (one past its closing bracket). False when none is complete yet;
 * *start then says how much leading whitespace may be dropped. */
bool em_json_next(const char *buf, size_t len, size_t *start, size_t *end);

/* em_json_next on a stream that grows: the scan resumes where it stopped, so a message
 * read in pieces is scanned once, not once per piece. buf is what is pending since the
 * last complete message; after false the caller drops *start bytes from its front (the
 * scanner has accounted for them), after true it takes [*start, *end) and passes what
 * follows. Zeroed for a new stream. */
typedef struct {
    size_t at, start, depth;
    bool started, string, escape;
} em_json_scanner;
bool em_json_scan(em_json_scanner *sc, const char *buf, size_t len, size_t *start, size_t *end);

/* {"method": METHOD, "params": PARAMS, "id": ID} (params owned by the result). */
cJSON *em_rpc_request(const char *method, cJSON *params, long id);
/* The reply to an echo request (its params as the result), or NULL when the message is
 * not one. */
cJSON *em_rpc_echo_reply(const cJSON *message);
/* Whether message is the reply to id; *result its result (borrowed) when it succeeded,
 * NULL when the peer answered with an error. */
bool em_rpc_is_reply(const cJSON *message, long id, const cJSON **result);

#endif
