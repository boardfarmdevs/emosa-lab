/* EMOSA C lab prototype: shared types. Not production code (see c/README.md). */
#ifndef EMOSA_COMMON_H
#define EMOSA_COMMON_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* The reference implementation's reason codes (emosa.errors.Reason), same names. */
typedef enum {
    EM_OK = 0,
    EM_INVALID_INPUT,
    EM_UNSUPPORTED_OPERATION,
    EM_BUSY,
    EM_NOT_READY,
    EM_PRECONDITION_FAILED,
    EM_OUTCOME_UNKNOWN,
    EM_OWNERSHIP_CONFLICT,
    EM_NO_MEMORY,
} em_reason;

const char *em_reason_name(em_reason reason);

/* A growable byte buffer. */
typedef struct {
    uint8_t *data;
    size_t len, cap;
} em_buf;

void em_buf_free(em_buf *b);
bool em_buf_put(em_buf *b, const void *data, size_t len);
bool em_buf_u8(em_buf *b, uint8_t v);
bool em_buf_u16(em_buf *b, uint16_t v); /* network order */
bool em_buf_u32(em_buf *b, uint32_t v); /* network order */

/* Hex helpers: lower-case, no separators ("aabb"), or MAC form ("aa:bb:.."). */
char *em_hex(const uint8_t *data, size_t len);
char *em_mac_str(const uint8_t mac[6]);
bool em_unhex(const char *text, em_buf *out);
bool em_parse_mac(const char *text, uint8_t mac[6]);

#endif
