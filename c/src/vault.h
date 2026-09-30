/* The agent's secret store, as emosa.secrets.SecretStore: private files under the state
 * directory's secrets/, one per reference, and a keyed fingerprint of values (HMAC-SHA256
 * over the compact sorted JSON), so a journal records which credential without it. The
 * same directory works for both implementations. */
#ifndef EMOSA_VAULT_H
#define EMOSA_VAULT_H

#include <cjson/cJSON.h>

#include "common.h"

typedef struct {
    char directory[512];
    uint8_t key[32];
} em_vault;

/* Creates the directory (0700) and the fingerprint key when absent. */
em_reason em_vault_open(em_vault *v, const char *directory);

/* A stored passphrase (8..63 printable ASCII) by reference; NULL with *why set. */
char *em_vault_resolve(const em_vault *v, const char *ref, em_reason *why);

/* HMAC fingerprint of any JSON value (65 bytes out). */
bool em_vault_fingerprint(const em_vault *v, const cJSON *value, char out[65]);
/* ... of one string. */
bool em_vault_fingerprint_text(const em_vault *v, const char *text, char out[65]);

/* A received M2 credential: "wsc-<32 hex>[-1..7]", never overwritten, synced. */
em_reason em_vault_persist_received(const em_vault *v, const char *ref, const char *value);
/* Removes a reference's file (a credential whose operation was never journaled). */
void em_vault_forget(const em_vault *v, const char *ref);

#endif
