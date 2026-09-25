/* IEEE 1905.1 envelope (spec §2.1): encoding, TLV-boundary fragmentation, reassembly.
 * Mirrors the reference emosa.wire.cmdu: the same budgets, rejections and reasons. */
#include "cmdu.h"

#include <stdlib.h>
#include <string.h>
#include <time.h>

const uint8_t EM_MULTICAST[6] = {0x01, 0x80, 0xc2, 0x00, 0x00, 0x13};
#define HEADER_SIZE 8

void em_message_free(em_message *m)
{
    if (!m)
        return;
    for (size_t i = 0; i < m->ntlvs; i++)
        free(m->tlvs[i].value);
    free(m->tlvs);
    free(m);
}

void em_frames_free(em_frames *f)
{
    for (size_t i = 0; i < f->count; i++)
        em_buf_free(&f->frames[i]);
    free(f->frames);
    f->frames = NULL;
    f->count = 0;
}

static bool encode_fragment(em_buf *out, const uint8_t destination[6], const uint8_t source[6],
                            uint16_t message_type, uint16_t mid, uint8_t fid, bool last,
                            bool relay, const uint8_t *payload, size_t len)
{
    return em_buf_put(out, destination, 6) && em_buf_put(out, source, 6) &&
           em_buf_u16(out, EM_ETHERTYPE) && em_buf_u8(out, 0) && em_buf_u8(out, 0) &&
           em_buf_u16(out, message_type) && em_buf_u16(out, mid) && em_buf_u8(out, fid) &&
           em_buf_u8(out, (uint8_t)((last ? 0x80 : 0) | (relay ? 0x40 : 0))) &&
           em_buf_put(out, payload, len);
}

em_reason em_fragment(const uint8_t destination[6], const uint8_t source[6],
                      uint16_t message_type, uint16_t mid, const em_tlv *tlvs, size_t ntlvs,
                      bool relay, unsigned mtu, em_frames *out)
{
    memset(out, 0, sizeof(*out));
    if (mtu < 14 || mtu > EM_MAX_CMDU || ntlvs > EM_MAX_TLVS)
        return EM_INVALID_INPUT;
    size_t budget = mtu - HEADER_SIZE;
    em_buf chunks[64];
    size_t nchunks = 0;
    em_buf current = {0};
    em_reason reason = EM_OK;
    for (size_t i = 0; i < ntlvs; i++) {
        if (tlvs[i].kind == 0 || tlvs[i].len > EM_MAX_VALUE) {
            reason = EM_INVALID_INPUT;
            goto fail;
        }
        size_t encoded = 3 + (size_t)tlvs[i].len;
        /* Leave room for EOM in the final fragment; never send an EOM-only fragment. */
        if (encoded + 3 > budget) {
            reason = EM_INVALID_INPUT;
            goto fail;
        }
        if (current.len + encoded + 3 > budget) {
            if (nchunks == 64) {
                reason = EM_INVALID_INPUT;
                goto fail;
            }
            chunks[nchunks++] = current;
            memset(&current, 0, sizeof(current));
        }
        if (!em_buf_u8(&current, tlvs[i].kind) || !em_buf_u16(&current, tlvs[i].len) ||
            !em_buf_put(&current, tlvs[i].value, tlvs[i].len)) {
            reason = EM_NO_MEMORY;
            goto fail;
        }
    }
    static const uint8_t end[3] = {0, 0, 0};
    if (nchunks == 64 || !em_buf_put(&current, end, 3)) {
        reason = nchunks == 64 ? EM_INVALID_INPUT : EM_NO_MEMORY;
        goto fail;
    }
    chunks[nchunks++] = current;
    memset(&current, 0, sizeof(current));
    out->frames = calloc(nchunks, sizeof(em_buf));
    if (!out->frames) {
        reason = EM_NO_MEMORY;
        goto fail;
    }
    out->count = nchunks;
    for (size_t fid = 0; fid < nchunks; fid++) {
        if (!encode_fragment(&out->frames[fid], destination, source, message_type, mid,
                             (uint8_t)fid, fid == nchunks - 1, relay, chunks[fid].data,
                             chunks[fid].len)) {
            reason = EM_NO_MEMORY;
            goto fail;
        }
    }
    for (size_t i = 0; i < nchunks; i++)
        em_buf_free(&chunks[i]);
    return EM_OK;
fail:
    em_buf_free(&current);
    for (size_t i = 0; i < nchunks; i++)
        em_buf_free(&chunks[i]);
    em_frames_free(out);
    return reason;
}

/* -- reassembly ------------------------------------------------------------------ */

typedef struct {
    bool present, last;
    size_t ntlvs;
    em_tlv *tlvs;
} part;

typedef struct {
    bool used, relay, poisoned, have_last;
    char ingress[32];
    uint8_t source[6], destination[6];
    uint16_t message_type, mid;
    double started;
    unsigned last;
    size_t size;
    part parts[256];
} context;

struct em_reassembler {
    em_clock clock;
    void *clock_ctx;
    double timeout;
    unsigned max_contexts, max_fragments;
    size_t max_bytes, max_message_bytes, buffered;
    context *contexts;
};

static double monotonic(void *ctx)
{
    (void)ctx;
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

em_reassembler *em_reassembler_new(em_clock clock, void *clock_ctx, double timeout,
                                   unsigned max_contexts, size_t max_bytes,
                                   size_t max_message_bytes, unsigned max_fragments)
{
    if (!(timeout > 0 && timeout <= 60) || max_contexts < 1 || max_contexts > 1024 ||
        max_fragments < 1 || max_fragments > 256 || max_message_bytes < 1 ||
        max_message_bytes > max_bytes || max_bytes > 16777216)
        return NULL;
    em_reassembler *r = calloc(1, sizeof(*r));
    if (!r)
        return NULL;
    r->contexts = calloc(max_contexts, sizeof(context));
    if (!r->contexts) {
        free(r);
        return NULL;
    }
    r->clock = clock ? clock : monotonic;
    r->clock_ctx = clock_ctx;
    r->timeout = timeout;
    r->max_contexts = max_contexts;
    r->max_bytes = max_bytes;
    r->max_message_bytes = max_message_bytes;
    r->max_fragments = max_fragments;
    return r;
}

em_reassembler *em_reassembler_default(void)
{
    return em_reassembler_new(NULL, NULL, 5.0, 64, 2 * 1024 * 1024, 65536, 64);
}

static void free_tlvs(em_tlv *tlvs, size_t n)
{
    for (size_t i = 0; i < n; i++)
        free(tlvs[i].value);
    free(tlvs);
}

static void clear_parts(context *c)
{
    for (int i = 0; i < 256; i++) {
        if (c->parts[i].present)
            free_tlvs(c->parts[i].tlvs, c->parts[i].ntlvs);
        memset(&c->parts[i], 0, sizeof(part));
    }
}

static void drop(em_reassembler *r, context *c)
{
    r->buffered -= c->size;
    clear_parts(c);
    memset(c, 0, sizeof(*c));
}

void em_reassembler_free(em_reassembler *r)
{
    if (!r)
        return;
    for (unsigned i = 0; i < r->max_contexts; i++)
        if (r->contexts[i].used)
            clear_parts(&r->contexts[i]);
    free(r->contexts);
    free(r);
}

static void expire(em_reassembler *r)
{
    double now = r->clock(r->clock_ctx);
    for (unsigned i = 0; i < r->max_contexts; i++)
        if (r->contexts[i].used && now - r->contexts[i].started >= r->timeout)
            drop(r, &r->contexts[i]);
}

/* The TLVs of one fragment payload (reserved length bits masked, padding after EOM). */
static em_reason parse_tlvs(const uint8_t *p, size_t n, bool final, em_tlv **out, size_t *count)
{
    size_t offset = 0, cap = 0;
    em_tlv *tlvs = NULL;
    *out = NULL;
    *count = 0;
    while (offset < n) {
        if (n - offset < 3)
            goto invalid;
        uint8_t kind = p[offset];
        uint16_t len = (uint16_t)((p[offset + 1] << 8 | p[offset + 2]) & EM_MAX_VALUE);
        offset += 3;
        if (kind == 0) {
            if (len || !final)
                goto invalid;
            *out = tlvs;
            return EM_OK;
        }
        if (offset + len > n || *count >= EM_MAX_TLVS)
            goto invalid;
        if (*count == cap) {
            cap = cap ? cap * 2 : 8;
            em_tlv *grown = realloc(tlvs, cap * sizeof(em_tlv));
            if (!grown)
                goto invalid;
            tlvs = grown;
        }
        em_tlv *t = &tlvs[(*count)++];
        t->kind = kind;
        t->len = len;
        t->value = malloc(len ? len : 1);
        if (!t->value) {
            (*count)--;
            goto invalid;
        }
        memcpy(t->value, p + offset, len);
        offset += len;
    }
    if (final || *count == 0)
        goto invalid; /* missing final end marker; empty non-final fragment */
    *out = tlvs;
    return EM_OK;
invalid:
    free_tlvs(tlvs, *count);
    *count = 0;
    return EM_INVALID_INPUT;
}

static bool same_tlvs(const part *a, bool last, const em_tlv *tlvs, size_t n)
{
    if (a->last != last || a->ntlvs != n)
        return false;
    for (size_t i = 0; i < n; i++)
        if (a->tlvs[i].kind != tlvs[i].kind || a->tlvs[i].len != tlvs[i].len ||
            memcmp(a->tlvs[i].value, tlvs[i].value, tlvs[i].len))
            return false;
    return true;
}

static em_reason poison(em_reassembler *r, context *c)
{
    r->buffered -= c->size;
    clear_parts(c);
    c->size = 0;
    c->poisoned = true;
    return EM_INVALID_INPUT;
}

em_reason em_reassembler_feed(em_reassembler *r, const uint8_t *frame, size_t len,
                              const char *ingress, em_message **out)
{
    *out = NULL;
    expire(r);
    if (len < 22 || len > 14 + EM_MAX_CMDU)
        return EM_INVALID_INPUT;
    if ((frame[12] << 8 | frame[13]) != EM_ETHERTYPE)
        return EM_INVALID_INPUT;
    uint8_t version = frame[14];
    uint16_t message_type = (uint16_t)(frame[16] << 8 | frame[17]);
    uint16_t mid = (uint16_t)(frame[18] << 8 | frame[19]);
    uint8_t fid = frame[20];
    bool last = frame[21] & 0x80, relay = frame[21] & 0x40;
    if (version != 0)
        return EM_UNSUPPORTED_OPERATION; /* reserved CMDU version; no dispatch */
    const char *in = ingress ? ingress : "offline";
    context *c = NULL, *spare = NULL;
    unsigned used = 0;
    for (unsigned i = 0; i < r->max_contexts; i++) {
        context *x = &r->contexts[i];
        if (!x->used) {
            if (!spare)
                spare = x;
            continue;
        }
        used++;
        if (!strncmp(x->ingress, in, sizeof(x->ingress)) && !memcmp(x->source, frame + 6, 6) &&
            !memcmp(x->destination, frame, 6) && x->message_type == message_type &&
            x->mid == mid)
            c = x;
    }
    if (!c) {
        if (used >= r->max_contexts || !spare)
            return EM_BUSY;
        c = spare;
        memset(c, 0, sizeof(*c));
        c->used = true;
        strncpy(c->ingress, in, sizeof(c->ingress) - 1);
        memcpy(c->source, frame + 6, 6);
        memcpy(c->destination, frame, 6);
        c->message_type = message_type;
        c->mid = mid;
        c->relay = relay;
        c->started = r->clock(r->clock_ctx);
    }
    if (c->poisoned)
        return EM_INVALID_INPUT;
    if (fid >= r->max_fragments || relay != c->relay)
        return poison(r, c);
    em_tlv *tlvs;
    size_t ntlvs;
    if (parse_tlvs(frame + 22, len - 22, last, &tlvs, &ntlvs) != EM_OK)
        return poison(r, c);
    if (c->parts[fid].present) {
        bool same = same_tlvs(&c->parts[fid], last, tlvs, ntlvs);
        free_tlvs(tlvs, ntlvs);
        return same ? EM_OK : poison(r, c);
    }
    bool later = false;
    size_t stored = 0;
    for (int i = 0; i < 256; i++)
        if (c->parts[i].present) {
            stored += c->parts[i].ntlvs;
            if (i > fid)
                later = true;
        }
    size_t size = 0;
    for (size_t i = 0; i < ntlvs; i++)
        size += tlvs[i].len + 3u;
    if ((c->have_last && (fid > c->last || (last && fid != c->last))) || (last && later) ||
        c->size + size > r->max_message_bytes || r->buffered + size > r->max_bytes ||
        stored + ntlvs > EM_MAX_TLVS) {
        free_tlvs(tlvs, ntlvs);
        return poison(r, c);
    }
    c->parts[fid] = (part){true, last, ntlvs, tlvs};
    c->size += size;
    r->buffered += size;
    if (last) {
        c->have_last = true;
        c->last = fid;
    }
    if (!c->have_last)
        return EM_OK;
    for (unsigned i = 0; i <= c->last; i++)
        if (!c->parts[i].present)
            return EM_OK;
    em_message *m = calloc(1, sizeof(*m));
    if (!m)
        return EM_NO_MEMORY;
    memcpy(m->destination, frame, 6);
    memcpy(m->source, frame + 6, 6);
    m->message_type = message_type;
    m->mid = mid;
    m->relay = relay;
    m->fragments = c->last + 1;
    for (unsigned i = 0; i <= c->last; i++)
        m->ntlvs += c->parts[i].ntlvs;
    m->tlvs = calloc(m->ntlvs ? m->ntlvs : 1, sizeof(em_tlv));
    if (!m->tlvs) {
        free(m);
        return EM_NO_MEMORY;
    }
    size_t k = 0;
    for (unsigned i = 0; i <= c->last; i++) {
        for (size_t j = 0; j < c->parts[i].ntlvs; j++)
            m->tlvs[k++] = c->parts[i].tlvs[j]; /* ownership moves */
        free(c->parts[i].tlvs);
        c->parts[i] = (part){0};
    }
    r->buffered -= c->size;
    memset(c, 0, sizeof(*c));
    *out = m;
    return EM_OK;
}
