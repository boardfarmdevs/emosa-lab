/* AP-Autoconfiguration Search/Response/WSC (spec §2.5), as emosa.wire.autoconfiguration. */
#ifndef EMOSA_AUTOCONF_H
#define EMOSA_AUTOCONF_H

#include "cmdu.h"
#include "wsc.h"

typedef enum { EM_SET_61 = 0, EM_SET_R1 = 1 } em_message_set;

typedef struct {
    uint8_t local_al[6], controller_al[6];
    size_t nsources;
    uint8_t sources[16][6]; /* the controller's interface addresses, its AL first */
} em_binding;

typedef struct {
    uint8_t operating_class;
    int8_t max_eirp_dbm;
    uint8_t nnon_operable;
    uint8_t non_operable[32];
} em_basic_class;

typedef struct {
    uint8_t ruid[6];
    uint8_t max_bss;
    uint8_t nclasses;
    em_basic_class classes[8];
    uint8_t advanced_flags;
    uint8_t profile2[4]; /* Profile-2 AP Capability value */
} em_radio_caps;

typedef struct {
    int band, profile;
    int controller_flags;             /* -1 when absent */
    bool security_capability_present;
} em_advertisement;

/* The Search message's TLVs, sent as a relayed multicast. */
em_reason em_search_frames(const em_binding *b, int band, int profile, const uint8_t profile2[4],
                           em_message_set set, uint16_t mid, em_frames *out);

/* A controller's Response, checked like parse_response; the caller matches MID and band. */
em_reason em_parse_response(const em_message *m, em_message_set set, em_advertisement *out);

/* Check a message against the binding (destination, source). */
bool em_binding_accepts(const em_binding *b, const em_message *m);

em_reason em_m1_frames(const em_binding *b, const em_radio_caps *radio, const em_m1 *m1,
                       em_message_set set, uint16_t mid, em_frames *out);

/* A WSC M2 message for this radio to BSS settings (a single-BSS radio unless multi_bss). */
em_reason em_receive_m2(const em_binding *b, const em_radio_caps *radio, const em_m1 *m1,
                        const em_message *m, bool multi_bss, bool shared_session,
                        em_m2_result *out);

#endif
