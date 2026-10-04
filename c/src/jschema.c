/* SPDX-License-Identifier: Apache-2.0 */
/* A JSON Schema subset validator for the repository's own schemas. */
#include "jschema.h"

#include <math.h>
#include <regex.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

struct em_schema {
    cJSON *root;
};

em_schema *em_schema_load(const char *directory, const char *name)
{
    char path[700];
    if (!em_format(path, sizeof(path), "%s/%s.schema.json", directory, name))
        return NULL;
    char *text = em_read_file(path, 1 << 20, NULL);
    if (!text)
        return NULL;
    cJSON *root = cJSON_Parse(text);
    free(text);
    if (!root)
        return NULL;
    em_schema *s = em_calloc(1, sizeof(*s));
    s->root = root;
    return s;
}

void em_schema_free(em_schema *s)
{
    if (s) {
        cJSON_Delete(s->root);
        free(s);
    }
}

const char *em_schema_directory(void)
{
    const char *d = getenv("EMOSA_SCHEMAS");
    return d && *d ? d : EMOSA_SCHEMAS_DIR; /* the build's (CMake EMOSA_SCHEMAS_DIR) */
}

typedef struct {
    const cJSON *root;
    char *where;
    size_t size;
    bool quiet; /* inside oneOf/anyOf/if: failures are not reported */
} ctx;

static bool fail(ctx *c, const char *path, const char *rule)
{
    if (!c->quiet && c->where && c->size)
        (void)em_format(c->where, c->size, "%s: %s", *path ? path : "/", rule); /* a message */
    return false;
}

static size_t code_points(const char *s)
{
    size_t n = 0;
    for (; *s; s++)
        n += ((unsigned char)*s & 0xC0) != 0x80;
    return n;
}

static bool is_integer(const cJSON *v)
{
    return cJSON_IsNumber(v) && isfinite(v->valuedouble) && v->valuedouble == floor(v->valuedouble);
}

static bool type_is(const cJSON *v, const char *type)
{
    if (!strcmp(type, "object"))
        return cJSON_IsObject(v);
    if (!strcmp(type, "array"))
        return cJSON_IsArray(v);
    if (!strcmp(type, "string"))
        return cJSON_IsString(v);
    if (!strcmp(type, "boolean"))
        return cJSON_IsBool(v);
    if (!strcmp(type, "null"))
        return cJSON_IsNull(v);
    if (!strcmp(type, "number"))
        return cJSON_IsNumber(v);
    if (!strcmp(type, "integer"))
        return is_integer(v);
    return false;
}

/* JSON equality, numbers by value (const, enum, uniqueItems) */
static bool equal(const cJSON *a, const cJSON *b)
{
    if (cJSON_IsNumber(a) && cJSON_IsNumber(b))
        return a->valuedouble == b->valuedouble;
    return cJSON_Compare(a, b, true);
}

static bool date_time(const char *s)
{
    struct tm tm = {0};
    int consumed = 0;
    if (sscanf(s, "%4d-%2d-%2dT%2d:%2d:%2d%n", &tm.tm_year, &tm.tm_mon, &tm.tm_mday, &tm.tm_hour, &tm.tm_min, /* NOLINT(cert-err34-c): QUALITY.md §5 */
               &tm.tm_sec, &consumed) != 6 || tm.tm_mon < 1 || tm.tm_mon > 12 || tm.tm_mday < 1 ||
        tm.tm_mday > 31 || tm.tm_hour > 23 || tm.tm_min > 59 || tm.tm_sec > 60)
        return false;
    const char *p = s + consumed;
    if (*p == '.') {
        p++;
        if (*p < '0' || *p > '9')
            return false;
        while (*p >= '0' && *p <= '9')
            p++;
    }
    if (!strcmp(p, "Z") || !strcmp(p, "z"))
        return true;
    int hh, mm;
    return (p[0] == '+' || p[0] == '-') && sscanf(p + 1, "%2d:%2d", &hh, &mm) == 2 && strlen(p) == 6; /* NOLINT(cert-err34-c): QUALITY.md §5 */
}

static const cJSON *resolve_ref(ctx *c, const char *ref)
{
    if (strncmp(ref, "#/", 2))
        return NULL;
    const cJSON *node = c->root;
    char part[128];
    for (const char *p = ref + 2; node && *p;) {
        size_t n = strcspn(p, "/");
        if (n >= sizeof(part))
            return NULL;
        memcpy(part, p, n);
        part[n] = 0;
        node = cJSON_GetObjectItemCaseSensitive(node, part);
        p += n + (p[n] == '/');
    }
    return node;
}

static bool valid(ctx *c, const cJSON *schema, const cJSON *v, const char *path);

static bool quietly(ctx *c, const cJSON *schema, const cJSON *v, const char *path)
{
    bool was = c->quiet;
    c->quiet = true;
    bool ok = valid(c, schema, v, path);
    c->quiet = was;
    return ok;
}

static bool valid(ctx *c, const cJSON *schema, const cJSON *v, const char *path)
{
    if (cJSON_IsBool(schema))
        return cJSON_IsTrue(schema) ? true : fail(c, path, "false schema");
    if (!cJSON_IsObject(schema))
        return true;
    const cJSON *k;
    char sub[512];
    if ((k = cJSON_GetObjectItemCaseSensitive(schema, "$ref"))) {
        const cJSON *target = cJSON_IsString(k) ? resolve_ref(c, k->valuestring) : NULL;
        if (!target || !valid(c, target, v, path))
            return target ? false : fail(c, path, "$ref");
    }
    if ((k = cJSON_GetObjectItemCaseSensitive(schema, "type"))) {
        bool ok = false;
        if (cJSON_IsString(k)) {
            ok = type_is(v, k->valuestring);
        } else {
            const cJSON *t;
            cJSON_ArrayForEach(t, k) ok = ok || (cJSON_IsString(t) && type_is(v, t->valuestring));
        }
        if (!ok)
            return fail(c, path, "type");
    }
    if ((k = cJSON_GetObjectItemCaseSensitive(schema, "const")) && !equal(k, v))
        return fail(c, path, "const");
    if ((k = cJSON_GetObjectItemCaseSensitive(schema, "enum"))) {
        bool ok = false;
        const cJSON *e;
        cJSON_ArrayForEach(e, k) ok = ok || equal(e, v);
        if (!ok)
            return fail(c, path, "enum");
    }
    if (cJSON_IsString(v)) {
        size_t n = code_points(v->valuestring);
        if ((k = cJSON_GetObjectItemCaseSensitive(schema, "minLength")) && n < (size_t)k->valuedouble)
            return fail(c, path, "minLength");
        if ((k = cJSON_GetObjectItemCaseSensitive(schema, "maxLength")) && n > (size_t)k->valuedouble)
            return fail(c, path, "maxLength");
        if ((k = cJSON_GetObjectItemCaseSensitive(schema, "pattern")) && cJSON_IsString(k)) {
            regex_t re;
            if (regcomp(&re, k->valuestring, REG_EXTENDED | REG_NOSUB))
                return fail(c, path, "pattern (unsupported)");
            bool ok = regexec(&re, v->valuestring, 0, NULL, 0) == 0;
            regfree(&re);
            if (!ok)
                return fail(c, path, "pattern");
        }
        if ((k = cJSON_GetObjectItemCaseSensitive(schema, "format")) && cJSON_IsString(k) &&
            !strcmp(k->valuestring, "date-time") && !date_time(v->valuestring))
            return fail(c, path, "format");
    }
    if (cJSON_IsNumber(v)) {
        if ((k = cJSON_GetObjectItemCaseSensitive(schema, "minimum")) && v->valuedouble < k->valuedouble)
            return fail(c, path, "minimum");
        if ((k = cJSON_GetObjectItemCaseSensitive(schema, "maximum")) && v->valuedouble > k->valuedouble)
            return fail(c, path, "maximum");
        if ((k = cJSON_GetObjectItemCaseSensitive(schema, "exclusiveMinimum")) && v->valuedouble <= k->valuedouble)
            return fail(c, path, "exclusiveMinimum");
        if ((k = cJSON_GetObjectItemCaseSensitive(schema, "exclusiveMaximum")) && v->valuedouble >= k->valuedouble)
            return fail(c, path, "exclusiveMaximum");
    }
    if (cJSON_IsArray(v)) {
        int n = cJSON_GetArraySize(v);
        if ((k = cJSON_GetObjectItemCaseSensitive(schema, "minItems")) && n < (int)k->valuedouble)
            return fail(c, path, "minItems");
        if ((k = cJSON_GetObjectItemCaseSensitive(schema, "maxItems")) && n > (int)k->valuedouble)
            return fail(c, path, "maxItems");
        if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(schema, "uniqueItems")))
            for (int i = 0; i < n; i++)
                for (int j = i + 1; j < n; j++)
                    if (equal(cJSON_GetArrayItem(v, i), cJSON_GetArrayItem(v, j)))
                        return fail(c, path, "uniqueItems");
        if ((k = cJSON_GetObjectItemCaseSensitive(schema, "items")) && cJSON_IsObject(k)) {
            int i = 0;
            const cJSON *item;
            cJSON_ArrayForEach(item, v)
            {
                (void)em_format(sub, sizeof(sub), "%s/%d", path, i++); /* a location for messages */
                if (!valid(c, k, item, sub))
                    return false;
            }
        }
    }
    if (cJSON_IsObject(v)) {
        const cJSON *props = cJSON_GetObjectItemCaseSensitive(schema, "properties");
        const cJSON *more = cJSON_GetObjectItemCaseSensitive(schema, "additionalProperties");
        const cJSON *names = cJSON_GetObjectItemCaseSensitive(schema, "propertyNames");
        if ((k = cJSON_GetObjectItemCaseSensitive(schema, "required"))) {
            const cJSON *r;
            cJSON_ArrayForEach(r, k) if (cJSON_IsString(r) && !cJSON_GetObjectItemCaseSensitive(v, r->valuestring))
            {
                (void)em_format(sub, sizeof(sub), "%s/%s", path, r->valuestring); /* a location for messages */
                return fail(c, sub, "required");
            }
        }
        const cJSON *member;
        cJSON_ArrayForEach(member, v)
        {
            (void)em_format(sub, sizeof(sub), "%s/%s", path, member->string); /* a location for messages */
            if (names) {
                cJSON *name = cJSON_CreateString(member->string);
                bool ok = valid(c, names, name, sub);
                cJSON_Delete(name);
                if (!ok)
                    return false;
            }
            const cJSON *p = props ? cJSON_GetObjectItemCaseSensitive(props, member->string) : NULL;
            if (p) {
                if (!valid(c, p, member, sub))
                    return false;
            } else if (more && !valid(c, more, member, sub)) {
                return cJSON_IsFalse(more) ? fail(c, sub, "additionalProperties") : false;
            }
        }
    }
    if ((k = cJSON_GetObjectItemCaseSensitive(schema, "allOf"))) {
        const cJSON *s;
        cJSON_ArrayForEach(s, k) if (!valid(c, s, v, path)) return false;
    }
    if ((k = cJSON_GetObjectItemCaseSensitive(schema, "anyOf"))) {
        bool ok = false;
        const cJSON *s;
        cJSON_ArrayForEach(s, k) ok = ok || quietly(c, s, v, path);
        if (!ok)
            return fail(c, path, "anyOf");
    }
    if ((k = cJSON_GetObjectItemCaseSensitive(schema, "oneOf"))) {
        int n = 0;
        const cJSON *s;
        cJSON_ArrayForEach(s, k) n += quietly(c, s, v, path);
        if (n != 1)
            return fail(c, path, "oneOf");
    }
    if ((k = cJSON_GetObjectItemCaseSensitive(schema, "if"))) {
        const cJSON *then = cJSON_GetObjectItemCaseSensitive(schema, "then");
        const cJSON *otherwise = cJSON_GetObjectItemCaseSensitive(schema, "else");
        if (quietly(c, k, v, path)) {
            if (then && !valid(c, then, v, path))
                return false;
        } else if (otherwise && !valid(c, otherwise, v, path)) {
            return false;
        }
    }
    if ((k = cJSON_GetObjectItemCaseSensitive(schema, "not")) && quietly(c, k, v, path))
        return fail(c, path, "not");
    return true;
}

bool em_schema_valid(const em_schema *s, const cJSON *value, char *where, size_t size)
{
    if (where && size)
        where[0] = 0;
    if (!s)
        return false;
    ctx c = {s->root, where, size, false};
    return valid(&c, s->root, value, "");
}
