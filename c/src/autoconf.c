/* AP-Autoconfiguration (spec §2.5). Mirrors emosa.wire.autoconfiguration. */
#include "autoconf.h"

#include <stdlib.h>
#include <string.h>

#define SEARCH 0x0007
#define RESPONSE 0x0008
#define WSC 0x0009

static em_tlv tlv(uint8_t kind, const void *value, size_t len)
{
    em_tlv t = {kind, (uint16_t)len, malloc(len ? len : 1)};
    if (t.value && len)
        memcpy(t.value, value, len);
    return t;
}

static void free_tlvs(em_tlv *t, size_t n)
{
    for (size_t i = 0; i < n; i++)
        free(t[i].value);
}

em_reason em_search_frames(const em_binding *b, int band, int profile, const uint8_t profile2[4],
                           em_message_set set, uint16_t mid, em_frames *out)
{
    if ((band != 0 && band != 1) || profile != 1)
        return EM_UNSUPPORTED_OPERATION;
    uint8_t role = 0, band_octet = (uint8_t)band, supported[2] = {1, 1}, searched[2] = {1, 0},
            prof = (uint8_t)profile;
    em_tlv t[7];
    size_t n = 0;
    t[n++] = tlv(0x01, b->local_al, 6);
    t[n++] = tlv(0x0D, &role, 1);
    t[n++] = tlv(0x0E, &band_octet, 1);
    t[n++] = tlv(0x80, supported, 2);
    t[n++] = tlv(0x81, searched, 2);
    if (set != EM_SET_R1) {
        t[n++] = tlv(0xB3, &prof, 1);
        t[n++] = tlv(0xB4, profile2, 4);
    }
    em_reason r = em_fragment(EM_MULTICAST, b->local_al, SEARCH, mid, t, n, true, 1500, out);
    free_tlvs(t, n);
    return r;
}

bool em_binding_accepts(const em_binding *b, const em_message *m)
{
    if (memcmp(m->destination, b->local_al, 6))
        return false;
    for (size_t i = 0; i < b->nsources; i++)
        if (!memcmp(m->source, b->sources[i], 6))
            return true;
    return false;
}

static bool unicast(const uint8_t mac[6])
{
    static const uint8_t zero[6] = {0};
    return memcmp(mac, zero, 6) && !(mac[0] & 1);
}

/* _header: type, relay flag, unicast addresses, no security envelopes. */
static em_reason header(const em_message *m, uint16_t kind)
{
    if (m->message_type != kind || m->relay || !unicast(m->source) || !unicast(m->destination))
        return EM_INVALID_INPUT;
    for (size_t i = 0; i < m->ntlvs; i++)
        if (m->tlvs[i].kind == 0xAB || m->tlvs[i].kind == 0xAC)
            return EM_UNSUPPORTED_OPERATION;
    return EM_OK;
}

/* _base: IEEE base TLVs aggregate; the fixed-size result must fit. */
static bool base(const em_message *m, uint8_t kind, size_t length, uint8_t *out)
{
    size_t total = 0;
    for (size_t i = 0; i < m->ntlvs; i++)
        if (m->tlvs[i].kind == kind) {
            if (total + m->tlvs[i].len > length)
                return false;
            memcpy(out + total, m->tlvs[i].value, m->tlvs[i].len);
            total += m->tlvs[i].len;
        }
    return total == length;
}

static const em_tlv *one(const em_message *m, uint8_t kind)
{
    const em_tlv *found = NULL;
    for (size_t i = 0; i < m->ntlvs; i++)
        if (m->tlvs[i].kind == kind) {
            if (found)
                return NULL;
            found = &m->tlvs[i];
        }
    return found;
}

em_reason em_parse_response(const em_message *m, em_message_set set, em_advertisement *out)
{
    em_reason r = header(m, RESPONSE);
    if (r != EM_OK)
        return r;
    uint8_t role, band;
    if (!base(m, 0x0F, 1, &role) || role != 0)
        return EM_INVALID_INPUT;
    if (!base(m, 0x10, 1, &band))
        return EM_INVALID_INPUT;
    if (band > 3)
        return EM_UNSUPPORTED_OPERATION;
    const em_tlv *services = one(m, 0x80);
    if (!services || services->len < 1 || services->len != 1 + services->value[0])
        return EM_INVALID_INPUT;
    bool controller = false;
    for (int i = 0; i < services->value[0]; i++)
        controller = controller || services->value[1 + i] == 0;
    if (!controller)
        return EM_INVALID_INPUT;
    int profiles = 0, flags = 0, security = 0;
    const em_tlv *profile = NULL, *flag = NULL;
    for (size_t i = 0; i < m->ntlvs; i++) {
        if (m->tlvs[i].kind == 0xB3) {
            profiles++;
            profile = &m->tlvs[i];
        } else if (m->tlvs[i].kind == 0xDD) {
            flags++;
            flag = &m->tlvs[i];
        } else if (m->tlvs[i].kind == 0xA9) {
            security++;
        }
    }
    if (set == EM_SET_R1 && !profiles) {
        out->profile = 1;
    } else {
        if (profiles != 1 || profile->len != 1)
            return EM_INVALID_INPUT;
        out->profile = profile->value[0];
    }
    if (flags > 1 || (flags && !flag->len) || security > 1)
        return EM_INVALID_INPUT;
    out->band = band;
    out->controller_flags = flags ? flag->value[0] : -1;
    out->security_capability_present = security == 1;
    return EM_OK;
}

static bool basic_caps(const em_radio_caps *r, em_buf *v)
{
    bool ok = em_buf_put(v, r->ruid, 6) && em_buf_u8(v, r->max_bss) && em_buf_u8(v, r->nclasses);
    for (int i = 0; ok && i < r->nclasses; i++) {
        const em_basic_class *c = &r->classes[i];
        ok = em_buf_u8(v, c->operating_class) && em_buf_u8(v, (uint8_t)c->max_eirp_dbm) &&
             em_buf_u8(v, c->nnon_operable) && em_buf_put(v, c->non_operable, c->nnon_operable);
    }
    return ok;
}

em_reason em_m1_frames(const em_binding *b, const em_radio_caps *radio, const em_m1 *m1,
                       em_message_set set, uint16_t mid, em_frames *out)
{
    em_buf basic = {0};
    uint8_t advanced[7];
    memcpy(advanced, radio->ruid, 6);
    advanced[6] = radio->advanced_flags;
    if (!basic_caps(radio, &basic))
        return EM_NO_MEMORY;
    em_tlv t[4];
    size_t n = 0;
    t[n++] = tlv(0x85, basic.data, basic.len);
    t[n++] = tlv(0x11, m1->message.data, m1->message.len);
    if (set != EM_SET_R1) {
        t[n++] = tlv(0xB4, radio->profile2, 4);
        t[n++] = tlv(0xBE, advanced, 7);
    }
    em_reason r = em_fragment(b->controller_al, b->local_al, WSC, mid, t, n, false, 1500, out);
    free_tlvs(t, n);
    em_buf_free(&basic);
    return r;
}

em_reason em_receive_m2(const em_binding *b, const em_radio_caps *radio, const em_m1 *m1,
                        const em_message *m, bool multi_bss, bool shared_session,
                        em_m2_result *out)
{
    if (!em_binding_accepts(b, m))
        return EM_INVALID_INPUT;
    em_reason r = header(m, WSC);
    if (r != EM_OK)
        return r;
    const em_tlv *ruid = one(m, 0x82);
    if (!ruid || ruid->len != 6 || memcmp(ruid->value, radio->ruid, 6))
        return EM_INVALID_INPUT;
    size_t count = 0;
    for (size_t i = 0; i < m->ntlvs; i++) {
        uint8_t k = m->tlvs[i].kind;
        bool empty_mld = k == 0xE0 && m->tlvs[i].len == 1 && m->tlvs[i].value[0] == 0;
        if ((k == 0xB5 || k == 0xB6 || k == 0xE0 || k == 0xE1 || k == 0xEB || k == 0xEC) &&
            !empty_mld)
            return EM_UNSUPPORTED_OPERATION; /* configuration we cannot apply partly */
        if (k == 0x11)
            count++;
    }
    unsigned max = radio->max_bss < 16 ? radio->max_bss : 16;
    if (count < 1 || count > max)
        return EM_INVALID_INPUT;
    em_buf *messages = calloc(count, sizeof(em_buf));
    if (!messages)
        return EM_NO_MEMORY;
    size_t k = 0;
    for (size_t i = 0; i < m->ntlvs; i++)
        if (m->tlvs[i].kind == 0x11) {
            messages[k].data = m->tlvs[i].value; /* borrowed */
            messages[k].len = m->tlvs[i].len;
            k++;
        }
    r = em_m2_decode(m1, messages, count, max, multi_bss, shared_session, out);
    free(messages);
    return r;
}
