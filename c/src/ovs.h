/* RFC 7047 values of OVSDB rows ({table: {uuid: row}}), read without the schema. */
#ifndef EMOSA_OVS_H
#define EMOSA_OVS_H

#include <cjson/cJSON.h>

#include "common.h"

/* A string column (["set", []] reads as NULL). */
const char *ovs_str(const cJSON *row, const char *column);
/* An integer column; false when absent or empty. */
bool ovs_int(const cJSON *row, const char *column, long *out);
/* True only for the boolean true. */
bool ovs_true(const cJSON *row, const char *column);
/* A UUID set (or single UUID) column, sorted like the reference's decoded sets. */
size_t ovs_uuids(const cJSON *row, const char *column, const char **out, size_t max);
/* A map column's value for a key, or NULL. */
const char *ovs_map_get(const cJSON *row, const char *column, const char *key);

/* The strings of a set column (a single atom reads as one), sorted. */
size_t ovs_strings(const cJSON *row, const char *column, const char **out, size_t max);
/* The keys of a map column, sorted. */
size_t ovs_map_keys(const cJSON *row, const char *column, const char **out, size_t max);
/* A map column's size (0 for an empty or absent map). */
size_t ovs_map_size(const cJSON *row, const char *column);

#endif
