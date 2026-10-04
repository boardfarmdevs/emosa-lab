/* SPDX-License-Identifier: Apache-2.0 */
/* JSON text exactly as the reference writes it (Python's json.dumps), for the values
 * that are hashed or compared as text: fingerprints (sort_keys, "," and ":"), the start
 * instance (default separators) and journal records. */
#ifndef EMOSA_CANON_H
#define EMOSA_CANON_H

#include <cjson/cJSON.h>

#include "common.h"

typedef enum {
    EM_JSON_COMPACT,  /* separators=(",", ":") */
    EM_JSON_DEFAULT,  /* separators=(", ", ": "), json.dumps' default */
    EM_JSON_INDENT2,  /* indent=2: the files the fleet writes (registry, agent configurations) */
} em_json_style;

/* json.dumps(value, sort_keys=sort, separators=style): ASCII only (non-ASCII as
 * \uXXXX, surrogate pairs above the BMP), integral numbers without a fraction.
 * Caller frees; NULL on allocation failure or an invalid UTF-8 string. */
char *em_json_dumps(const cJSON *value, em_json_style style, bool sort);

/* sha256 of text, as lower-case hex (65 bytes with the NUL). */
void em_sha256_hex(const void *data, size_t len, char out[65]);

/* HMAC-SHA256 of text with key, as lower-case hex. */
void em_hmac_hex(const uint8_t *key, size_t key_len, const void *data, size_t len, char out[65]);

/* A random UUID4, "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx", and its 32-digit hex form. */
void em_uuid4(char out[37]);
void em_uuid4_hex(char out[33]);

/* UTC now as the reference writes it: 2026-09-29T12:34:56.789Z (clock.utc_now). */
void em_utc_now(char out[32]);
/* Seconds between two such timestamps (b - a); false when either is unparsable. */
bool em_utc_diff(const char *a, const char *b, double *seconds);
/* a + seconds, in the Python isoformat the engine writes for deadlines
 * (datetime.fromisoformat(now) + timedelta): "...T12:34:56.789000+00:00". */
bool em_utc_add(const char *a, double seconds, char out[40]);

#endif
