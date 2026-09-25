/* RFC 7047 values of OVSDB rows, read without the schema (the columns EMOSA uses). */
#include "ovs.h"

#include <stdlib.h>
#include <string.h>

static const cJSON *column(const cJSON *row, const char *name)
{
    return cJSON_GetObjectItemCaseSensitive(row, name);
}

/* ["set", [x]] of a max-1 column reads as x, ["set", []] as nothing. */
static const cJSON *scalar(const cJSON *v)
{
    if (cJSON_IsArray(v) && cJSON_GetArraySize(v) == 2 && cJSON_IsString(cJSON_GetArrayItem(v, 0)) &&
        !strcmp(cJSON_GetArrayItem(v, 0)->valuestring, "set")) {
        const cJSON *items = cJSON_GetArrayItem(v, 1);
        return cJSON_GetArraySize(items) == 1 ? cJSON_GetArrayItem(items, 0) : NULL;
    }
    return v;
}

const char *ovs_str(const cJSON *row, const char *name)
{
    const cJSON *v = scalar(column(row, name));
    if (cJSON_IsArray(v) && cJSON_GetArraySize(v) == 2) /* ["uuid", ...] */
        v = cJSON_GetArrayItem(v, 1);
    return cJSON_IsString(v) ? v->valuestring : NULL;
}

bool ovs_int(const cJSON *row, const char *name, long *out)
{
    const cJSON *v = scalar(column(row, name));
    if (!cJSON_IsNumber(v))
        return false;
    *out = (long)v->valuedouble;
    return true;
}

bool ovs_true(const cJSON *row, const char *name)
{
    return cJSON_IsTrue(scalar(column(row, name)));
}

static int cmp(const void *a, const void *b)
{
    return strcmp(*(const char *const *)a, *(const char *const *)b);
}

size_t ovs_uuids(const cJSON *row, const char *name, const char **out, size_t max)
{
    const cJSON *v = column(row, name);
    size_t n = 0;
    if (!cJSON_IsArray(v) || cJSON_GetArraySize(v) != 2)
        return 0;
    const char *tag = cJSON_GetArrayItem(v, 0)->valuestring;
    if (tag && !strcmp(tag, "uuid")) {
        out[n++] = cJSON_GetArrayItem(v, 1)->valuestring;
    } else if (tag && !strcmp(tag, "set")) {
        const cJSON *item;
        cJSON_ArrayForEach(item, cJSON_GetArrayItem(v, 1))
        {
            if (n < max && cJSON_IsArray(item) && cJSON_GetArraySize(item) == 2)
                out[n++] = cJSON_GetArrayItem(item, 1)->valuestring;
        }
    }
    qsort(out, n, sizeof(*out), cmp);
    return n;
}

const char *ovs_map_get(const cJSON *row, const char *name, const char *key)
{
    const cJSON *v = column(row, name), *pair;
    if (!cJSON_IsArray(v) || cJSON_GetArraySize(v) != 2)
        return NULL;
    cJSON_ArrayForEach(pair, cJSON_GetArrayItem(v, 1))
    {
        const cJSON *k = cJSON_GetArrayItem(pair, 0), *x = cJSON_GetArrayItem(pair, 1);
        if (cJSON_IsString(k) && !strcmp(k->valuestring, key) && cJSON_IsString(x))
            return x->valuestring;
    }
    return NULL;
}
