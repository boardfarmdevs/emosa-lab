/* SPDX-License-Identifier: Apache-2.0 */
/* The provisioned agent's control procedures (spec §2.4, §3.4, §3.7): channel,
 * Multi-AP policy and client steering, as emosa.wire.channel, .reporting_policy
 * and .steering. */
#ifndef EMOSA_CONTROL_H
#define EMOSA_CONTROL_H

#include <cjson/cJSON.h>

#include "autoconf.h"
#include "stats.h"
#include "view.h"

/* A decoded Steering Request TLV (spec §3.7). */
typedef struct {
    uint8_t source_bssid[6];
    bool mandate, disassoc_imminent, abridged;
    uint16_t window, disassoc_timer;
    size_t nstations, ntargets;
    uint8_t stations[255][6]; /* the TLV's one-byte counts: as many as it can name */
    struct {
        uint8_t bssid[6], op_class, channel;
    } targets[255];
} em_steering_request;

/* A radio the agent reports, for the channel procedures and the policy (spec 9). */
typedef struct {
    uint8_t ruid[6], op_class;
    int channel, tx_power_dbm;
    int8_t max_eirp_dbm;
    bool measured;
    /* the pod reports no transmit power for the radio (OpenSync leaves Wifi_Radio_State.tx_power
     * unset when its VIFs disagree): tx_power_dbm is its advertised maximum EIRP, an upper bound,
     * not a measurement (channel.OperatingRadio.bound, spec 9) */
    bool bound;
} em_control_radio;

/* Starts a mandate on the pod; NULL, or why it did not start (e.g. "busy"). */
typedef const char *(*em_steering_executor)(void *ctx, const em_steering_request *r, uint16_t mid);

typedef struct {
    em_binding binding;
    const em_radio_view *radio; /* the represented radio (its BSSes and stations) */
    /* every radio the agent reports, the fronthaul first (spec 9): its operating class, its one
     * operable channel (a second radio's follows the pod's station), the operated power (unmeasured:
     * the channel procedures are not ready) and the advertised maximum. A policy's radio lists and a
     * channel request may name each; nradios 0: the represented radio alone, on class 81 at
     * tx_power_dbm, max_eirp_dbm. */
    size_t nradios;
    em_control_radio radios[4];
    /* the further radios the agent shows (spec 9), their BSSes and stations: a station on any of
     * them is associated with the pod */
    size_t nfurther;
    const em_radio_view *further[3];
    int tx_power_dbm;           /* the operated power */
    int8_t max_eirp_dbm;        /* advertised */
    uint16_t previous_mid;      /* the agent's own messages take the next MIDs */
    em_steering_executor executor;
    void *executor_ctx;
    /* recent requests: (message type << 16 | MID) */
    size_t nrecent;
    uint32_t recent[64];
    /* the stored channel and reporting policy, as receipt only */
    bool channel_policy_declined;
    /* Operating Channel Report TLVs sent with a radio's bound for its power (counted: the agent
     * logs and counts each, operating_power_bound_reported) */
    unsigned bound_reported;
    /* keeps an accepted channel policy's record before it is answered
     * (emosa.wire.channel.ChannelPolicyStore); false: it could not be kept, and the
     * request is refused unanswered (NOT_READY). NULL: not kept. */
    bool (*keep_channel_policy)(void *ctx, const cJSON *record);
    void *keep_ctx;
    /* the Channel Scan Report's time, RFC 3339 UTC; NULL: the system clock */
    void (*utc)(char out[40]);
    /* the pod's statistics, whose probe requests answer an Unassociated STA Link
     * Metrics Query (spec §3.9); NULL: no telemetry, every station is refused */
    const em_pod_stats *stats;
    /* epoch seconds, for probe ages; NULL: the system clock */
    double (*wall)(void);
    /* told the stations queried on the pod's own channel (emosa.agent.probe_watch.ask) */
    void (*watch)(void *ctx, const uint8_t (*stations)[6], size_t n);
    void *watch_ctx;
} em_control;

/* Handle one complete message. Returns the result label, or NULL when the
 * message is not a control procedure. Frames to send are appended to *out.
 * *error is set when the message is refused without an answer. */
const char *em_control_handle(em_control *c, const em_message *m, em_frames *out, em_reason *error);

em_reason em_decode_steering_request(const em_message *m, em_steering_request *out);

#endif
