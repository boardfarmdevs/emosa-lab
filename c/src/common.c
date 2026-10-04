/* SPDX-License-Identifier: Apache-2.0 */
/* EMOSA C: shared helpers. */
#include "common.h"

#include <cjson/cJSON.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void out_of_memory(size_t size)
{
    (void)fprintf(stderr, "emosa: out of memory (%zu bytes)\n", size);
    abort();
}

void *em_malloc(size_t size)
{
    void *p = malloc(size ? size : 1);
    if (!p)
        out_of_memory(size);
    return p;
}

void *em_calloc(size_t n, size_t size)
{
    void *p = calloc(n ? n : 1, size ? size : 1);
    if (!p)
        out_of_memory(n * size);
    return p;
}

void *em_realloc(void *p, size_t size)
{
    void *grown = realloc(p, size ? size : 1);
    if (!grown)
        out_of_memory(size);
    return grown;
}

char *em_strdup(const char *s)
{
    size_t n = strlen(s) + 1;
    return memcpy(em_malloc(n), s, n);
}

void em_init(void)
{
    cJSON_Hooks hooks = {em_malloc, free};
    cJSON_InitHooks(&hooks);
}

const char *em_reason_name(em_reason reason)
{
    switch (reason) {
    case EM_OK: return "OK";
    case EM_INVALID_INPUT: return "INVALID_INPUT";
    case EM_UNSUPPORTED_OPERATION: return "UNSUPPORTED_OPERATION";
    case EM_BUSY: return "BUSY";
    case EM_NOT_READY: return "NOT_READY";
    case EM_PRECONDITION_FAILED: return "PRECONDITION_FAILED";
    case EM_OUTCOME_UNKNOWN: return "OUTCOME_UNKNOWN";
    case EM_OWNERSHIP_CONFLICT: return "OWNERSHIP_CONFLICT";
    case EM_NO_MEMORY: return "NO_MEMORY";
    case EM_SCHEMA_MISMATCH: return "SCHEMA_MISMATCH";
    case EM_APPLY_TIMEOUT: return "APPLY_TIMEOUT";
    case EM_MISSING_PREREQUISITE: return "MISSING_PREREQUISITE";
    case EM_NOT_FOUND: return "NOT_FOUND";
    }
    return "UNKNOWN";
}

em_reason em_reason_parse(const char *name)
{
    for (int r = EM_INVALID_INPUT; name && r <= EM_NOT_FOUND; r++)
        if (!strcmp(em_reason_name((em_reason)r), name))
            return (em_reason)r;
    return EM_OK;
}

void em_buf_free(em_buf *b)
{
    free(b->data);
    b->data = NULL;
    b->len = b->cap = 0;
}

bool em_buf_put(em_buf *b, const void *data, size_t len)
{
    if (b->len + len > b->cap) {
        size_t cap = b->cap ? b->cap : 64;
        while (cap < b->len + len)
            cap *= 2;
        uint8_t *grown = em_realloc(b->data, cap);
        if (!grown)
            return false;
        b->data = grown;
        b->cap = cap;
    }
    if (len)
        memcpy(b->data + b->len, data, len);
    b->len += len;
    return true;
}

bool em_buf_u8(em_buf *b, uint8_t v) { return em_buf_put(b, &v, 1); }

bool em_buf_u16(em_buf *b, uint16_t v)
{
    uint8_t x[2] = {(uint8_t)(v >> 8), (uint8_t)v};
    return em_buf_put(b, x, 2);
}

bool em_buf_u32(em_buf *b, uint32_t v)
{
    uint8_t x[4] = {(uint8_t)(v >> 24), (uint8_t)(v >> 16), (uint8_t)(v >> 8), (uint8_t)v};
    return em_buf_put(b, x, 4);
}

char *em_hex(const uint8_t *data, size_t len)
{
    char *text = em_malloc(len * 2 + 1);
    if (!text)
        return NULL;
    for (size_t i = 0; i < len; i++)
        EM_FORMAT_FIXED(text + 2 * i, 3, "%02x", data[i]);
    text[len * 2] = 0;
    return text;
}

char *em_mac_str(const uint8_t mac[6])
{
    char *text = em_malloc(18);
    if (text)
        EM_FORMAT_FIXED(text, 18, "%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2], mac[3], mac[4],
                mac[5]);
    return text;
}

static int nibble(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

bool em_unhex(const char *text, em_buf *out)
{
    size_t n = strlen(text);
    if (n % 2)
        return false;
    for (size_t i = 0; i < n; i += 2) {
        int hi = nibble(text[i]), lo = nibble(text[i + 1]);
        if (hi < 0 || lo < 0 || !em_buf_u8(out, (uint8_t)(hi << 4 | lo)))
            return false;
    }
    return true;
}

bool em_mac_text_ok(const char *text)
{
    if (!text || strlen(text) != 17)
        return false;
    for (size_t i = 0; i < 17; i++) {
        char c = text[i];
        bool ok = i % 3 == 2 ? c == ':' : (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
        if (!ok)
            return false;
    }
    return true;
}

bool em_parse_mac(const char *text, uint8_t mac[6])
{
    if (strlen(text) != 17)
        return false;
    for (int i = 0; i < 6; i++) {
        int hi = nibble(text[3 * i]), lo = nibble(text[3 * i + 1]);
        if (hi < 0 || lo < 0 || (i < 5 && text[3 * i + 2] != ':'))
            return false;
        mac[i] = (uint8_t)(hi << 4 | lo);
    }
    return true;
}

void em_sort(void *base, size_t n, size_t size, int (*compare)(const void *, const void *))
{
    if (n > 1)
        qsort(base, n, size, compare);
}

char *em_read_file(const char *path, size_t max, size_t *len)
{
    FILE *f = fopen(path, "rb");
    if (!f)
        return NULL;
    size_t size = 0, cap = 4096;
    char *text = em_malloc(cap);
    bool ok = true;
    for (;;) {
        if (cap - size < 2) {
            if (cap > max) {
                ok = false;
                break;
            }
            cap *= 2;
            text = em_realloc(text, cap);
        }
        size_t got = fread(text + size, 1, cap - size - 1, f);
        size += got;
        if (got == 0) {
            ok = !ferror(f);
            break;
        }
    }
    (void)fclose(f); /* read only: nothing is lost if closing fails */
    if (!ok || size > max) {
        free(text);
        return NULL;
    }
    text[size] = 0;
    if (len)
        *len = size;
    return text;
}

bool em_write_file(const char *path, const char *text, bool durable)
{
    size_t n = strlen(path) + 5;
    char *tmp = em_malloc(n);
    int written = snprintf(tmp, n, "%s.tmp", path);
    FILE *f = written > 0 && (size_t)written < n ? fopen(tmp, "w") : NULL;
    bool ok = f != NULL;
    if (f) {
        ok = fputs(text, f) >= 0 && fflush(f) == 0 && (!durable || fsync(fileno(f)) == 0);
        ok = fclose(f) == 0 && ok;
        ok = ok && rename(tmp, path) == 0;
        if (!ok)
            (void)unlink(tmp);
    }
    free(tmp);
    return ok;
}

bool em_copy(char *dst, size_t size, const char *src)
{
    if (!size)
        return false;
    size_t n = strlen(src);
    bool fits = n < size;
    if (!fits)
        n = size - 1;
    memcpy(dst, src, n);
    dst[n] = 0;
    return fits;
}

static bool vformat(char *dst, size_t size, const char *fmt, va_list ap) __attribute__((format(printf, 3, 0)));
static bool vformat(char *dst, size_t size, const char *fmt, va_list ap)
{
    int n = vsnprintf(dst, size, fmt, ap);
    return n >= 0 && (size_t)n < size;
}

bool em_format(char *dst, size_t size, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    bool ok = vformat(dst, size, fmt, ap);
    va_end(ap);
    return ok;
}

void em_format_fixed(const char *where, char *dst, size_t size, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    bool ok = vformat(dst, size, fmt, ap);
    va_end(ap);
    if (!ok) {
        (void)fprintf(stderr, "emosa: %s: text longer than its buffer (%zu bytes)\n", where, size);
        abort();
    }
}
