/* SPDX-License-Identifier: Apache-2.0 */
/* JSON text as the reference writes it, hashes and time stamps. */
#include "canon.h"

#include <math.h>
#include <openssl/hmac.h>
#include <openssl/rand.h>
#include <openssl/sha.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static bool put(em_buf *b, const char *text) { return em_buf_put(b, text, strlen(text)); }

/* One UTF-8 code point from s (advancing it), or -1 when invalid. */
static long utf8_next(const unsigned char **s)
{
    const unsigned char *p = *s;
    long c;
    int more;
    if (p[0] < 0x80) {
        c = p[0];
        more = 0;
    } else if ((p[0] & 0xE0) == 0xC0) {
        c = p[0] & 0x1F;
        more = 1;
    } else if ((p[0] & 0xF0) == 0xE0) {
        c = p[0] & 0x0F;
        more = 2;
    } else if ((p[0] & 0xF8) == 0xF0) {
        c = p[0] & 0x07;
        more = 3;
    } else {
        return -1;
    }
    for (int i = 1; i <= more; i++) {
        if ((p[i] & 0xC0) != 0x80)
            return -1;
        c = (c << 6) | (p[i] & 0x3F);
    }
    *s = p + 1 + more;
    return c;
}

/* json.dumps' ensure_ascii string encoding */
static bool put_string(em_buf *b, const char *text)
{
    char tmp[32];
    if (!em_buf_u8(b, '"'))
        return false;
    const unsigned char *s = (const unsigned char *)text;
    while (*s) {
        long c = utf8_next(&s);
        if (c < 0)
            return false;
        switch (c) {
        case '"': put(b, "\\\""); break;
        case '\\': put(b, "\\\\"); break;
        case '\n': put(b, "\\n"); break;
        case '\r': put(b, "\\r"); break;
        case '\t': put(b, "\\t"); break;
        case '\b': put(b, "\\b"); break;
        case '\f': put(b, "\\f"); break;
        default:
            if (c < 0x20 || (c >= 0x7F && c < 0x10000)) { /* json.dumps keeps only ' '..'~' */
                EM_FORMAT_FIXED(tmp, sizeof(tmp), "\\u%04lx", c);
                put(b, tmp);
            } else if (c >= 0x10000) {
                long v = c - 0x10000;
                EM_FORMAT_FIXED(tmp, sizeof(tmp), "\\u%04lx\\u%04lx", 0xD800 + (v >> 10), 0xDC00 + (v & 0x3FF));
                put(b, tmp);
            } else {
                em_buf_u8(b, (uint8_t)c);
            }
        }
    }
    return em_buf_u8(b, '"');
}

/* Python's repr of a float: the shortest text that reads back as the same double. */
static void put_number(em_buf *b, double v)
{
    char tmp[40];
    if (isfinite(v) && v == floor(v) && fabs(v) < 1e16) {
        EM_FORMAT_FIXED(tmp, sizeof(tmp), "%.0f", v); /* under 1e16: at most 17 characters */
    } else {
        for (int precision = 1; precision <= 17; precision++) {
            EM_FORMAT_FIXED(tmp, sizeof(tmp), "%.*g", precision, v); /* 17 digits, sign, exponent */
            if (strtod(tmp, NULL) == v)
                break;
        }
        if (!strchr(tmp, '.') && !strchr(tmp, 'e') && !strchr(tmp, 'n') && !strchr(tmp, 'i'))
            strcat(tmp, ".0");
    }
    put(b, tmp);
}

static int compare_keys(const void *a, const void *b)
{
    /* Python sorts str keys by code point; UTF-8 byte order is the same order */
    return strcmp((*(const cJSON *const *)a)->string, (*(const cJSON *const *)b)->string);
}

/* before an item of a container at depth: indent=2 starts it on a line of its own */
static bool put_item_start(em_buf *b, em_json_style style, size_t depth, bool first)
{
    if (style != EM_JSON_INDENT2)
        return first || put(b, style == EM_JSON_COMPACT ? "," : ", ");
    if (!first && !put(b, ","))
        return false;
    if (!put(b, "\n"))
        return false;
    for (size_t i = 0; i <= depth; i++)
        if (!put(b, "  "))
            return false;
    return true;
}

/* the end of a non-empty container at depth */
static bool put_end(em_buf *b, em_json_style style, size_t depth, const char *close)
{
    if (style == EM_JSON_INDENT2) {
        if (!put(b, "\n"))
            return false;
        for (size_t i = 0; i < depth; i++)
            if (!put(b, "  "))
                return false;
    }
    return put(b, close);
}

static bool dump(em_buf *b, const cJSON *v, em_json_style style, bool sort, size_t depth)
{
    const char *key_sep = style == EM_JSON_COMPACT ? ":" : ": ";
    if (cJSON_IsNull(v))
        return put(b, "null");
    if (cJSON_IsTrue(v))
        return put(b, "true");
    if (cJSON_IsFalse(v))
        return put(b, "false");
    if (cJSON_IsNumber(v)) {
        put_number(b, v->valuedouble);
        return true;
    }
    if (cJSON_IsString(v))
        return put_string(b, v->valuestring);
    if (cJSON_IsArray(v)) {
        if (!cJSON_GetArraySize(v))
            return put(b, "[]");
        put(b, "[");
        bool first = true;
        const cJSON *item;
        cJSON_ArrayForEach(item, v)
        {
            if (!put_item_start(b, style, depth, first) || !dump(b, item, style, sort, depth + 1))
                return false;
            first = false;
        }
        return put_end(b, style, depth, "]");
    }
    if (cJSON_IsObject(v)) {
        size_t n = (size_t)cJSON_GetArraySize(v), i = 0;
        const cJSON **items = em_calloc(n, sizeof(*items));
        const cJSON *item;
        cJSON_ArrayForEach(item, v) items[i++] = item;
        if (sort && n > 1)
            em_sort(items, n, sizeof(*items), compare_keys);
        if (!n) {
            free(items);
            return put(b, "{}");
        }
        put(b, "{");
        bool ok = true;
        for (i = 0; ok && i < n; i++)
            ok = put_item_start(b, style, depth, i == 0) && put_string(b, items[i]->string) &&
                 put(b, key_sep) && dump(b, items[i], style, sort, depth + 1);
        free(items);
        return ok && put_end(b, style, depth, "}");
    }
    return false;
}

char *em_json_dumps(const cJSON *value, em_json_style style, bool sort)
{
    em_buf b = {0};
    if (!dump(&b, value, style, sort, 0) || !em_buf_u8(&b, 0)) {
        em_buf_free(&b);
        return NULL;
    }
    return (char *)b.data;
}

static void hex(const uint8_t *d, size_t n, char *out)
{
    for (size_t i = 0; i < n; i++)
        EM_FORMAT_FIXED(out + 2 * i, 3, "%02x", d[i]);
    out[2 * n] = 0;
}

void em_sha256_hex(const void *data, size_t len, char out[65])
{
    uint8_t d[32];
    SHA256(data, len, d);
    hex(d, 32, out);
}

void em_hmac_hex(const uint8_t *key, size_t key_len, const void *data, size_t len, char out[65])
{
    uint8_t d[32];
    unsigned n = 32;
    HMAC(EVP_sha256(), key, (int)key_len, data, len, d, &n);
    hex(d, 32, out);
}

void em_uuid4_hex(char out[33])
{
    uint8_t b[16];
    RAND_bytes(b, sizeof(b));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    hex(b, 16, out);
}

void em_uuid4(char out[37])
{
    char h[33];
    em_uuid4_hex(h);
    EM_FORMAT_FIXED(out, 37, "%.8s-%.4s-%.4s-%.4s-%.12s", h, h + 8, h + 12, h + 16, h + 20);
}

void em_utc_now(char out[32])
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    struct tm tm;
    char base[24];
    /* the system clock: a year of four digits (a failure here is the platform's) */
    if (!gmtime_r(&ts.tv_sec, &tm) || !strftime(base, sizeof(base), "%Y-%m-%dT%H:%M:%S", &tm))
        abort();
    EM_FORMAT_FIXED(out, 32, "%.20s.%03dZ", base, (int)(ts.tv_nsec / 1000000));
}

/* seconds since the epoch of "YYYY-MM-DDTHH:MM:SS[.ffffff](Z|+00:00)" */
static bool parse_utc(const char *text, double *out)
{
    struct tm tm = {0};
    int consumed = 0;
    if (!text || sscanf(text, "%4d-%2d-%2dT%2d:%2d:%2d%n", &tm.tm_year, &tm.tm_mon, &tm.tm_mday, &tm.tm_hour, /* NOLINT(cert-err34-c): QUALITY.md §5 */
                        &tm.tm_min, &tm.tm_sec, &consumed) != 6)
        return false;
    tm.tm_year -= 1900;
    tm.tm_mon -= 1;
    const char *p = text + consumed;
    double fraction = 0;
    if (*p == '.') {
        double scale = 0.1;
        for (p++; *p >= '0' && *p <= '9'; p++) {
            fraction += (*p - '0') * scale;
            scale /= 10;
        }
    }
    if (strcmp(p, "Z") && strcmp(p, "+00:00"))
        return false;
    *out = (double)timegm(&tm) + fraction;
    return true;
}

bool em_utc_diff(const char *a, const char *b, double *seconds)
{
    double x, y;
    if (!parse_utc(a, &x) || !parse_utc(b, &y))
        return false;
    *seconds = y - x;
    return true;
}

bool em_utc_add(const char *a, double seconds, char out[40])
{
    double t;
    if (!parse_utc(a, &t))
        return false;
    t += seconds;
    time_t whole = (time_t)floor(t);
    long micro = lround((t - (double)whole) * 1e6);
    if (micro >= 1000000) {
        whole++;
        micro -= 1000000;
    }
    struct tm tm;
    char base[24];
    /* a time the sum carried beyond what gmtime or four year digits hold is no time */
    if (!gmtime_r(&whole, &tm) || tm.tm_year + 1900 > 9999 || !strftime(base, sizeof(base), "%Y-%m-%dT%H:%M:%S", &tm))
        return false;
    if (micro)
        EM_FORMAT_FIXED(out, 40, "%.20s.%06d+00:00", base, (int)micro);
    else
        EM_FORMAT_FIXED(out, 40, "%.20s+00:00", base);
    return true;
}
