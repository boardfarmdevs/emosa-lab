/* Backhaul Steering from the controller, as emosa.wire.backhaul_steering: a Backhaul
 * Steering Request (0x8019) is acknowledged (1905 Ack), handed to the uplink scope, and
 * answered with a Backhaul Steering Response (0x801A, the request's MID) once the move
 * applied or failed, or at the deadline. The move under way and the recent answers are
 * the agent's (em_bh_shared), so the next session answers a move the previous began. */
#ifndef EMOSA_BHSTEER_H
#define EMOSA_BHSTEER_H

#include <cjson/cJSON.h>

#include "cmdu.h"

typedef struct {
    bool pending;
    uint16_t pending_mid;
    uint8_t request[14]; /* station, target, operating class, channel */
    double deadline;
    size_t nanswered;
    struct {
        uint16_t mid;
        uint8_t request[14];
        uint8_t error;
        double expiry;
    } answered[16];
} em_bh_shared;

/* starts moving the station to bssid ("aa:.." lower case): NULL, or why not */
typedef const char *(*em_bh_executor)(void *ctx, const char *bssid);
/* 0 under way, 1 applied, -1 failed (*why) */
typedef int (*em_bh_outcome)(void *ctx, const char *bssid, const char **why);

typedef struct {
    em_bh_shared *shared;
    uint8_t controller[6], local_al[6];
    em_bh_executor executor;
    em_bh_outcome outcome;
    void *ctx;
    bool closed;
    cJSON *last;
    struct {
        size_t n;
        struct {
            char name[96];
            unsigned count;
        } items[16];
    } counts;
} em_bh_coordinator;

void em_bh_start(em_bh_coordinator *c, em_bh_shared *shared, const uint8_t controller[6], const uint8_t local_al[6],
                 em_bh_executor executor, em_bh_outcome outcome, void *ctx);
/* A closed session takes no new request; a move under way is answered by the next. */
void em_bh_close(em_bh_coordinator *c);

/* 0x8019: the result label, or NULL and *error when refused. stations: the pod's backhaul
 * stations now (the request's STA must be one). */
const char *em_bh_handle(em_bh_coordinator *c, const em_message *m, double now, const uint8_t (*stations)[6],
                         size_t nstations, em_frames *out, em_reason *error);
/* Answer a started move once its outcome is known, or at its deadline. */
void em_bh_tick(em_bh_coordinator *c, double now, bool source_available, em_frames *out);
cJSON *em_bh_status(const em_bh_coordinator *c);

#endif
