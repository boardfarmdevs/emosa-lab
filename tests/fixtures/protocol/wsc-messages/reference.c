/* Offline synthetic AP M1/M2 payload vectors. No EMOSA code is linked.
 * The included upstream enrollee implementation is unmodified. Its static M1
 * builder is called directly. M2 assembly uses upstream attribute/crypto helpers;
 * both payloads pass the independent CONFIG_WPS_STRICT validators.
 * Deterministic test keys/nonces/IVs below must NEVER be used in a live exchange. */
#include "includes.h"
#include "common.h"
#include "crypto/dh_group5.h"
#include "wps/wps_i.h"
#include "wps/wps_dev_attr.h"
#include "wps/wps_enrollee.c"
#include <openssl/bn.h>
#include <assert.h>

static unsigned random_base;
int random_get_bytes(void *buf, size_t len)
{
    assert(len == 16);
    for (size_t i = 0; i < len; i++) ((u8 *) buf)[i] = random_base + i;
    random_base += 16;
    return 0;
}

static void emit(const char *name, const void *bytes, size_t len)
{
    printf("%s=", name);
    for (size_t i = 0; i < len; i++) printf("%02x", ((const u8 *) bytes)[i]);
    puts("");
}

static void attr(struct wpabuf *msg, u16 kind, const void *data, size_t size)
{
    wpabuf_put_be16(msg, kind); wpabuf_put_be16(msg, size);
    wpabuf_put_data(msg, data, size);
}

static struct wpabuf *public_key(const u8 *private)
{
    BN_CTX *ctx = BN_CTX_new();
    BIGNUM *p = BN_get_rfc3526_prime_1536(NULL), *g = BN_new();
    BIGNUM *x = BN_bin2bn(private, 32, NULL), *y = BN_new();
    u8 output[192];
    assert(ctx && p && g && x && y && BN_set_word(g, 2));
    assert(BN_mod_exp(y, g, x, p, ctx));
    assert(BN_bn2binpad(y, output, sizeof(output)) == sizeof(output));
    BN_CTX_free(ctx); BN_free(p); BN_free(g); BN_free(x); BN_free(y);
    return wpabuf_alloc_copy(output, sizeof(output));
}

static int rf_band(void *context) { return WPS_RF_24GHZ; }

int main(void)
{
    struct wps_context context = {0};
    struct wps_data enrollee = {0}, registrar = {0};
    u8 x[32], y[32];
    for (size_t i = 0; i < 32; i++) { x[i] = i + 1; y[i] = i + 33; }
    context.ap = 1; context.wps_state = WPS_STATE_CONFIGURED;
    context.auth_types = WPS_AUTH_WPA2PSK; context.encr_types = WPS_ENCR_AES;
    context.config_methods = WPS_CONFIG_PUSHBUTTON | WPS_CONFIG_VIRT_PUSHBUTTON;
    context.rf_band_cb = rf_band;
    context.dev.manufacturer = "EMOSA synthetic laboratory";
    context.dev.model_name = "Payload reference";
    context.dev.model_number = "1";
    context.dev.serial_number = "public-vector-1";
    context.dev.device_name = "Synthetic represented AP";
    const u8 primary[] = {0, 6, 0, 0x50, 0xf2, 4, 0, 1};
    memcpy(context.dev.pri_dev_type, primary, 8);
    context.dev.rf_bands = WPS_RF_24GHZ; context.dev.os_version = 1;
    context.dh_privkey = wpabuf_alloc_copy(x, 32);
    context.dh_pubkey = public_key(x);
    context.dh_ctx = dh5_init_fixed(context.dh_privkey, context.dh_pubkey);
    assert(context.dh_ctx);
    enrollee.wps = &context; enrollee.dev_pw_id = DEV_PW_PUSHBUTTON;
    enrollee.mac_addr_e[0] = 2; enrollee.mac_addr_e[5] = 1;
    for (size_t i = 0; i < 16; i++) enrollee.uuid_e[i] = 0x40 + i;
    struct wpabuf *m1 = wps_build_m1(&enrollee);
    assert(m1 && wps_validate_m1(m1) == 0);
    puts("case=synthetic-ap-payloads");
    emit("enrollee_private", x, 32);
    emit("enrollee_public", wpabuf_head(enrollee.dh_pubkey_e), 192);
    emit("registrar_private", y, 32);
    emit("m1", wpabuf_head(m1), wpabuf_len(m1));

    registrar.wps = &context; registrar.registrar = 1;
    registrar.dev_pw_id = DEV_PW_PUSHBUTTON;
    memcpy(registrar.nonce_e, enrollee.nonce_e, 16);
    memcpy(registrar.mac_addr_e, enrollee.mac_addr_e, 6);
    for (size_t i = 0; i < 16; i++) {
        registrar.nonce_r[i] = 0x10 + i; registrar.uuid_r[i] = 0x60 + i;
    }
    registrar.dh_privkey = wpabuf_alloc_copy(y, 32);
    registrar.dh_pubkey_e = wpabuf_dup(enrollee.dh_pubkey_e);
    registrar.dh_pubkey_r = public_key(y);
    registrar.dh_ctx = dh5_init_fixed(registrar.dh_privkey, registrar.dh_pubkey_r);
    registrar.last_msg = m1;
    assert(wps_derive_keys(&registrar) == 0);
    emit("registrar_public", wpabuf_head(registrar.dh_pubkey_r), 192);
    emit("auth_key", registrar.authkey, WPS_AUTHKEY_LEN);
    emit("key_wrap_key", registrar.keywrapkey, WPS_KEYWRAPKEY_LEN);
    emit("emsk", registrar.emsk, WPS_EMSK_LEN);

    struct wpabuf *m2 = wpabuf_alloc(4096), *plain = wpabuf_alloc(1024);
    assert(wps_build_version(m2) == 0 && wps_build_msg_type(m2, WPS_M2) == 0);
    assert(wps_build_enrollee_nonce(&registrar, m2) == 0);
    assert(wps_build_registrar_nonce(&registrar, m2) == 0);
    attr(m2, ATTR_UUID_R, registrar.uuid_r, 16);
    attr(m2, ATTR_PUBLIC_KEY, wpabuf_head(registrar.dh_pubkey_r), 192);
    assert(wps_build_auth_type_flags(&registrar, m2) == 0);
    assert(wps_build_encr_type_flags(&registrar, m2) == 0);
    assert(wps_build_conn_type_flags(&registrar, m2) == 0);
    assert(wps_build_config_methods(m2, context.config_methods) == 0);
    assert(wps_build_device_attrs(&context.dev, m2) == 0);
    assert(wps_build_rf_bands(&context.dev, m2, WPS_RF_24GHZ) == 0);
    assert(wps_build_assoc_state(&registrar, m2) == 0);
    assert(wps_build_config_error(m2, WPS_CFG_NO_ERROR) == 0);
    assert(wps_build_dev_password_id(m2, DEV_PW_PUSHBUTTON) == 0);
    assert(wps_build_os_version(&context.dev, m2) == 0);
    assert(wps_build_wfa_ext(m2, 0, NULL, 0, 0) == 0);
    const char ssid[] = "EMOSA-payload-vector", key[] = "public-vector-passphrase";
    const u8 auth_type[] = {0, WPS_AUTH_WPA2PSK}, encr_type[] = {0, WPS_ENCR_AES};
    const u8 bssid[] = {2, 0, 0, 0, 0x10, 1};
    attr(plain, ATTR_SSID, ssid, sizeof(ssid) - 1);
    attr(plain, ATTR_AUTH_TYPE, auth_type, 2);
    attr(plain, ATTR_ENCR_TYPE, encr_type, 2);
    attr(plain, ATTR_NETWORK_KEY, key, sizeof(key) - 1);
    attr(plain, ATTR_MAC_ADDR, bssid, 6);
    assert(wps_build_wfa_ext(plain, 0, NULL, 0, MULTI_AP_FRONTHAUL_BSS) == 0);
    emit("ap_settings", wpabuf_head(plain), wpabuf_len(plain));
    assert(wps_build_key_wrap_auth(&registrar, plain) == 0);
    assert(wps_validate_m8_encr(plain, 1, 1) == 0); /* Same AP settings Table 20. */
    random_base = 0xa0;
    const size_t prefix_len = wpabuf_len(m2);
    assert(wps_build_encr_settings(&registrar, m2, plain) == 0);
    /* New EasyMesh BSS_Index is not interpreted by this older hostap version. */
    const u8 index = 1;
    attr(m2, 0x1bbc, &index, 1);
    assert(wps_build_authenticator(&registrar, m2) == 0);
    assert(wps_validate_m2(m2) == 0);
    const u8 *auth = wpabuf_head_u8(m2) + wpabuf_len(m2) - WPS_AUTHENTICATOR_LEN;
    assert(wps_process_authenticator(&registrar, auth, m2) == 0);
    struct wps_parse_attr parsed;
    assert(wps_parse_msg(m2, &parsed) == 0);
    struct wpabuf *decrypted = wps_decrypt_encr_settings(
        &registrar, parsed.encr_settings, parsed.encr_settings_len);
    assert(decrypted && wps_validate_m8_encr(decrypted, 1, 1) == 0);
    assert(wps_process_key_wrap_auth(&registrar, decrypted,
        wpabuf_head_u8(decrypted) + wpabuf_len(decrypted) - WPS_KWA_LEN) == 0);
    struct wps_parse_attr config;
    assert(wps_parse_msg(decrypted, &config) == 0);
    assert(config.multi_ap_ext == MULTI_AP_FRONTHAUL_BSS);
    emit("m2", wpabuf_head(m2), wpabuf_len(m2));
    /* Separate teardown alternative, not a second M2 in the same response.
     * EasyMesh 7.1 ignores other settings for teardown. Validate the envelope,
     * native role parsing and crypto, not hostap's ordinary AP-field validator. */
    struct wpabuf *teardown = wpabuf_alloc(4096), *empty = wpabuf_alloc(128);
    assert(teardown && empty);
    wpabuf_put_data(teardown, wpabuf_head(m2), prefix_len);
    assert(wps_build_wfa_ext(empty, 0, NULL, 0, MULTI_AP_TEAR_DOWN) == 0);
    emit("teardown_settings", wpabuf_head(empty), wpabuf_len(empty));
    assert(wps_build_key_wrap_auth(&registrar, empty) == 0);
    random_base = 0xb0;
    assert(wps_build_encr_settings(&registrar, teardown, empty) == 0);
    assert(wps_build_authenticator(&registrar, teardown) == 0);
    assert(wps_validate_m2(teardown) == 0);
    assert(wps_process_authenticator(&registrar,
        wpabuf_head_u8(teardown) + wpabuf_len(teardown) - WPS_AUTHENTICATOR_LEN, teardown) == 0);
    assert(wps_parse_msg(teardown, &parsed) == 0);
    struct wpabuf *teardown_data = wps_decrypt_encr_settings(
        &registrar, parsed.encr_settings, parsed.encr_settings_len);
    assert(teardown_data && wps_parse_msg(teardown_data, &config) == 0);
    assert(config.multi_ap_ext == MULTI_AP_TEAR_DOWN);
    assert(wps_process_key_wrap_auth(&registrar, teardown_data,
        wpabuf_head_u8(teardown_data) + wpabuf_len(teardown_data) - WPS_KWA_LEN) == 0);
    emit("teardown_m2", wpabuf_head(teardown), wpabuf_len(teardown));
    /* Each validator must actually reject a missing required Version attribute. */
    struct wpabuf *bad = wpabuf_alloc_copy(wpabuf_head_u8(m1) + 5, wpabuf_len(m1) - 5);
    assert(wps_validate_m1(bad) < 0); wpabuf_free(bad);
    bad = wpabuf_alloc_copy(wpabuf_head_u8(m2) + 5, wpabuf_len(m2) - 5);
    assert(wps_validate_m2(bad) < 0); wpabuf_free(bad);
    return 0;
}
