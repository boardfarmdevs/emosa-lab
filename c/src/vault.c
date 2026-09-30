/* The secret store (emosa.secrets.SecretStore). */
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

static bool matches(const char *pattern, const char *text)
{
    regex_t re;
    if (regcomp(&re, pattern, REG_EXTENDED | REG_NOSUB))
        return false;
    bool ok = regexec(&re, text, 0, NULL, 0) == 0;
    regfree(&re);
    return ok;
}

/* an owned private regular file, at most 4096 bytes; returns its length or -1 */
static long read_private(const char *path, uint8_t *out, size_t max)
{
    int fd = open(path, O_RDONLY | O_NOFOLLOW);
    if (fd < 0)
        return -1;
    struct stat st;
    long n = -1;
    if (!fstat(fd, &st) && S_ISREG(st.st_mode) && !(st.st_mode & 077) && st.st_uid == getuid() &&
        st.st_size <= 4096 && (size_t)st.st_size <= max) {
        n = read(fd, out, max);
        if (n != st.st_size)
            n = -1;
    }
    close(fd);
    return n;
}

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
    char path[600];
    EM_FORMAT_FIXED(path, sizeof(path), "%s/.fingerprint-key", directory);
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd >= 0) {
        uint8_t key[32];
        RAND_bytes(key, sizeof(key));
        bool ok = write(fd, key, sizeof(key)) == (ssize_t)sizeof(key) && !fsync(fd);
        close(fd);
        if (!ok)
            return EM_INVALID_INPUT;
    }
    return read_private(path, v->key, sizeof(v->key)) == 32 ? EM_OK : EM_INVALID_INPUT;
}

char *em_vault_resolve(const em_vault *v, const char *ref, em_reason *why)
{
    *why = EM_INVALID_INPUT;
    if (!ref || !matches("^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$", ref))
        return NULL;
    char path[700];
    uint8_t data[4097];
    EM_FORMAT_FIXED(path, sizeof(path), "%s/%s", v->directory, ref); /* 511 + 1 + a checked ref (96) */
    long n = read_private(path, data, sizeof(data) - 1);
    if (n < 0) {
        *why = EM_MISSING_PREREQUISITE;
        return NULL;
    }
    if (n < 8 || n > 63)
        return NULL;
    for (long i = 0; i < n; i++)
        if (data[i] < 0x20 || data[i] > 0x7E) /* isascii() and isprintable() */
            return NULL;
    data[n] = 0;
    *why = EM_OK;
    return em_strdup((const char *)data);
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
    if (!ref || !matches("^wsc-[a-f0-9]{32}(-[1-7])?$", ref) || n < 8 || n > 63)
        return EM_INVALID_INPUT;
    for (size_t i = 0; i < n; i++)
        if ((unsigned char)value[i] < 0x20 || (unsigned char)value[i] > 0x7E)
            return EM_INVALID_INPUT;
    char path[700];
    EM_FORMAT_FIXED(path, sizeof(path), "%s/%s", v->directory, ref); /* 511 + 1 + a checked ref (96) */
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    if (fd < 0)
        return EM_INVALID_INPUT;
    bool ok = write(fd, value, n) == (ssize_t)n && !fsync(fd);
    close(fd);
    int dir = open(v->directory, O_RDONLY | O_DIRECTORY);
    if (dir >= 0) {
        ok = ok && !fsync(dir);
        close(dir);
    }
    if (!ok) {
        unlink(path);
        return EM_INVALID_INPUT;
    }
    return EM_OK;
}

void em_vault_forget(const em_vault *v, const char *ref)
{
    /* the same names as the rest of the vault: never a path out of its directory */
    if (!ref || !matches("^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$", ref))
        return;
    char path[700];
    EM_FORMAT_FIXED(path, sizeof(path), "%s/%s", v->directory, ref); /* 511 + 1 + a checked ref (96) */
    unlink(path);
}
