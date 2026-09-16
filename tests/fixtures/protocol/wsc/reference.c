/* Synthetic, offline vector harness. Never use these keys or deterministic IVs
 * in an exchange. This calls unmodified hostap 2.11 WPS functions; EMOSA is not
 * imported or linked. See README.md for provenance and reproduction. */
#include "includes.h"
#include "common.h"
#include "crypto/dh_group5.h"
#include "crypto/aes_wrap.h"
#include "wps/wps_i.h"
#include <openssl/bn.h>
#include <assert.h>

/* The only caller in this harness is wps_build_encr_settings. */
int random_get_bytes(void *buf, size_t len)
{
    assert(len == 16);
    for (size_t i = 0; i < len; i++)
        ((u8 *) buf)[i] = 0xa0 + i;
    return 0;
}

static void emit(const char *name, const void *bytes, size_t len)
{
    printf("%s=", name);
    for (size_t i = 0; i < len; i++)
        printf("%02x", ((const u8 *) bytes)[i]);
    puts("");
}

static struct wpabuf *public_key(const u8 *private, size_t len)
{
    BN_CTX *ctx = BN_CTX_new();
    BIGNUM *p = BN_get_rfc3526_prime_1536(NULL), *g = BN_new();
    BIGNUM *x = BN_bin2bn(private, len, NULL), *y = BN_new();
    u8 output[192];
    assert(ctx && p && g && x && y && BN_set_word(g, 2));
    assert(BN_mod_exp(y, g, x, p, ctx));
    assert(BN_bn2binpad(y, output, sizeof(output)) == sizeof(output));
    BN_CTX_free(ctx);
    BN_free(p); BN_free(g); BN_free(x); BN_free(y);
    return wpabuf_alloc_copy(output, sizeof(output));
}

static void vector(int small)
{
    struct wps_data wps = {0};
    u8 x[32], y[32];
    for (size_t i = 0; i < 32; i++) {
        x[i] = small ? 0 : i + 1;
        y[i] = small ? 0 : i + 33;
    }
    if (small) { x[31] = 2; y[31] = 3; }
    for (size_t i = 0; i < WPS_NONCE_LEN; i++) {
        wps.nonce_e[i] = i;
        wps.nonce_r[i] = i + 16;
    }
    wps.mac_addr_e[0] = 2;
    wps.mac_addr_e[5] = 1;
    struct wpabuf *pub = public_key(x, sizeof(x));
    wps.dh_pubkey_r = public_key(y, sizeof(y));
    wps.dh_privkey = wpabuf_alloc_copy(x, sizeof(x));
    wps.dh_ctx = dh5_init_fixed(wps.dh_privkey, pub);
    assert(wps.dh_ctx);
    puts(small ? "case=leading-zero-shared-secret" : "case=synthetic-exchange");
    emit("enrollee_private", x, sizeof(x));
    emit("enrollee_public", wpabuf_head(pub), wpabuf_len(pub));
    emit("registrar_private", y, sizeof(y));
    emit("registrar_public", wpabuf_head(wps.dh_pubkey_r), wpabuf_len(wps.dh_pubkey_r));
    emit("enrollee_nonce", wps.nonce_e, WPS_NONCE_LEN);
    emit("registrar_nonce", wps.nonce_r, WPS_NONCE_LEN);
    emit("enrollee_mac", wps.mac_addr_e, ETH_ALEN);
    assert(wps_derive_keys(&wps) == 0);
    emit("auth_key", wps.authkey, WPS_AUTHKEY_LEN);
    emit("key_wrap_key", wps.keywrapkey, WPS_KEYWRAPKEY_LEN);
    emit("emsk", wps.emsk, WPS_EMSK_LEN);

    /* Attribute fragments, NOT complete M1/M2 messages or qualified settings. */
    const u8 previous[] = {0x10, 0x4a, 0, 1, 0x10, 0x10, 0x22, 0, 1, 4};
    const u8 current[] = {0x10, 0x4a, 0, 1, 0x10, 0x10, 0x22, 0, 1, 5,
                          0xff, 0xfe, 0, 3, 0x61, 0x62, 0x63};
    wps.last_msg = wpabuf_alloc_copy(previous, sizeof(previous));
    struct wpabuf *msg = wpabuf_alloc(1024);
    wpabuf_put_data(msg, current, sizeof(current));
    assert(wps_build_authenticator(&wps, msg) == 0);
    const u8 *auth = wpabuf_head_u8(msg) + wpabuf_len(msg) - WPS_AUTHENTICATOR_LEN;
    assert(wps_process_authenticator(&wps, auth, msg) == 0);
    emit("previous_fragment", previous, sizeof(previous));
    emit("unsigned_fragment", current, sizeof(current));
    emit("authenticated_fragment", wpabuf_head(msg), wpabuf_len(msg));
    ((u8 *) wpabuf_mhead(msg))[wpabuf_len(msg) - 1] ^= 1;
    assert(wps_process_authenticator(&wps, auth, msg) < 0);

    struct wpabuf *plain = wpabuf_alloc(1024), *wrapped = wpabuf_alloc(1024);
    const char ssid[] = "EMOSA-public-vector";
    wpabuf_put_be16(plain, ATTR_SSID);
    wpabuf_put_be16(plain, sizeof(ssid) - 1);
    wpabuf_put_data(plain, ssid, sizeof(ssid) - 1);
    emit("plaintext_fragment", wpabuf_head(plain), wpabuf_len(plain));
    assert(wps_build_key_wrap_auth(&wps, plain) == 0);
    assert(wps_build_encr_settings(&wps, wrapped, plain) == 0);
    const u8 *encrypted = wpabuf_head_u8(wrapped) + 4;
    size_t encrypted_len = wpabuf_len(wrapped) - 4;
    emit("encrypted_settings", encrypted, encrypted_len);
    struct wpabuf *decrypted = wps_decrypt_encr_settings(&wps, encrypted, encrypted_len);
    assert(decrypted);
    const u8 *kwa = wpabuf_head_u8(decrypted) + wpabuf_len(decrypted) - WPS_KWA_LEN;
    assert(wps_process_key_wrap_auth(&wps, decrypted, kwa) == 0);
    ((u8 *) wpabuf_mhead(decrypted))[wpabuf_len(decrypted) - 1] ^= 1;
    assert(wps_process_key_wrap_auth(&wps, decrypted, kwa) < 0);

    /* Produce independently encrypted negative vectors with valid padding so
     * tests reach KWA validation, and invalid padding so tests reach unpadding. */
    struct wpabuf *negative = wpabuf_alloc(1024);
    struct wpabuf *bad_plain = wpabuf_alloc(1024);
    wpabuf_put_buf(bad_plain, decrypted);
    assert(wps_build_encr_settings(&wps, negative, bad_plain) == 0);
    emit("bad_kwa", wpabuf_head_u8(negative) + 4, wpabuf_len(negative) - 4);
    u8 invalid_padding[16] = {0}; /* PKCS padding zero is invalid. */
    u8 iv[16];
    random_get_bytes(iv, sizeof(iv));
    assert(aes_128_cbc_encrypt(wps.keywrapkey, iv, invalid_padding, 16) == 0);
    u8 bad_padding[32];
    memcpy(bad_padding, iv, 16); memcpy(bad_padding + 16, invalid_padding, 16);
    emit("bad_padding", bad_padding, sizeof(bad_padding));

    wpabuf_free(negative); wpabuf_free(bad_plain);
    wpabuf_free(pub); wpabuf_free(wps.dh_pubkey_r); wpabuf_free(wps.last_msg);
    wpabuf_free(msg); wpabuf_free(plain); wpabuf_free(wrapped); wpabuf_free(decrypted);
}

int main(void)
{
    vector(0);
    vector(1);
    return 0;
}
