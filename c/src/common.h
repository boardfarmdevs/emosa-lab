/* SPDX-License-Identifier: Apache-2.0 */
/* EMOSA C: shared types and helpers (see c/README.md). */
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
    EM_SCHEMA_MISMATCH,
    EM_APPLY_TIMEOUT,
    EM_MISSING_PREREQUISITE,
    EM_NOT_FOUND,
} em_reason;

/* The reason named `name` (a Reason value), or EM_OK when unknown. */
em_reason em_reason_parse(const char *name);

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

/* Allocation. A failed allocation ends the process (a message, then abort()): the
 * agent is supervised and its journal makes a restart safe (spec/design.md §6), so
 * no caller handles allocation failure itself. em_init() installs the same
 * allocator in cJSON; every program calls it first. */
void em_init(void);
void *em_malloc(size_t size);
void *em_calloc(size_t n, size_t size);
void *em_realloc(void *p, size_t size);
char *em_strdup(const char *s);

/* Files. em_read_file: the whole file, NUL-terminated (its length in *len when len
 * is not NULL), or NULL when it cannot be read completely or is longer than max.
 * em_write_file: text to path through path.tmp and a rename, so a reader finds the
 * old file or the new one; durable also flushes it to the disk first (state that
 * must survive a power loss, not the status file rewritten every second, which
 * would wear a router's flash); false, and the old file kept, on any error. */
char *em_read_file(const char *path, size_t max, size_t *len);
bool em_write_file(const char *path, const char *text, bool durable);

/* Bounded strings (CERT ERR33-C: no truncation goes unnoticed). em_copy copies src
 * into dst of size bytes; em_format is snprintf. Both return false when the result
 * did not fit (dst then holds a truncated, terminated string) or formatting failed,
 * and the caller acts on it. EM_FORMAT_FIXED is for text whose length is bounded by
 * construction (numbers, MACs, the program's own names) in a buffer sized for it: a
 * truncation there is a programming error, and it ends the process like a failed
 * allocation. Never use it for text from the pod, the LAN or a file. */
bool em_copy(char *dst, size_t size, const char *src);
bool em_format(char *dst, size_t size, const char *fmt, ...) __attribute__((format(printf, 3, 4)));
void em_format_fixed(const char *where, char *dst, size_t size, const char *fmt, ...)
    __attribute__((format(printf, 4, 5)));
#define EM_STR_(x) #x
#define EM_STR(x) EM_STR_(x)
#define EM_FORMAT_FIXED(dst, size, ...) em_format_fixed(__FILE__ ":" EM_STR(__LINE__), dst, size, __VA_ARGS__)

/* The longest state directory the agent accepts (its configuration is refused
 * otherwise). Every path the agent and its scopes build under it fits the buffers
 * they use for it (at least 512 bytes), so those paths are bounded by construction. */
#define EM_STATE_DIR_MAX 256

/* qsort for arrays that may be empty (and NULL): qsort's pointer must not be null. */
void em_sort(void *base, size_t n, size_t size, int (*compare)(const void *, const void *));

#endif
