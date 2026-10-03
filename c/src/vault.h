/* The agent's secret store, as emosa.secrets.SecretStore: references to secrets kept by a
 * backend, and a keyed fingerprint of values (HMAC-SHA256 over the compact sorted JSON),
 * so a journal records which credential without it. The policy is the vault's, the same
 * for every backend: which references are valid, what a usable passphrase is, that a
 * received credential is never overwritten. Where the secrets live is the backend's: the
 * files backend (em_vault_open) keeps private files under the state directory's secrets/,
 * the same directory for both implementations; a platform's secure storage implements
 * em_secret_backend instead (spec §6). */
#ifndef EMOSA_VAULT_H
#define EMOSA_VAULT_H

#include <cjson/cJSON.h>

#include "common.h"

#define EM_SECRET_MAX 4096 /* the longest stored value a backend gives back */

typedef struct em_vault em_vault;

/* Where secrets are kept. Each function gets the vault (its ctx is the backend's). */
typedef struct {
    const char *name;
    /* the stored value (at most EM_SECRET_MAX bytes, its length in *len; the caller
     * frees it), or NULL when there is none or it is not kept privately */
    uint8_t *(*read)(const em_vault *v, const char *ref, size_t *len);
    /* a new reference, never replacing one, durable when it returns true */
    bool (*create)(const em_vault *v, const char *ref, const uint8_t *data, size_t len);
    void (*remove)(const em_vault *v, const char *ref);
    /* the 32-byte fingerprint key, created on first use and the same ever after */
    bool (*key)(const em_vault *v, uint8_t out[32]);
} em_secret_backend;

struct em_vault {
    em_secret_backend backend;
    void *ctx;              /* a platform backend's own */
    char directory[512];    /* the files backend's */
    uint8_t key[32];
};

/* The files backend in directory: created (0700) when absent, refused otherwise unless
 * private; the fingerprint key created when absent. */
em_reason em_vault_open(em_vault *v, const char *directory);
/* Another backend (ctx its own). */
em_reason em_vault_open_backend(em_vault *v, em_secret_backend backend, void *ctx);

/* A stored passphrase (8..63 printable ASCII) by reference; NULL with *why set. */
char *em_vault_resolve(const em_vault *v, const char *ref, em_reason *why);

/* HMAC fingerprint of any JSON value (65 bytes out). */
bool em_vault_fingerprint(const em_vault *v, const cJSON *value, char out[65]);
/* ... of one string. */
bool em_vault_fingerprint_text(const em_vault *v, const char *text, char out[65]);

/* A received M2 credential: "wsc-<32 hex>[-1..7]", never overwritten, durable. */
em_reason em_vault_persist_received(const em_vault *v, const char *ref, const char *value);
/* Removes a reference (a credential whose operation was never journaled). */
void em_vault_forget(const em_vault *v, const char *ref);

#endif
