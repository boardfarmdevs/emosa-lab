/* IEEE 1905.1 envelope (spec §2.1), as the reference emosa.wire.cmdu. */
#ifndef EMOSA_CMDU_H
#define EMOSA_CMDU_H

#include "common.h"

#define EM_ETHERTYPE 0x893A
#define EM_MAX_CMDU 1500
#define EM_MAX_VALUE 0x3FFF
#define EM_MAX_TLVS 256

extern const uint8_t EM_MULTICAST[6];

typedef struct {
    uint8_t kind;
    uint16_t len;
    uint8_t *value; /* owned */
} em_tlv;

typedef struct {
    uint8_t destination[6], source[6];
    uint16_t message_type, mid;
    bool relay;
    size_t ntlvs;
    em_tlv *tlvs;
    unsigned fragments;
} em_message;

void em_message_free(em_message *m);

/* Ethernet frames of one message, fragmented at TLV boundaries within mtu. */
typedef struct {
    size_t count;
    em_buf *frames;
} em_frames;

void em_frames_free(em_frames *f);

em_reason em_fragment(const uint8_t destination[6], const uint8_t source[6],
                      uint16_t message_type, uint16_t mid, const em_tlv *tlvs, size_t ntlvs,
                      bool relay, unsigned mtu, em_frames *out);

/* Bounded reassembly with fixed deadlines; conflicting fragments poison a context. */
typedef struct em_reassembler em_reassembler;
typedef double (*em_clock)(void *ctx);

em_reassembler *em_reassembler_new(em_clock clock, void *clock_ctx, double timeout,
                                   unsigned max_contexts, size_t max_bytes,
                                   size_t max_message_bytes, unsigned max_fragments);
em_reassembler *em_reassembler_default(void);
void em_reassembler_free(em_reassembler *r);

/* EM_OK with *out set when complete, EM_OK with *out NULL while incomplete. */
em_reason em_reassembler_feed(em_reassembler *r, const uint8_t *frame, size_t len,
                              const char *ingress, em_message **out);

#endif
