/* SPDX-License-Identifier: Apache-2.0 */
/* The secret store (emosa.secrets.SecretStore): the policy here, the storage in a backend;
 * the files backend below. */
#include "vault.h"

#include <errno.h>
#include <fcntl.h>
#include <openssl/rand.h>
#include <regex.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "canon.h"

#define REFERENCE "^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$"
#define RECEIVED "^wsc-[a-f0-9]{32}(-[1-7])?$"

static bool matches(const char *pattern, const char *text)
{
    regex_t re;
    if (regcomp(&re, pattern, REG_EXTENDED | REG_NOSUB))
        return false;
    bool ok = regexec(&re, text, 0, NULL, 0) == 0;
    regfree(&re);
    return ok;
}

/* -- the files backend: one private file per reference ------------------------------- */

/* an owned private regular file, at most max bytes; returns its length or -1 */
static long read_private(const char *path, uint8_t *out, size_t max)
{
    int fd = open(path, O_RDONLY | O_NOFOLLOW);
    if (fd < 0)
        return -1;
    struct stat st;
    long n = -1;
    if (!fstat(fd, &st) && S_ISREG(st.st_mode) && !(st.st_mode & 077) && st.st_uid == getuid() &&
        st.st_size <= EM_SECRET_MAX && (size_t)st.st_size <= max) {
        n = read(fd, out, max);
        if (n != st.st_size)
            n = -1;
    }
    close(fd);
    return n;
}

static bool file_path(const em_vault *v, const char *ref, char out[700])
{
    /* 511 + 1 + a reference the vault checked (at most 96) */
    return em_format(out, 700, "%s/%s", v->directory, ref);
}

static uint8_t *files_read(const em_vault *v, const char *ref, size_t *len)
{
    char path[700];
    uint8_t *data = em_malloc(EM_SECRET_MAX + 1);
    long n = file_path(v, ref, path) ? read_private(path, data, EM_SECRET_MAX) : -1;
    if (n < 0) {
        free(data);
        return NULL;
    }
    *len = (size_t)n;
    return data;
}

static bool files_create(const em_vault *v, const char *ref, const uint8_t *data, size_t len)
{
    char path[700];
    if (!file_path(v, ref, path))
        return false;
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    if (fd < 0)
        return false;
    bool ok = write(fd, data, len) == (ssize_t)len && !fsync(fd);
    close(fd);
    int dir = open(v->directory, O_RDONLY | O_DIRECTORY);
    if (dir >= 0) {
        ok = ok && !fsync(dir);
        close(dir);
    }
    if (!ok)
        unlink(path);
    return ok;
}

static void files_remove(const em_vault *v, const char *ref)
{
    char path[700];
    if (file_path(v, ref, path))
        unlink(path);
}

static bool files_key(const em_vault *v, uint8_t out[32])
{
    char path[600];
    EM_FORMAT_FIXED(path, sizeof(path), "%s/.fingerprint-key", v->directory); /* 511 + 17 */
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd >= 0) {
        uint8_t key[32];
        bool ok = RAND_bytes(key, sizeof(key)) == 1 && write(fd, key, sizeof(key)) == (ssize_t)sizeof(key) &&
                  !fsync(fd);
        close(fd);
        if (!ok)
            return false;
    }
    return read_private(path, out, 32) == 32;
}

static const em_secret_backend FILES = {"files", files_read, files_create, files_remove, files_key};

em_reason em_vault_open(em_vault *v, const char *directory)
{
    memset(v, 0, sizeof(*v));
    if (!em_copy(v->directory, sizeof(v->directory), directory)) /* 511 bytes at most */
        return EM_INVALID_INPUT;
    if (mkdir(directory, 0700) && errno != EEXIST)
        return EM_INVALID_INPUT;
    struct stat st;
    if (stat(directory, &st) || !S_ISDIR(st.st_mode) || (st.st_mode & 077))
        return EM_INVALID_INPUT; /* "secret directory must have mode 0700" */
    return em_vault_open_backend(v, FILES, NULL);
}

em_reason em_vault_open_backend(em_vault *v, em_secret_backend backend, void *ctx)
{
    v->backend = backend;
    v->ctx = ctx;
    return backend.key(v, v->key) ? EM_OK : EM_INVALID_INPUT;
}

/* -- the policy -------------------------------------------------------------------- */

static bool printable_ascii(const uint8_t *data, size_t n)
{
    for (size_t i = 0; i < n; i++)
        if (data[i] < 0x20 || data[i] > 0x7E) /* isascii() and isprintable() */
            return false;
    return true;
}

char *em_vault_resolve(const em_vault *v, const char *ref, em_reason *why)
{
    *why = EM_INVALID_INPUT;
    if (!ref || !matches(REFERENCE, ref))
        return NULL;
    size_t n = 0;
    uint8_t *data = v->backend.read(v, ref, &n);
    if (!data) {
        *why = EM_MISSING_PREREQUISITE;
        return NULL;
    }
    char *value = NULL;
    if (n >= 8 && n <= 63 && printable_ascii(data, n)) {
        value = em_malloc(n + 1);
        memcpy(value, data, n);
        value[n] = 0;
        *why = EM_OK;
    }
    free(data);
    return value;
}

bool em_vault_fingerprint(const em_vault *v, const cJSON *value, char out[65])
{
    char *text = em_json_dumps(value, EM_JSON_COMPACT, true);
    if (!text)
        return false;
    em_hmac_hex(v->key, sizeof(v->key), text, strlen(text), out);
    free(text);
    return true;
}

bool em_vault_fingerprint_text(const em_vault *v, const char *text, char out[65])
{
    cJSON *s = cJSON_CreateString(text);
    bool ok = em_vault_fingerprint(v, s, out);
    cJSON_Delete(s);
    return ok;
}

em_reason em_vault_persist_received(const em_vault *v, const char *ref, const char *value)
{
    size_t n = value ? strlen(value) : 0;
    if (!ref || !matches(RECEIVED, ref) || n < 8 || n > 63 || !printable_ascii((const uint8_t *)value, n))
        return EM_INVALID_INPUT;
    return v->backend.create(v, ref, (const uint8_t *)value, n) ? EM_OK : EM_INVALID_INPUT;
}

void em_vault_forget(const em_vault *v, const char *ref)
{
    /* the same names as the rest of the vault: never a path out of its directory */
    if (ref && matches(REFERENCE, ref))
        v->backend.remove(v, ref);
}
