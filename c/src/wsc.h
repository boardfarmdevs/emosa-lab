/* WPS 2.0.10 M1/M2 as EMOSA uses it (spec §2.5), mirroring emosa.wsc,
 * emosa.wsc_messages and emosa.wsc_radio. */
#ifndef EMOSA_WSC_H
#define EMOSA_WSC_H

#include "common.h"

typedef struct {
    uint8_t uuid[16];
    uint8_t al_mac[6];
    uint16_t authentication_types, encryption_types;
    uint8_t connection_types;
    uint16_t configuration_methods;
    uint8_t wps_state;
    em_buf manufacturer, model_name, model_number, serial_number, device_name;
    uint8_t primary_device_type[8];
    uint8_t rf_band;
    uint16_t association_state, device_password_id, configuration_error;
    uint32_t os_version;
} em_m1_device;

/* One M1 and the ephemeral key that authenticates its M2. */
typedef struct {
    em_buf message;
    uint8_t private_key[192]; /* big-endian exponent; never logged or persisted */
    size_t private_len;
    uint8_t public_key[192];
} em_m1;

/* Entropy for M1: NULL for fresh randomness; fixed values only for vectors. */
typedef struct {
    const uint8_t *private_key; /* big-endian, 1 to 192 octets */
    size_t private_len;
    const uint8_t *enrollee_nonce; /* 16 octets */
} em_wsc_entropy;

em_reason em_m1_create(const em_m1_device *device, const em_wsc_entropy *entropy, em_m1 *out);
void em_m1_free(em_m1 *m1);

/* One BSS of an authenticated M2 set that fits EMOSA's WPA2-PSK mapping. */
typedef struct {
    char role[10];     /* "fronthaul" or "backhaul" */
    char ssid[33];     /* UTF-8, no NUL inside */
    char passphrase[64];
    int bss_index;     /* -1 when absent */
} em_bss_candidate;

typedef struct {
    bool teardown;
    size_t count;
    em_bss_candidate bss[16];
} em_m2_result;

/* Authenticate every M2 of one radio's set, then map it (EasyMesh 6.1 §7.1).
 * multi_bss false: exactly one fronthaul BSS. shared_session: one registrar
 * nonce and public key over the set (RDK). */
em_reason em_m2_decode(const em_m1 *m1, const em_buf *messages, size_t count, unsigned max_bss,
                       bool multi_bss, bool shared_session, em_m2_result *out);

#endif
