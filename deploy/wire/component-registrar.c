/* Synthetic registrar payload exerciser, not an EasyMesh controller.
 * Reads one bounded raw M1 on stdin, emits one M2 on stdout. Uses pinned,
 * unmodified hostap 2.11 helpers/validators and fresh OpenSSL entropy.
 * The SSID/key below are intentionally PUBLIC simulation inputs.
 */
#include "includes.h"
#include "common.h"
#include "crypto/dh_group5.h"
#include "wps/wps_i.h"
#include "wps/wps_dev_attr.h"
#include <openssl/bn.h>
#include <openssl/rand.h>
#include <assert.h>

int random_get_bytes(void *buf, size_t len)
{
    return len <= 4096 && RAND_bytes(buf, (int) len) == 1 ? 0 : -1;
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
    BN_CTX_free(ctx); BN_free(p); BN_free(g); BN_clear_free(x); BN_free(y);
    return wpabuf_alloc_copy(output, sizeof(output));
}

int main(int argc, char **argv)
{
    u8 input[4097], private[32];
    size_t size = fread(input, 1, sizeof(input), stdin);
    if (size == 0 || size > 4096 || ferror(stdin) || argc != 2 ||
        (strcmp(argv[1], "configure") && strcmp(argv[1], "changed") &&
         strcmp(argv[1], "teardown"))) return 2;
    struct wpabuf *m1 = wpabuf_alloc_copy(input, size);
    struct wps_parse_attr fields;
    if (!m1 || wps_validate_m1(m1) || wps_parse_msg(m1, &fields) ||
        !fields.enrollee_nonce || !fields.mac_addr || !fields.public_key ||
        fields.public_key_len != 192) return 3;
    struct wps_context context = {0};
    struct wps_data registrar = {0};
    context.ap = 1; context.wps_state = WPS_STATE_CONFIGURED;
    context.auth_types = WPS_AUTH_WPA2PSK; context.encr_types = WPS_ENCR_AES;
    context.config_methods = WPS_CONFIG_PUSHBUTTON | WPS_CONFIG_VIRT_PUSHBUTTON;
    context.dev.manufacturer = "EMOSA public component fixture";
    context.dev.model_name = "Hostap payload exerciser";
    context.dev.model_number = "1";
    context.dev.serial_number = "public-component-1";
    context.dev.device_name = "Synthetic registrar";
    const u8 primary[] = {0, 6, 0, 0x50, 0xf2, 4, 0, 1};
    memcpy(context.dev.pri_dev_type, primary, 8);
    context.dev.rf_bands = WPS_RF_24GHZ; context.dev.os_version = 1;
    registrar.wps = &context; registrar.registrar = 1;
    registrar.dev_pw_id = DEV_PW_PUSHBUTTON;
    memcpy(registrar.nonce_e, fields.enrollee_nonce, 16);
    memcpy(registrar.mac_addr_e, fields.mac_addr, 6);
    assert(random_get_bytes(private, sizeof(private)) == 0);
    assert(random_get_bytes(registrar.nonce_r, 16) == 0);
    assert(random_get_bytes(registrar.uuid_r, 16) == 0);
    registrar.dh_privkey = wpabuf_alloc_copy(private, 32);
    registrar.dh_pubkey_e = wpabuf_alloc_copy(fields.public_key, 192);
    registrar.dh_pubkey_r = public_key(private);
    registrar.dh_ctx = dh5_init_fixed(registrar.dh_privkey, registrar.dh_pubkey_r);
    registrar.last_msg = m1;
    assert(registrar.dh_ctx && wps_derive_keys(&registrar) == 0);

    struct wpabuf *m2 = wpabuf_alloc(4096), *plain = wpabuf_alloc(1024);
    assert(m2 && plain);
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
    if (strcmp(argv[1], "teardown")) {
        const char *ssid = !strcmp(argv[1], "changed") ? "Conflicting-WSC-request" : "EMOSA-WSC-component";
        const char key[] = "OnlySimulationWscKey2026!";
        const u8 auth_type[] = {0, WPS_AUTH_WPA2PSK}, encr_type[] = {0, WPS_ENCR_AES};
        const u8 bssid[] = {2, 0, 0, 0, 0x50, 0x11};
        attr(plain, ATTR_SSID, ssid, strlen(ssid));
        attr(plain, ATTR_AUTH_TYPE, auth_type, 2);
        attr(plain, ATTR_ENCR_TYPE, encr_type, 2);
        attr(plain, ATTR_NETWORK_KEY, key, sizeof(key) - 1);
        attr(plain, ATTR_MAC_ADDR, bssid, 6);
    }
    assert(wps_build_wfa_ext(plain, 0, NULL, 0,
        !strcmp(argv[1], "teardown") ? MULTI_AP_TEAR_DOWN : MULTI_AP_FRONTHAUL_BSS) == 0);
    assert(wps_build_key_wrap_auth(&registrar, plain) == 0);
    assert(wps_build_encr_settings(&registrar, m2, plain) == 0);
    const u8 index = 1;
    attr(m2, 0x1bbc, &index, 1);
    assert(wps_build_authenticator(&registrar, m2) == 0);
    assert(wps_validate_m2(m2) == 0);
    assert(wps_process_authenticator(&registrar,
        wpabuf_head_u8(m2) + wpabuf_len(m2) - WPS_AUTHENTICATOR_LEN, m2) == 0);
    assert(wps_parse_msg(m2, &fields) == 0);
    struct wpabuf *decrypted = wps_decrypt_encr_settings(
        &registrar, fields.encr_settings, fields.encr_settings_len);
    assert(decrypted && wps_process_key_wrap_auth(&registrar, decrypted,
        wpabuf_head_u8(decrypted) + wpabuf_len(decrypted) - WPS_KWA_LEN) == 0);
    if (strcmp(argv[1], "teardown")) assert(wps_validate_m8_encr(decrypted, 1, 1) == 0);
    assert(fwrite(wpabuf_head(m2), 1, wpabuf_len(m2), stdout) == wpabuf_len(m2));
    return 0;
}
