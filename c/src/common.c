/* EMOSA C lab prototype: shared helpers. */
#include "common.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

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
    }
    return "UNKNOWN";
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
        uint8_t *grown = realloc(b->data, cap);
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
    char *text = malloc(len * 2 + 1);
    if (!text)
        return NULL;
    for (size_t i = 0; i < len; i++)
        sprintf(text + 2 * i, "%02x", data[i]);
    text[len * 2] = 0;
    return text;
}

char *em_mac_str(const uint8_t mac[6])
{
    char *text = malloc(18);
    if (text)
        sprintf(text, "%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2], mac[3], mac[4],
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
