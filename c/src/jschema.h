/* SPDX-License-Identifier: Apache-2.0 */
/* Validation against the repository's JSON Schemas (schemas/NAME.schema.json), the ones the
 * reference validates with jsonschema: the agent configuration at start, journal records
 * and the status. The keywords those schemas use: type, enum, const, properties,
 * required, additionalProperties, items, min/maxItems, uniqueItems, min/maxLength,
 * pattern (POSIX extended), minimum, maximum, oneOf, anyOf, allOf, if/then/else,
 * propertyNames, format date-time and local $ref. Others are annotations. */
#ifndef EMOSA_JSCHEMA_H
#define EMOSA_JSCHEMA_H

#include <cjson/cJSON.h>

#include "common.h"

typedef struct em_schema em_schema;

/* <directory>/<name>.schema.json; NULL when absent or not JSON. */
em_schema *em_schema_load(const char *directory, const char *name);
void em_schema_free(em_schema *s);

/* True when value conforms. Otherwise *where names the first failing location
 * ("/uplink/bssid: pattern") in a buffer of `size`, never the rejected value. */
bool em_schema_valid(const em_schema *s, const cJSON *value, char *where, size_t size);

/* The schema directory: EMOSA_SCHEMAS, else the installed kit's. */
const char *em_schema_directory(void);

#endif
