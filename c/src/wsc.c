/* WPS 2.0.10 M1/M2 (spec §2.5). Mirrors emosa.wsc, emosa.wsc_messages, emosa.wsc_radio. */
#include "wsc.h"

#include <openssl/bn.h>
#include <openssl/crypto.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/rand.h>
#include <openssl/sha.h>
#include <stdlib.h>
#include <string.h>

#define VERSION 0x104A
#define MESSAGE_TYPE 0x1022
#define ENROLLEE_NONCE 0x101A
#define REGISTRAR_NONCE 0x1039
#define PUBLIC_KEY 0x1032
#define VENDOR_EXTENSION 0x1049
#define ENCRYPTED_SETTINGS 0x1018
#define AUTHENTICATOR 0x1005
#define KEY_WRAP_AUTHENTICATOR 0x101E
#define BSS_INDEX 0x1BBC
#define MAX_ATTRIBUTES 4096
#define MAX_M2 16
static const uint8_t WFA_ID[3] = {0x00, 0x37, 0x2a};

static const char *MODP_1536 =
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA237327FFFFFFFFFFFFFFFF";

/* -- attributes ------------------------------------------------------------------ */

typedef struct {
    uint16_t kind, len;
    const uint8_t *value; /* borrowed */
} attr;

typedef struct {
    size_t count;
    attr items[MAX_ATTRIBUTES];
} attrs;

static bool put_attr(em_buf *b, uint16_t kind, const void *value, size_t len)
{
    return len <= 0xFFFF && em_buf_u16(b, kind) && em_buf_u16(b, (uint16_t)len) &&
           em_buf_put(b, value, len);
}

static bool decode_attrs(const uint8_t *data, size_t len, attrs *out)
{
    size_t offset = 0;
    out->count = 0;
    if (len > 1024 * 1024)
        return false;
    while (offset < len) {
        if (len - offset < 4 || out->count == MAX_ATTRIBUTES)
            return false;
        uint16_t kind = (uint16_t)(data[offset] << 8 | data[offset + 1]);
        uint16_t size = (uint16_t)(data[offset + 2] << 8 | data[offset + 3]);
        offset += 4;
        if (size > len - offset)
            return false;
        out->items[out->count++] = (attr){kind, size, data + offset};
        offset += size;
    }
    return true;
}

/* Singleton fields with exact (min == max) or ranged lengths; others retained. */
typedef struct {
    uint16_t kind;
    uint16_t min, max;
    bool required;
} field_rule;

static bool singletons(const attrs *a, const field_rule *rules, size_t nrules,
                       const attr **found)
{
    for (size_t r = 0; r < nrules; r++)
        found[r] = NULL;
    for (size_t i = 0; i < a->count; i++) {
        for (size_t r = 0; r < nrules; r++) {
            if (a->items[i].kind != rules[r].kind)
                continue;
            if (found[r] || a->items[i].len < rules[r].min || a->items[i].len > rules[r].max)
                return false;
            found[r] = &a->items[i];
        }
    }
    for (size_t r = 0; r < nrules; r++)
        if (rules[r].required && !found[r])
            return false;
    return true;
}

/* WFA vendor subelements (kind, value) of the Vendor Extension attributes. */
typedef struct {
    size_t count;
    struct {
        uint8_t kind, len;
        const uint8_t *value;
    } items[MAX_ATTRIBUTES];
} subelements;

static bool vendor_subelements(const attrs *a, subelements *out)
{
    out->count = 0;
    for (size_t i = 0; i < a->count; i++) {
        const attr *x = &a->items[i];
        if (x->kind != VENDOR_EXTENSION)
            continue;
        if (x->len < 4 || x->len > 1024)
            return false;
        if (memcmp(x->value, WFA_ID, 3))
            continue;
        size_t offset = 3;
        while (offset < x->len) {
            if (x->len - offset < 2 || out->count == MAX_ATTRIBUTES)
                return false;
            uint8_t kind = x->value[offset], len = x->value[offset + 1];
            offset += 2;
            if (len > x->len - offset)
                return false;
            out->items[out->count].kind = kind;
            out->items[out->count].len = len;
            out->items[out->count].value = x->value + offset;
            out->count++;
            offset += len;
        }
    }
    return true;
}

/* The common M1/M2 rules (emosa.wsc_messages._message); found[] per rule. */
enum { F_VERSION, F_TYPE, F_ENONCE, F_PUBLIC, F_AUTH, F_ENCR, F_CONN, F_METHODS, F_PDT, F_BAND,
       F_ASSOC, F_PWID, F_ERROR, F_OS, F_MANUF, F_MODEL, F_NUMBER, F_SERIAL, F_NAME,
       F_UUID_E, F_MAC, F_STATE, F_RNONCE, F_UUID_R, F_AUTHENTICATOR, F_BSS_INDEX, F_ENCRYPTED,
       F_COUNT };

static bool message(const uint8_t *data, size_t len, int type, attrs *a, const attr **found)
{
    field_rule rules[F_COUNT] = {
        [F_VERSION] = {VERSION, 1, 1, true},       [F_TYPE] = {MESSAGE_TYPE, 1, 1, true},
        [F_ENONCE] = {ENROLLEE_NONCE, 16, 16, true}, [F_PUBLIC] = {PUBLIC_KEY, 192, 192, true},
        [F_AUTH] = {0x1004, 2, 2, true},           [F_ENCR] = {0x1010, 2, 2, true},
        [F_CONN] = {0x100D, 1, 1, true},           [F_METHODS] = {0x1008, 2, 2, true},
        [F_PDT] = {0x1054, 8, 8, true},            [F_BAND] = {0x103C, 1, 1, true},
        [F_ASSOC] = {0x1002, 2, 2, true},          [F_PWID] = {0x1012, 2, 2, true},
        [F_ERROR] = {0x1009, 2, 2, true},          [F_OS] = {0x102D, 4, 4, true},
        [F_MANUF] = {0x1021, 0, 64, true},         [F_MODEL] = {0x1023, 0, 32, true},
        [F_NUMBER] = {0x1024, 0, 32, true},        [F_SERIAL] = {0x1042, 0, 32, true},
        [F_NAME] = {0x1011, 0, 32, true},
        [F_UUID_E] = {0x1047, 16, 16, type == 4},  [F_MAC] = {0x1020, 6, 6, type == 4},
        [F_STATE] = {0x1044, 1, 1, type == 4},
        [F_RNONCE] = {REGISTRAR_NONCE, 16, 16, type == 5},
        [F_UUID_R] = {0x1048, 16, 16, type == 5},
        [F_AUTHENTICATOR] = {AUTHENTICATOR, 8, 8, type == 5},
        [F_BSS_INDEX] = {BSS_INDEX, 1, 1, false},
        [F_ENCRYPTED] = {ENCRYPTED_SETTINGS, 32, 65535, false},
    };
    /* M1 does not consume the M2-only fields and vice versa */
    if (type == 4) {
        rules[F_RNONCE].kind = rules[F_UUID_R].kind = rules[F_AUTHENTICATOR].kind = 0xFFFF;
    } else {
        rules[F_UUID_E].kind = rules[F_MAC].kind = rules[F_STATE].kind = 0xFFFF;
    }
    if (!decode_attrs(data, len, a) || !singletons(a, rules, F_COUNT, found))
        return false;
    if (found[F_TYPE]->value[0] != type)
        return false;
    subelements *v = malloc(sizeof(*v));
    if (!v || !vendor_subelements(a, v)) {
        free(v);
        return false;
    }
    int versions = 0;
    bool ok = true;
    for (size_t i = 0; i < v->count; i++)
        if (v->items[i].kind == 0) {
            versions++;
            ok = ok && v->items[i].len == 1;
        }
    free(v);
    if (versions > 1 || !ok)
        return false;
    if (found[F_BSS_INDEX] && (type != 5 || a->count < 2 || a->items[a->count - 2].kind != BSS_INDEX))
        return false;
    return true;
}

/* -- crypto ------------------------------------------------------------------------ */

static bool hmac256(const uint8_t *key, size_t klen, const uint8_t *data, size_t len,
                    uint8_t out[32])
{
    unsigned int n = 32;
    return HMAC(EVP_sha256(), key, (int)klen, data, len, out, &n) != NULL && n == 32;
}

typedef struct {
    uint8_t auth_key[32], key_wrap_key[16], emsk[32];
} session_keys;

/* g^x mod p, or peer^x mod p, as 192 big-endian octets. */
static bool modexp(const uint8_t *base_bytes, const uint8_t *exponent, size_t exponent_len,
                   uint8_t out[192])
{
    BN_CTX *ctx = BN_CTX_new();
    BIGNUM *p = NULL, *base = BN_new(), *x = BN_secure_new(), *r = BN_new();
    bool ok = ctx && base && x && r && BN_hex2bn(&p, MODP_1536);
    if (ok) {
        ok = (base_bytes ? BN_bin2bn(base_bytes, 192, base) != NULL : BN_set_word(base, 2)) &&
             BN_bin2bn(exponent, (int)exponent_len, x) != NULL;
    }
    if (ok) {
        BN_set_flags(x, BN_FLG_CONSTTIME);
        ok = BN_mod_exp(r, base, x, p, ctx) && BN_bn2binpad(r, out, 192) == 192;
    }
    BN_free(p);
    BN_free(base);
    BN_clear_free(x);
    BN_clear_free(r);
    BN_CTX_free(ctx);
    return ok;
}

/* RFC 2785 3.1: 2 <= y <= p-2 and y^q = 1 (q = (p-1)/2). */
static bool admissible(const uint8_t peer[192])
{
    BN_CTX *ctx = BN_CTX_new();
    BIGNUM *p = NULL, *y = BN_bin2bn(peer, 192, NULL), *q = BN_new(), *r = BN_new(),
           *two = BN_new(), *limit = BN_new();
    bool ok = ctx && y && q && r && two && limit && BN_hex2bn(&p, MODP_1536) &&
              BN_set_word(two, 2) && BN_sub(limit, p, two) && BN_cmp(y, two) >= 0 &&
              BN_cmp(y, limit) <= 0 && BN_rshift1(q, p) && BN_mod_exp(r, y, q, p, ctx) &&
              BN_is_one(r);
    BN_free(p);
    BN_free(y);
    BN_free(q);
    BN_free(r);
    BN_free(two);
    BN_free(limit);
    BN_CTX_free(ctx);
    return ok;
}

static bool derive(const em_m1 *m1, const uint8_t peer[192], const uint8_t enonce[16],
                   const uint8_t emac[6], const uint8_t rnonce[16], session_keys *k)
{
    uint8_t shared[192], dh_key[32], kdk[32], material[96], seed[38];
    if (!admissible(peer) || !modexp(peer, m1->private_key, m1->private_len, shared))
        return false;
    SHA256(shared, sizeof(shared), dh_key);
    memcpy(seed, enonce, 16);
    memcpy(seed + 16, emac, 6);
    memcpy(seed + 22, rnonce, 16);
    bool ok = hmac256(dh_key, 32, seed, sizeof(seed), kdk);
    static const char label[] = "Wi-Fi Easy and Secure Key Derivation";
    for (uint32_t i = 1; ok && i <= 3; i++) {
        em_buf in = {0};
        ok = em_buf_u32(&in, i) && em_buf_put(&in, label, sizeof(label) - 1) &&
             em_buf_u32(&in, 640) && hmac256(kdk, 32, in.data, in.len, material + 32 * (i - 1));
        em_buf_free(&in);
    }
    if (ok) {
        memcpy(k->auth_key, material, 32);
        memcpy(k->key_wrap_key, material + 32, 16);
        memcpy(k->emsk, material + 48, 32);
    }
    OPENSSL_cleanse(shared, sizeof(shared));
    OPENSSL_cleanse(dh_key, sizeof(dh_key));
    OPENSSL_cleanse(kdk, sizeof(kdk));
    OPENSSL_cleanse(material, sizeof(material));
    return ok;
}

/* The authenticator of M2: HMAC(auth_key, M1 || M2 without its trailer)[:8]. */
static bool verify_message(const session_keys *k, const em_buf *previous, const uint8_t *current,
                           size_t len)
{
    attrs *a = malloc(sizeof(*a));
    bool ok = a && decode_attrs(current, len, a) && a->count &&
              a->items[a->count - 1].kind == AUTHENTICATOR && a->items[a->count - 1].len == 8;
    for (size_t i = 0; ok && i + 1 < a->count; i++)
        ok = a->items[i].kind != AUTHENTICATOR;
    if (ok) {
        uint8_t tag[32];
        em_buf data = {0};
        ok = em_buf_put(&data, previous->data, previous->len) &&
             em_buf_put(&data, current, len - 12) &&
             hmac256(k->auth_key, 32, data.data, data.len, tag) &&
             CRYPTO_memcmp(tag, a->items[a->count - 1].value, 8) == 0;
        em_buf_free(&data);
    }
    free(a);
    return ok;
}

/* Encrypted Settings: IV || AES-128-CBC(settings || KWA), PKCS#7; KWA checked. */
static bool decrypt_settings(const session_keys *k, const uint8_t *enc, size_t len, em_buf *out)
{
    if (len < 32 || len > 0xFFFF || len % 16)
        return false;
    EVP_CIPHER_CTX *ctx = EVP_CIPHER_CTX_new();
    uint8_t *plain = malloc(len);
    int n1 = 0, n2 = 0;
    bool ok = ctx && plain &&
              EVP_DecryptInit_ex(ctx, EVP_aes_128_cbc(), NULL, k->key_wrap_key, enc) &&
              EVP_DecryptUpdate(ctx, plain, &n1, enc + 16, (int)len - 16) &&
              EVP_DecryptFinal_ex(ctx, plain + n1, &n2);
    EVP_CIPHER_CTX_free(ctx);
    if (ok) {
        size_t n = (size_t)(n1 + n2);
        attrs *a = malloc(sizeof(*a));
        ok = a && decode_attrs(plain, n, a) && a->count &&
             a->items[a->count - 1].kind == KEY_WRAP_AUTHENTICATOR &&
             a->items[a->count - 1].len == 8;
        for (size_t i = 0; ok && i + 1 < a->count; i++)
            ok = a->items[i].kind != KEY_WRAP_AUTHENTICATOR;
        if (ok) {
            uint8_t tag[32];
            ok = hmac256(k->auth_key, 32, plain, n - 12, tag) &&
                 CRYPTO_memcmp(tag, a->items[a->count - 1].value, 8) == 0 &&
                 em_buf_put(out, plain, n - 12);
        }
        free(a);
    }
    if (plain)
        OPENSSL_cleanse(plain, len);
    free(plain);
    return ok;
}

/* -- M1 ---------------------------------------------------------------------------- */

void em_m1_free(em_m1 *m1)
{
    em_buf_free(&m1->message);
    OPENSSL_cleanse(m1->private_key, sizeof(m1->private_key));
}

static bool no_nul(const em_buf *b) { return !b->len || !memchr(b->data, 0, b->len); }

em_reason em_m1_create(const em_m1_device *d, const em_wsc_entropy *entropy, em_m1 *out)
{
    memset(out, 0, sizeof(*out));
    if ((d->rf_band != 1 && d->rf_band != 2 && d->rf_band != 4 && d->rf_band != 8) ||
        (d->wps_state != 1 && d->wps_state != 2) || !no_nul(&d->manufacturer) ||
        !no_nul(&d->model_name) || !no_nul(&d->model_number) || !no_nul(&d->serial_number) ||
        !no_nul(&d->device_name))
        return EM_INVALID_INPUT;
    uint8_t nonce[16];
    if (entropy && entropy->private_key) {
        if (entropy->private_len < 1 || entropy->private_len > 192)
            return EM_INVALID_INPUT;
        memcpy(out->private_key, entropy->private_key, entropy->private_len);
        out->private_len = entropy->private_len;
    } else {
        out->private_len = 192;
        /* x uniform in [1, q-1]: 1535 random bits, never zero. */
        do {
            if (RAND_priv_bytes(out->private_key, 192) != 1)
                return EM_NOT_READY;
            out->private_key[0] &= 0x3F;
        } while (!out->private_key[0]);
    }
    if (entropy && entropy->enrollee_nonce)
        memcpy(nonce, entropy->enrollee_nonce, 16);
    else if (RAND_bytes(nonce, 16) != 1)
        return EM_NOT_READY;
    if (!modexp(NULL, out->private_key, out->private_len, out->public_key))
        return EM_NOT_READY;
    uint8_t os[4] = {(uint8_t)(d->os_version >> 24 | 0x80), (uint8_t)(d->os_version >> 16),
                     (uint8_t)(d->os_version >> 8), (uint8_t)d->os_version};
    uint8_t u16[2];
#define U16(v) (u16[0] = (uint8_t)((v) >> 8), u16[1] = (uint8_t)(v), u16)
    uint8_t version = 0x10, type = 4, vendor[6] = {0x00, 0x37, 0x2a, 0x00, 0x01, 0x20};
    em_buf *m = &out->message;
    bool ok = put_attr(m, VERSION, &version, 1) && put_attr(m, MESSAGE_TYPE, &type, 1) &&
              put_attr(m, 0x1047, d->uuid, 16) && put_attr(m, 0x1020, d->al_mac, 6) &&
              put_attr(m, ENROLLEE_NONCE, nonce, 16) &&
              put_attr(m, PUBLIC_KEY, out->public_key, 192) &&
              put_attr(m, 0x1004, U16(d->authentication_types), 2) &&
              put_attr(m, 0x1010, U16(d->encryption_types), 2) &&
              put_attr(m, 0x100D, &d->connection_types, 1) &&
              put_attr(m, 0x1008, U16(d->configuration_methods), 2) &&
              put_attr(m, 0x1044, &d->wps_state, 1) &&
              put_attr(m, 0x1021, d->manufacturer.data, d->manufacturer.len) &&
              put_attr(m, 0x1023, d->model_name.data, d->model_name.len) &&
              put_attr(m, 0x1024, d->model_number.data, d->model_number.len) &&
              put_attr(m, 0x1042, d->serial_number.data, d->serial_number.len) &&
              put_attr(m, 0x1054, d->primary_device_type, 8) &&
              put_attr(m, 0x1011, d->device_name.data, d->device_name.len) &&
              put_attr(m, 0x103C, &d->rf_band, 1) &&
              put_attr(m, 0x1002, U16(d->association_state), 2) &&
              put_attr(m, 0x1012, U16(d->device_password_id), 2) &&
              put_attr(m, 0x1009, U16(d->configuration_error), 2) &&
              put_attr(m, 0x102D, os, 4) && put_attr(m, VENDOR_EXTENSION, vendor, 6);
#undef U16
    attrs *a = malloc(sizeof(*a));
    const attr *found[F_COUNT];
    ok = ok && a && message(m->data, m->len, 4, a, found);
    free(a);
    if (!ok) {
        em_m1_free(out);
        return EM_INVALID_INPUT;
    }
    return EM_OK;
}

/* -- M2 ---------------------------------------------------------------------------- */

typedef struct {
    em_buf config;            /* decrypted ConfigData */
    uint8_t registrar_nonce[16], public_key[192];
    int bss_index;
    uint8_t role;             /* Multi-AP flags, reserved bits cleared */
} envelope;

static em_reason authenticate(const em_m1 *m1, const em_buf *m2, envelope *e)
{
    attrs *a1 = malloc(sizeof(*a1)), *a2 = malloc(sizeof(*a2));
    const attr *f1[F_COUNT], *f2[F_COUNT];
    em_reason r = EM_INVALID_INPUT;
    session_keys k;
    if (!a1 || !a2 || !message(m1->message.data, m1->message.len, 4, a1, f1) ||
        !message(m2->data, m2->len, 5, a2, f2))
        goto done;
    if (memcmp(m1->public_key, f1[F_PUBLIC]->value, 192) ||
        CRYPTO_memcmp(f1[F_ENONCE]->value, f2[F_ENONCE]->value, 16))
        goto done;
    if (!derive(m1, f2[F_PUBLIC]->value, f1[F_ENONCE]->value, f1[F_MAC]->value,
                f2[F_RNONCE]->value, &k) ||
        !verify_message(&k, &m1->message, m2->data, m2->len))
        goto done;
    if (!f2[F_ENCRYPTED]) {
        r = EM_UNSUPPORTED_OPERATION; /* M2 without AP settings */
        goto done;
    }
    memset(&e->config, 0, sizeof(e->config));
    if (!decrypt_settings(&k, f2[F_ENCRYPTED]->value, f2[F_ENCRYPTED]->len, &e->config))
        goto done;
    attrs *c = malloc(sizeof(*c));
    subelements *v = malloc(sizeof(*v));
    bool ok = c && v && decode_attrs(e->config.data, e->config.len, c) && vendor_subelements(c, v);
    /* The Multi-AP role must be inside the encrypted ConfigData, exactly once. */
    int roles = 0;
    for (size_t i = 0; ok && i < v->count; i++)
        if (v->items[i].kind == 0x06) {
            roles++;
            ok = v->items[i].len == 1;
            e->role = v->items[i].value[0] & 0xFC;
        }
    subelements *outer = malloc(sizeof(*outer));
    ok = ok && outer && vendor_subelements(a2, outer);
    for (size_t i = 0; ok && i < outer->count; i++)
        ok = outer->items[i].kind != 0x06;
    free(outer);
    free(c);
    free(v);
    if (!ok || roles != 1) {
        em_buf_free(&e->config);
        goto done;
    }
    memcpy(e->registrar_nonce, f2[F_RNONCE]->value, 16);
    memcpy(e->public_key, f2[F_PUBLIC]->value, 192);
    e->bss_index = f2[F_BSS_INDEX] ? f2[F_BSS_INDEX]->value[0] : -1;
    r = EM_OK;
done:
    OPENSSL_cleanse(&k, sizeof(k));
    free(a1);
    free(a2);
    return r;
}

/* WPA2-PSK/AES settings to a candidate (emosa.wsc_radio._psk_candidate). */
static em_reason psk_candidate(const envelope *e, uint16_t m1_auth, uint16_t m1_encr,
                               em_bss_candidate *out)
{
    attrs *a = malloc(sizeof(*a));
    const attr *f[8];
    field_rule rules[8] = {
        {0x1045, 0, 32, true}, {0x1003, 2, 2, true}, {0x100F, 2, 2, true},
        {0x1027, 0, 64, true}, {0x1020, 6, 6, true}, {0x1028, 1, 1, false},
        {0x102A, 0, 64, false}, {0x1012, 2, 2, false},
    };
    em_reason r = EM_INVALID_INPUT;
    if (!a || !decode_attrs(e->config.data, e->config.len, a) || !singletons(a, rules, 8, f) ||
        (f[6] && !f[7]))
        goto done;
    r = EM_UNSUPPORTED_OPERATION;
    uint16_t auth = (uint16_t)(f[1]->value[0] << 8 | f[1]->value[1]);
    uint16_t encr = (uint16_t)(f[2]->value[0] << 8 | f[2]->value[1]);
    if (auth != 0x20 || encr != 0x08 || !(m1_auth & 0x20) || !(m1_encr & 0x08) || f[6] || f[7])
        goto done;
    size_t klen = f[3]->len, slen = f[0]->len;
    if (klen && !f[3]->value[klen - 1])
        klen--; /* one trailing NUL on a legacy passphrase */
    if (slen && !f[0]->value[slen - 1])
        slen--; /* one trailing NUL on the SSID (RDK) */
    if (slen < 1 || slen > 32 || memchr(f[0]->value, 0, slen) || klen < 8 || klen > 63)
        goto done;
    for (size_t i = 0; i < klen; i++)
        if (f[3]->value[i] < 0x20 || f[3]->value[i] > 0x7E)
            goto done;
    /* SSID must be UTF-8 (validated loosely: no C0 NUL, well-formed sequences) */
    for (size_t i = 0; i < slen;) {
        uint8_t c = f[0]->value[i];
        size_t n = c < 0x80 ? 1 : (c >> 5) == 6 ? 2 : (c >> 4) == 14 ? 3 : (c >> 3) == 30 ? 4 : 0;
        if (!n || i + n > slen)
            goto done;
        for (size_t j = 1; j < n; j++)
            if ((f[0]->value[i + j] & 0xC0) != 0x80)
                goto done;
        i += n;
    }
    memcpy(out->ssid, f[0]->value, slen);
    out->ssid[slen] = 0;
    memcpy(out->passphrase, f[3]->value, klen);
    out->passphrase[klen] = 0;
    out->bss_index = e->bss_index;
    r = EM_OK;
done:
    free(a);
    return r;
}

em_reason em_m2_decode(const em_m1 *m1, const em_buf *messages, size_t count, unsigned max_bss,
                       bool multi_bss, bool shared_session, em_m2_result *out)
{
    memset(out, 0, sizeof(*out));
    if (max_bss < 1 || max_bss > MAX_M2 || count < 1 || count > max_bss)
        return EM_INVALID_INPUT;
    envelope *e = calloc(count, sizeof(envelope));
    if (!e)
        return EM_NO_MEMORY;
    em_reason r = EM_OK;
    size_t done = 0;
    for (; done < count && r == EM_OK; done++)
        r = authenticate(m1, &messages[done], &e[done]);
    if (r != EM_OK) {
        done--;
        goto out;
    }
    for (size_t i = 0; i < count && r == EM_OK; i++)
        for (size_t j = i + 1; j < count && r == EM_OK; j++) {
            bool same_nonce = !memcmp(e[i].registrar_nonce, e[j].registrar_nonce, 16);
            if (shared_session) {
                if (!same_nonce || memcmp(e[i].public_key, e[j].public_key, 192) ||
                    (messages[i].len == messages[j].len &&
                     !memcmp(messages[i].data, messages[j].data, messages[i].len)))
                    r = EM_INVALID_INPUT;
            } else if (same_nonce) {
                r = EM_INVALID_INPUT;
            }
            if (e[i].bss_index >= 0 && e[i].bss_index == e[j].bss_index)
                r = EM_INVALID_INPUT;
        }
    if (r != EM_OK)
        goto out;
    for (size_t i = 0; i < count; i++)
        if (e[i].role & 0x10) {
            if (count != 1)
                r = EM_INVALID_INPUT; /* a teardown is one M2 */
            else
                out->teardown = true;
            goto out;
        }
    uint16_t auth = 0, encr = 0;
    {
        attrs *a = malloc(sizeof(*a));
        const attr *f[F_COUNT];
        if (!a || !message(m1->message.data, m1->message.len, 4, a, f)) {
            free(a);
            r = EM_INVALID_INPUT;
            goto out;
        }
        auth = (uint16_t)(f[F_AUTH]->value[0] << 8 | f[F_AUTH]->value[1]);
        encr = (uint16_t)(f[F_ENCR]->value[0] << 8 | f[F_ENCR]->value[1]);
        free(a);
    }
    if (!multi_bss && (count != 1 || e[0].role != 0x20)) {
        r = EM_UNSUPPORTED_OPERATION;
        goto out;
    }
    bool fronthaul = false;
    for (size_t i = 0; i < count && r == EM_OK; i++) {
        uint8_t flags = e[i].role;
        const char *role = flags == 0x20 ? "fronthaul"
                           : (flags & ~(0x08 | 0x04 | 0x80)) == 0x40 ? "backhaul" : NULL;
        if (!role) {
            r = EM_UNSUPPORTED_OPERATION;
            break;
        }
        fronthaul = fronthaul || flags == 0x20;
        strcpy(out->bss[i].role, role);
        r = psk_candidate(&e[i], auth, encr, &out->bss[i]);
    }
    if (r == EM_OK && !fronthaul)
        r = EM_UNSUPPORTED_OPERATION;
    if (r == EM_OK)
        out->count = count;
out:
    for (size_t i = 0; i <= done && i < count; i++)
        em_buf_free(&e[i].config);
    free(e);
    if (r != EM_OK)
        memset(out, 0, sizeof(*out));
    return r;
}
