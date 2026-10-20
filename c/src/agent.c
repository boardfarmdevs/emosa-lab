/* SPDX-License-Identifier: Apache-2.0 */
/* emosa-agent-c: the EMOSA virtual EasyMesh agent for one OpenSync pod (see c/README.md
 * and spec/design.md). Same configuration and status file as the Python
 * agent (emosa.agent.pod); one owner thread, a poll loop (spec/design.md §5.1).
 *
 *   emosa-agent-c CONFIG.json [--profiles DIR]
 *
 * The same state directory as the Python agent (journal, secrets, kept policies), so
 * either can take over a pod from the other. The known differences are in c/README.md. */
#include <cjson/cJSON.h>
#include <errno.h>
#include <openssl/rand.h>
#include <openssl/sha.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "autoconf.h"
#include "bhsteer.h"
#include "canon.h"
#include "channel_store.h"
#include "early.h"
#include "cmdu.h"
#include "control.h"
#include "engine.h"
#include "ethernet.h"
#include "journal.h"
#include "lifecycle.h"
#include "log.h"
#include "version.h"
#include "jschema.h"
#include "mqtt.h"
#include "ovs.h"
#include "ovsdb.h"
#include "peers.h"
#include "proc.h"
#include "reporting.h"
#include "scope.h"
#include "scope_ap.h"
#include "scope_steering.h"
#include "scope_telemetry.h"
#include "scope_gre_parent.h"
#include "scope_gtp.h"
#include "scope_node.h"
#include "scope_plan.h"
#include "scope_wired.h"
#include "scope_uplink.h"
#include "scope_watch.h"
#include "stats.h"
#include "southbound.h"
#include "vault.h"
#include "view.h"
#include "wsc.h"

#define REFRESH_PERIOD 0.5
#define LEASE 1.5
/* spec 2.5 (pod.RECONNECT_WINDOW): a provisioned session held this long while its pod's link is
 * away, and kept when the same pod comes back within it (OpenSync's CM opens the link again 15 to
 * 18 s after a backhaul move; well inside a controller's own silence windows) */
#define RECONNECT_WINDOW 60
#define DISCOVERY_PERIOD 60
#define AP_DEADLINE 120

static volatile sig_atomic_t stopping;
static void on_signal(int sig) { (void)sig; stopping = 1; }

static double now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static double wall(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static double stats_clock(void *ctx)
{
    (void)ctx;
    return wall();
}

#define LOG(...) em_log(EM_LOG_INFO, "emosa.agent", __VA_ARGS__)
#define WARN(...) em_log(EM_LOG_WARNING, "emosa.agent", __VA_ARGS__)
#define FAIL(...) em_log(EM_LOG_ERROR, "emosa.agent", __VA_ARGS__)

/* -- counters (the status file's names) ------------------------------------------- */

typedef struct {
    size_t n;
    struct {
        char name[64];
        unsigned count;
    } items[96];
} counters;

static void count(counters *c, const char *name)
{
    for (size_t i = 0; i < c->n; i++)
        if (!strcmp(c->items[i].name, name)) {
            c->items[i].count++;
            return;
        }
    if (c->n < 96) {
        EM_FORMAT_FIXED(c->items[c->n].name, sizeof(c->items[c->n].name), "%s", name);
        c->items[c->n++].count = 1;
    }
}

/* The counters whose names start with prefix ("" for all). */
static cJSON *counters_json(const counters *c, const char *prefix)
{
    cJSON *o = cJSON_CreateObject();
    for (size_t i = 0; i < c->n; i++)
        if (!strncmp(c->items[i].name, prefix, strlen(prefix)))
            cJSON_AddNumberToObject(o, c->items[i].name, c->items[i].count);
    return o;
}

/* -- the agent ---------------------------------------------------------------------- */

#define S_NONE EM_SESSION_NONE
#define S_DISCOVERING EM_SESSION_DISCOVERING
#define S_AWAITING_M2 EM_SESSION_AWAITING_M2
#define S_PROVISIONING EM_SESSION_PROVISIONING
#define S_FAILED EM_SESSION_FAILED
#define S_SOURCE_LOST EM_SESSION_SOURCE_LOST
#define S_INCOMPATIBLE EM_SESSION_INCOMPATIBLE
static const char *const SESSION_NAMES[] = {"recovering",   "discovering", "awaiting_m2", "provisioning",
                                            "failed",       "source_lost", "incompatible"};

#define EXTRA_RADIOS 2 /* the profile's further radios (pod-profile schema: radios, at most 2) */

/* A further radio of the pod (spec 9): its own write scope (journal, engine) configured by its
 * own M2, and in a session its own WSC exchange (emosa.wire.onboarding radio_bridge_factory) */
typedef struct {
    const char *band;
    em_ap_scope ap;
    em_engine engine;
    em_journal *journal;
    /* as of the last refresh: the pod brought it up (in the reports), the BSSes shown on it */
    bool bound;
    size_t caps_index; /* its radio among the reported ones (the AP Capability Report's order) */
    em_radio_view represented;
    int channel;
    /* the session's exchange: M1 sent, its M2 not yet taken; the context its M1 went in */
    bool started, awaiting, has_m1, context_live;
    em_m1 m1;
    uint8_t ruid[6];
    char exchange_id[33], m1_sha256[65], op_of_session[37];
    int context_generation;
    char context_schema[65], context_token[65];
} radio_scope;


typedef struct {
    /* configuration */
    cJSON *config;
    const char *pod_id, *serial, *interface, *state_dir, *profile_id;
    const char *status_dir; /* run_dir when configured (a RAM disk), else state_dir */
    uint8_t al[6], controller[6];
    bool r1, multi_bss, shared_session, steering_on;
    bool watch_on; /* the probe watch (§3.9): with telemetry, never on a legacy pod (spec 3.10) */
    double topology_query_window; /* 0: the renewal rule is off */
    const char *underlay;          /* the pod's underlay as a GRE parent (spec 8.6); NULL: none */
    em_profile profile;
    /* I/O */
    em_ovsdb *ovs;
    em_ethernet eth;
    uint16_t mid;
    /* the report source */
    bool available, caps_fixed;
    int caps_generation; /* the pod source (OVSDB generation) caps are fixed for */
    double refreshed_at;
    /* spec 2.5: the pod source the session is on (the pod, the start of its OpenSync, as of the
     * last refresh), the new generations that continued it and those that were another source by
     * reason; a Topology Notification due for a kept reconnection; the radios whose operable
     * channel moved in a kept session, their reports due */
    char source_serial[65], source_node[65], source_instance[17];
    bool has_source;
    unsigned reconnections_continued, reconnections_config_lost;
    counters reconnections_fresh;
    unsigned session_starts; /* spec 3.10: a legacy pod's starts as its agent saw them (its start
                              * markers, else its sessions) */
    char start_token[17];    /* the start marker's token as last read ("" before one) */
    unsigned start_writes;   /* the markers it wrote */
    int start_tried_generation; /* the database generation and time of its last write */
    double start_tried_at;
    /* since when (wall time, 0: marked) the pod's start has no marker, and why the last write
     * failed ("" before one did): a pod whose database keeps refusing it stays not ready */
    double start_pending_since;
    char start_error[EM_START_ERROR_MAX];
    unsigned auth_reloads;   /* spec 3.10: the oftags auth_reload restored */
    unsigned bridge_ports;   /* spec 3.10: the APs bridge_port put into their bridge */
    unsigned bridge_modes;   /* spec 3.10: the writes bridge_mode made */
    bool phy_netdev_up;      /* spec 3.10: the fleet's phy_netdev_up, on by default (em_phy_netdev_ops) */
    unsigned phy_writes;     /* spec 3.10: the writes phy_netdev made */
    bool notify_due, check_drift;
    uint8_t channel_moves[1 + EXTRA_RADIOS][6];
    size_t nchannel_moves;
    int generation;
    unsigned long revision; /* the pod database's, at the last refresh */
    em_device_view view;
    em_radio_view represented; /* the radio with only the BSSes the agent represents */
    bool has_primary;
    int channel, tx_power;
    /* the fronthaul radio's channel as wm shows it (has_observed: it shows one) */
    int observed_channel;
    bool has_observed;
    int8_t max_eirp;
    em_tlv_list caps;
    em_tlv inventory;
    size_t nfirst;
    struct first_seen {
        uint8_t mac[6];
        double at;
    } first_seen[256];
    /* the session: its attempts and the agent's own renewals (lifecycle.c) */
    em_attempts at;
    const char *admission_issues[8]; /* why the controller's Response was refused */
    size_t nadmission_issues;
    em_renew renewals;
    em_reassembler *assembly;
    uint16_t search_mids[3];
    em_m1 m1;
    bool has_m1;
    char op_of_session[37];
    em_control control;
    bool control_ready;
    size_t nclients;
    struct {
        uint8_t bssid[6], mac[6];
    } clients[256];
    char last_topology[512];
    em_reannounce reannounce; /* every client again, once, after M2 */
    unsigned topology_responses;
    double tokens, token_time, next_discovery;
    counters counts;
    /* the AP scope: its journal, secrets and engine (emosa.reconcile, .store, .secrets) */
    const char *run_id;
    em_vault vault;
    em_journal_schemas schemas;
    em_journal *journal;
    em_ap_scope ap;
    em_engine ap_engine;
    /* spec 9: each further radio's scope, in the profile's order; the reported radios' max EIRP,
     * kept for one pod source (the fronthaul first) */
    radio_scope radio_scopes[EXTRA_RADIOS];
    size_t nradio_scopes;
    em_topology_radio metric_radios[1 + EXTRA_RADIOS]; /* the radios of the last metric source */
    struct {
        uint8_t ruid[6];
        int8_t max_eirp;
    } reported[1 + EXTRA_RADIOS];
    size_t nreported;
    /* the live WSC exchange: its identity, the M1 digest and the context at M1 */
    char exchange_id[33], m1_sha256[65];
    bool context_live;
    int context_generation;
    char context_schema[65], context_token[65];
    /* telemetry (the pod publishes its statistics; the agent subscribes) and the
     * AP metrics reported from them */
    bool telemetry_on, pod_metrics_on;
    em_telemetry_scope telemetry;
    em_pod_stats stats;
    em_mqtt *mqtt;
    char subscribe_host[256];
    int subscribe_port;
    double freshness;
    em_policy_store *policy_store;
    em_channel_store *channel_store; /* the accepted channel policy's record */
    em_reporting reporting;
    bool reporting_live;
    unsigned writes;
    em_steering_scope steering; /* client steering (emosa.agent.steering) */
    em_watch_scope watch;       /* the probe watch (emosa.agent.probe_watch), with telemetry */
    bool uplink_on;             /* the uplink scope (emosa.agent.uplink): uplink.mode multi-ap */
    em_uplink_scope uplink;
    bool wired_on;              /* the wired uplink scope (emosa.agent.wired): uplink.mode ethernet */
    em_wired_scope wired;
    bool gtp_on;                /* the GTP station scope (emosa.agent.gtp_station): uplink.bootstrap */
    em_gtp_scope gtp;
    bool plan_on;               /* the radio plan scope (emosa.agent.radio_plan): profile radio_plan emosa */
    em_plan_scope plan;
    bool node_on;               /* the node-config scope (emosa.agent.node_config): configured modules */
    em_node_scope node;
    em_gre_scope *gre;          /* a GRE parent's tunnels (emosa.agent.gre_parent, spec 8.6); NULL: none */
    em_bh_shared bh_shared;     /* Backhaul Steering under way: the agent's, across sessions */
    em_bh_coordinator bh;
    bool bh_live;
    em_backhaul backhaul;       /* the pod's EasyMesh backhaul, when it is one */
    bool has_backhaul;
    /* spec 3.10: the connected pod's schema is not the profile's generation: nothing read or written */
    bool pod_refused;
    /* the pod's radios with a backhaul station, the one in use first: the Backhaul STA
     * Capability Report (spec §8.3), as of the last refresh */
    em_backhaul_radio radios[EM_UPLINK_BANDS + 1];
    size_t nradios;
    bool peers_on;              /* the fleet's other agents (spec §8.5): with a run_dir */
    em_peers peers;
    /* spec §8.3: every AP of the pod seen (an enabled VIF with a MAC in its State), kept
     * in <state_dir>/aps.json so that a restarted agent still names them while its pod is away;
     * the status says which are up (ap_up), for the fleet's children whose kept target one is */
    uint8_t aps_known[EM_PEER_APS][6];
    size_t naps_known;
    em_backhaul_link links[16]; /* the Wi-Fi backhaul's 1905 neighbors, as of the last refresh */
    size_t nlinks;
    em_early early; /* the Early AP Capability Report awaiting its Ack */
    int early_generation; /* the pod State it was built from */
    unsigned long early_revision;
    char status_summary[512];
    double status_written;
} agent;

static uint16_t next_mid(agent *a) { return ++a->mid; }
static uint16_t next_mid_cb(void *ctx) { return next_mid(ctx); }

static size_t shown_radios(const agent *a, em_topology_radio out[1 + EXTRA_RADIOS]);

/* what an AP Metrics Response is built from (spec §3.8): every radio the agent shows (spec 9) */
static em_metric_source metric_source(agent *a)
{
    size_t n = shown_radios(a, a->metric_radios);
    return (em_metric_source){a->metric_radios, n, a->pod_metrics_on ? &a->stats : NULL, a->profile.esp_be,
                              a->freshness, wall};
}

static void send_frames(agent *a, em_frames *f)
{
    for (size_t i = 0; i < f->count; i++)
        if (em_ethernet_send(&a->eth, f->frames[i].data, f->frames[i].len) != EM_OK)
            WARN("1905 send failed");
    em_frames_free(f);
}

static void send_message(agent *a, const uint8_t *dst, uint16_t type, uint16_t mid, const em_tlv *tlvs,
                         size_t n, bool relay)
{
    em_frames f;
    if (em_fragment(dst, a->al, type, mid, tlvs, n, relay, 1500, &f) == EM_OK)
        send_frames(a, &f);
}

/* -- the OVSDB session wrapper the scopes use --------------------------------------- */

static cJSON *transact(void *ctx, const cJSON *ops)
{
    agent *a = ctx;
    if (a->pod_refused) { /* spec 3.10: never written; each operation refused (the write rejected) */
        cJSON *results = cJSON_CreateArray();
        for (int i = 0; i < cJSON_GetArraySize(ops); i++) {
            cJSON *e = cJSON_CreateObject();
            cJSON_AddStringToObject(e, "error", "SCHEMA_MISMATCH");
            cJSON_AddItemToArray(results, e);
        }
        return results;
    }
    a->writes++;
    return em_ovsdb_transact(a->ovs, ops, 2.0);
}

/* -- the report source (refresh every 0.5 s, 1.5 s lease) --------------------------- */

static const cJSON *vif_state_row(agent *a, const char *if_name)
{
    const cJSON *row;
    cJSON_ArrayForEach(row, cJSON_GetObjectItemCaseSensitive(em_ovsdb_tables(a->ovs), "Wifi_VIF_State"))
    {
        const char *name = ovs_str(row, "if_name");
        if (name && !strcmp(name, if_name))
            return row;
    }
    return NULL;
}

static bool same_tlvs(const em_tlv_list *x, const em_tlv_list *y)
{
    bool same = x->count == y->count;
    for (size_t i = 0; same && i < x->count; i++)
        same = x->tlvs[i].kind == y->tlvs[i].kind && x->tlvs[i].len == y->tlvs[i].len &&
               !memcmp(x->tlvs[i].value, y->tlvs[i].value, x->tlvs[i].len);
    return same;
}

/* Only the further radios of two capability reports differ (pod.further_radios_changed): the
 * Early AP Capability TLV and the fronthaul's three before them, the device's three after them,
 * are the same (spec 9: a second radio's channel follows the pod's station, and it is reported
 * once the pod brings it up). */
static bool further_radios_changed(const em_tlv_list *before, const em_tlv_list *after)
{
    if (before->count < 7 || after->count < 7)
        return false;
    em_tlv_list x = {0}, y = {0};
    for (size_t i = 0; i < 7; i++) {
        size_t from = i < 4 ? i : before->count - 7 + i, to = i < 4 ? i : after->count - 7 + i;
        x.tlvs[x.count++] = before->tlvs[from];
        y.tlvs[y.count++] = after->tlvs[to];
    }
    return same_tlvs(&x, &y);
}

/* The radios with the maximum EIRP each advertised so far give the capabilities the agent has: a
 * change of its capabilities is then only a radio's power risen above it (spec 2.5). */
static bool power_rose(const em_capability_radio *before, size_t n, const em_tlv_list *caps)
{
    em_tlv_list old;
    if (em_capabilities_tlvs(before, n, &old) != EM_OK)
        return false;
    bool same = same_tlvs(&old, caps);
    em_tlv_list_free(&old);
    return same;
}

/* An AP Radio Basic Capabilities TLV's value apart from its operable channels (spec 2.5): its RUID,
 * maximum BSS and class count, and each class with its maximum EIRP, the non-operable channel
 * lists left out (RUID(6) max_bss(1) classes(1), per class: class(1) EIRP(1) n(1) channels(n)).
 * Its length, or 0 when the value is malformed or does not fit. */
static size_t basic_identity(const em_tlv *t, uint8_t *out, size_t size)
{
    const uint8_t *v = t->value;
    size_t off = 8, n = 8;
    if (t->len < 8 || size < 8)
        return 0;
    memcpy(out, v, 8);
    for (unsigned k = 0; k < v[7]; k++) {
        if (off + 3 > t->len || n + 2 > size)
            return 0;
        out[n++] = v[off];
        out[n++] = v[off + 1];
        off += 3 + (size_t)v[off + 2];
    }
    return off == t->len ? n : 0;
}

/* Two capability reports are the same apart from their radios' operable channels
 * (reports.capabilities_identity, spec 2.5). */
static bool same_identity(const em_tlv_list *x, const em_tlv_list *y)
{
    if (x->count != y->count)
        return false;
    for (size_t i = 0; i < x->count; i++) {
        const em_tlv *p = &x->tlvs[i], *q = &y->tlvs[i];
        if (p->kind != q->kind)
            return false;
        if (p->kind == 0x85) {
            uint8_t ip[256], iq[256];
            size_t np = basic_identity(p, ip, sizeof(ip)), nq = basic_identity(q, iq, sizeof(iq));
            if (!np || np != nq || memcmp(ip, iq, np))
                return false;
        } else if (p->len != q->len || memcmp(p->value, q->value, p->len)) {
            return false;
        }
    }
    return true;
}

/* The RUIDs of the radios whose AP Radio Basic Capabilities differ between two reports of the
 * same identity: their operable channel moved (reports.operable_channels_moved), how many. */
static size_t operable_moved(const em_tlv_list *before, const em_tlv_list *after, uint8_t out[][6], size_t max)
{
    size_t n = 0;
    for (size_t i = 0; i < before->count && i < after->count && n < max; i++) {
        const em_tlv *p = &before->tlvs[i], *q = &after->tlvs[i];
        if (p->kind == 0x85 && q->kind == 0x85 && p->len >= 6 &&
            (p->len != q->len || memcmp(p->value, q->value, p->len)))
            memcpy(out[n++], q->value, 6);
    }
    return n;
}

/* spec 2.5: a provisioned session kept while radios' operable channels moved (a further radio's, or
 * the fronthaul's): the radios that moved are due their Channel Preference and Operating Channel
 * Reports (report_channel_moves) */
static void channel_moves_kept(agent *a, const em_tlv_list *caps)
{
    uint8_t moved[1 + EXTRA_RADIOS][6];
    size_t n = operable_moved(&a->caps, caps, moved, 1 + EXTRA_RADIOS);
    for (size_t i = 0; i < n; i++) {
        bool known = false;
        for (size_t j = 0; j < a->nchannel_moves; j++)
            known = known || !memcmp(a->channel_moves[j], moved[i], 6);
        if (!known && a->nchannel_moves < 1 + EXTRA_RADIOS)
            memcpy(a->channel_moves[a->nchannel_moves++], moved[i], 6);
    }
    count(&a->counts, "channel_move_kept");
}

/* The radios the agent shows, the fronthaul first, then each further radio the pod has brought
 * up (spec 9), each with the BSSes the agent represents on it, on its channel. */
static size_t shown_radios(const agent *a, em_topology_radio out[1 + EXTRA_RADIOS])
{
    size_t n = 0;
    out[n++] = (em_topology_radio){&a->represented, a->channel};
    for (size_t k = 0; k < a->nradio_scopes; k++)
        if (a->radio_scopes[k].bound)
            out[n++] = (em_topology_radio){&a->radio_scopes[k].represented, a->radio_scopes[k].channel};
    return n;
}

/* The pod's 1905 neighbors on its Wi-Fi backhaul (spec §8.5): its parent pod's agent on its
 * backhaul station, then each child pod's agent on the backhaul BSS it is on (peers' order),
 * on any of the pod's radios (a GRE child on a parent's b-ap-50, spec 9). */
static void backhaul_links(agent *a, double t)
{
    a->nlinks = 0;
    if (!a->peers_on)
        return;
    em_peers_read(&a->peers, t);
    if (a->has_backhaul) {
        const em_peer *p = em_peers_upstream(&a->peers, a->backhaul.station.parent);
        if (p) {
            memcpy(a->links[0].local, a->backhaul.station.mac, 6);
            memcpy(a->links[0].al, p->al, 6);
            a->nlinks = 1;
        }
    }
    em_topology_radio shown[1 + EXTRA_RADIOS];
    size_t nshown = shown_radios(a, shown);
    for (size_t k = 0; k < a->peers.n && a->nlinks < 16; k++) {
        const em_peer *p = &a->peers.peers[k];
        const em_bss_view *b = NULL;
        for (size_t r = 0; p->has_station && !b && r < nshown; r++)
            for (size_t i = 0; !b && i < shown[r].radio->nbss; i++)
                if (shown[r].radio->bss[i].backhaul && !memcmp(shown[r].radio->bss[i].bssid, p->parent, 6))
                    b = &shown[r].radio->bss[i];
        bool on = false;
        for (size_t s = 0; b && s < b->nstations; s++)
            on = on || !memcmp(b->stations[s], p->station, 6);
        if (on) {
            memcpy(a->links[a->nlinks].local, b->bssid, 6);
            memcpy(a->links[a->nlinks].al, p->al, 6);
            a->nlinks++;
        }
    }
}

/* spec §8.5: an upstream whose chain of pods reaches this one (the uplink scope's loop hook) */
static bool upstream_loops(void *ctx, const uint8_t target[6], const uint8_t (*own)[6], size_t nown)
{
    agent *a = ctx;
    em_peers_read(&a->peers, now());
    return em_peers_loops(&a->peers, a->al, own, nown, target);
}

/* spec §8.6: the target is a GRE parent AP of the fleet (the uplink scope's parent hook) */
static bool upstream_is_parent(void *ctx, const uint8_t bssid[6])
{
    agent *a = ctx;
    em_peers_read(&a->peers, now());
    return em_peers_is_parent(&a->peers, bssid);
}

/* spec §8.3: whether the fleet pod whose AP bssid is has it up (the uplink scope's
 * parent-AP hook): a kept target on another fleet pod waits for it */
static int upstream_ap_state(void *ctx, const uint8_t bssid[6])
{
    agent *a = ctx;
    em_peers_read(&a->peers, now());
    return em_peers_ap_state(&a->peers, bssid);
}

static void close_session(agent *a, em_session_state end);

/* the pod's backhaul stations: the configured one (or the profile's) first, then the profile's
 * station on each band, each once */
static size_t station_names(const agent *a, const char *out[EM_UPLINK_BANDS + 1])
{
    size_t n = 0;
    const char *first = a->uplink.station ? a->uplink.station : a->profile.uplink_station;
    if (first)
        out[n++] = first;
    for (size_t i = 0; i < a->profile.nuplink_bands && n < EM_UPLINK_BANDS + 1; i++) {
        bool seen = false;
        for (size_t k = 0; k < n && !seen; k++)
            seen = !strcmp(out[k], a->profile.uplink_band_stations[i]);
        if (!seen)
            out[n++] = a->profile.uplink_band_stations[i];
    }
    return n;
}

/* spec 3.10: a legacy pod's new start (its radio rows come back from its vendor's seed with the same
 * UUIDs; its start marker tells the starts apart, else the agent's sessions do): a new ownership
 * period, and the writes made once per start written again where the pod lacks them, a re-claim
 * (pod.PodReportSource.new_start) */
static void new_start(agent *a, const char *why)
{
    if (!a->profile.legacy)
        return;
    if (!em_start_marker_used() && !em_new_session_start()) {
        WARN("a legacy pod's new start (%s): no randomness, the start kept", why);
        return;
    }
    a->session_starts++;
    LOG("a legacy pod's new start (%s): its configuration claimed again", why);
}

/* spec 3.10 (PodReportSource.mark_start): a legacy pod's start marker read, or written where the pod
 * has none (a re-seed's or a reboot's database, or the first EMOSA with it): once per START_RETRY s
 * in one database generation. A token other than the last one read is a new start; the first one
 * read is this agent's own start (an EMOSA restart reads the pod's back). Unmarked, the pod is not
 * ready for any scope: since when, and why the last write failed, are in the status
 * (start_marker.pending_since, last_error). */
#define START_RETRY 5.0
static void mark_start(agent *a, const cJSON *tables)
{
    if (!em_start_marker_used())
        return;
    if (em_ovsdb_node_config_schema(a->ovs) == 0) {
        em_use_start_marker(false); /* the agent's own token from now on (em_new_session_start) */
        WARN("the pod's schema has no Node_Config: its starts counted by sessions");
        return;
    }
    const char *token = em_start_marker(tables);
    if (token) {
        if (a->start_token[0] && strcmp(token, a->start_token))
            new_start(a, "a new start marker");
        EM_FORMAT_FIXED(a->start_token, sizeof(a->start_token), "%.16s", token);
        a->start_pending_since = 0;
        a->start_error[0] = '\0'; /* marked: why an earlier write failed no longer holds the pod */
        return;
    }
    if (a->start_pending_since <= 0)
        a->start_pending_since = wall();
    int generation = em_ovsdb_generation(a->ovs);
    double t = now();
    if (a->start_tried_at > 0 && a->start_tried_generation == generation && t - a->start_tried_at < START_RETRY)
        return;
    a->start_tried_generation = generation;
    a->start_tried_at = t;
    uint8_t raw[8];
    if (RAND_bytes(raw, sizeof(raw)) != 1) {
        EM_FORMAT_FIXED(a->start_error, sizeof(a->start_error), "%s", "no randomness");
        WARN("the pod's start marker not written: no randomness");
        return;
    }
    char *hex = em_hex(raw, sizeof(raw));
    cJSON *ops = em_start_marker_ops(hex);
    free(hex);
    const int counts[2] = {-1, -1};
    cJSON *results = transact(a, ops);
    em_reason r = results ? em_check_results(results, counts, 2) : EM_OUTCOME_UNKNOWN;
    if (r != EM_OK)
        em_start_marker_error(r, results, a->start_error, sizeof(a->start_error));
    cJSON_Delete(results);
    cJSON_Delete(ops);
    if (r != EM_OK) {
        WARN("the pod's start marker not written: %s", a->start_error);
        return;
    }
    a->start_writes++;
    LOG("the pod's start marked (spec 3.10): its database had no start marker");
}

/* spec 2.5: a fresh session's log line, with what differs when the capabilities changed (the
 * first radio field or TLV of their identity, against the source's) */
static void fresh_log(const agent *a, const char *why, const em_tlv_list *caps)
{
    char what[160];
    if (strcmp(why, "capabilities_changed") || !*em_identity_difference(&a->caps, caps, what, sizeof(what)))
        LOG("a new pod source (%s): a fresh session", why);
    else
        LOG("a new pod source (%s: %s): a fresh session", why, what);
}

/* Why a new database generation of a pod source already taken is another source (spec 2.5), or
 * NULL: it continues the last one (pod._fresh_reason). Only the link to the pod comes back in a
 * continuation: the same pod, the same start of its OpenSync (its database and the configuration
 * in it; a restart's comes from its template, and the engine writes only for an M2), within the
 * window, with the same capabilities apart from their operable channels. */
static const char *fresh_reason(const agent *a, const em_device_view *view, const cJSON *tables,
                                const em_tlv_list *caps)
{
    char instance[17];
    if (!a->has_source || strcmp(view->serial, a->source_serial) ||
        strcmp(view->has_node_id ? view->node_id : "", a->source_node))
        return "another_pod";
    if (!em_start_instance(tables, instance) || strcmp(instance, a->source_instance))
        return "pod_restarted";
    if (now() - a->refreshed_at > RECONNECT_WINDOW)
        return "window_passed";
    if (!same_identity(caps, &a->caps))
        return "capabilities_changed";
    return NULL;
}

static bool ap_known(const agent *a, const uint8_t bssid[6])
{
    for (size_t i = 0; i < a->naps_known; i++)
        if (!memcmp(a->aps_known[i], bssid, 6))
            return true;
    return false;
}

static void aps_path(const agent *a, char *out, size_t n)
{
    EM_FORMAT_FIXED(out, n, "%s/aps.json", a->state_dir); /* EM_STATE_DIR_MAX */
}

/* spec 8.3: the pod's APs seen on this refresh among those known; a new one kept in aps.json */
static void note_aps(agent *a)
{
    bool grew = false;
    for (size_t r = 0; r < a->view.nradios; r++)
        for (size_t b = 0; b < a->view.radios[r].nbss; b++) {
            const uint8_t *bssid = a->view.radios[r].bss[b].bssid;
            if (!ap_known(a, bssid) && a->naps_known < EM_PEER_APS) {
                memcpy(a->aps_known[a->naps_known++], bssid, 6);
                grew = true;
            }
        }
    if (!grew)
        return;
    cJSON *list = cJSON_CreateArray();
    for (size_t i = 0; i < a->naps_known; i++) {
        char *text = em_mac_str(a->aps_known[i]);
        cJSON_AddItemToArray(list, cJSON_CreateString(text));
        free(text);
    }
    char path[700], *body = em_json_dumps(list, EM_JSON_DEFAULT, false);
    cJSON_Delete(list);
    aps_path(a, path, sizeof(path));
    if (!body || !em_write_file(path, body, false))
        WARN("the pod's known APs not kept in %s", path);
    free(body);
}

/* the APs aps.json keeps (a file on the disk: an entry that is not a MAC is not an AP) */
static void load_aps(agent *a)
{
    char path[700];
    aps_path(a, path, sizeof(path));
    char *body = em_read_file(path, 1 << 16, NULL);
    cJSON *list = body ? cJSON_Parse(body) : NULL, *x;
    free(body);
    if (cJSON_IsArray(list)) {
        cJSON_ArrayForEach(x, list)
        {
            uint8_t mac[6];
            if (cJSON_IsString(x) && em_parse_mac(x->valuestring, mac) && !ap_known(a, mac) &&
                a->naps_known < EM_PEER_APS)
                memcpy(a->aps_known[a->naps_known++], mac, 6);
        }
    }
    cJSON_Delete(list);
}

static int by_text(const void *x, const void *y) { return strcmp(*(char *const *)x, *(char *const *)y); }

static bool source_current(agent *a);

/* spec 8.3: each AP of the pod known (and its GRE parent APs, gre_aps from its status), true while
 * enabled in the pod's Wifi_VIF_State on its current start with the report source live */
static cJSON *ap_up_status(agent *a, const cJSON *gre_aps)
{
    char *names[EM_PEER_APS * 2];
    size_t n = 0;
    for (size_t i = 0; i < a->naps_known; i++)
        names[n++] = em_mac_str(a->aps_known[i]);
    /* its GRE parent APs: one BSSID, or a list */
    uint8_t mac[6];
    if (cJSON_IsString(gre_aps) && em_parse_mac(gre_aps->valuestring, mac) && !ap_known(a, mac))
        names[n++] = em_mac_str(mac);
    if (cJSON_IsArray(gre_aps)) {
        const cJSON *x;
        cJSON_ArrayForEach(x, gre_aps)
            if (n < EM_PEER_APS * 2 && cJSON_IsString(x) && em_parse_mac(x->valuestring, mac) && !ap_known(a, mac))
                names[n++] = em_mac_str(mac);
    }
    qsort(names, n, sizeof(names[0]), by_text);
    bool live = source_current(a);
    cJSON *o = cJSON_CreateObject();
    for (size_t i = 0; i < n; i++) {
        bool up = false;
        for (size_t r = 0; live && em_parse_mac(names[i], mac) && r < a->view.nradios; r++)
            for (size_t b = 0; b < a->view.radios[r].nbss; b++)
                up = up || !memcmp(a->view.radios[r].bss[b].bssid, mac, 6);
        if (!cJSON_GetObjectItemCaseSensitive(o, names[i]))
            cJSON_AddBoolToObject(o, names[i], up);
        free(names[i]);
    }
    return o;
}

static bool refresh(agent *a)
{
    const cJSON *tables = em_ovsdb_tables(a->ovs), *row;
    if (!em_ovsdb_ready(a->ovs))
        return false;
    /* spec 3.10: a pod whose schema is not the profile's generation is refused, and said so */
    int legacy = em_ovsdb_legacy_schema(a->ovs);
    bool refused = legacy >= 0 && (legacy == 1) != a->profile.legacy;
    if (refused != a->pod_refused && refused)
        WARN("pod refused: its schema is not the profile's generation (%s)",
             a->profile.legacy ? "opensync-2.0" : "opensync-6.6");
    a->pod_refused = refused;
    if (refused)
        return false;
    if (a->profile.legacy)
        mark_start(a, tables); /* spec 3.10: before the scopes read the pod */
    /* the bound radio: the profile's band in Config, its State with a MAC */
    const char *radio_uuid = NULL;
    cJSON_ArrayForEach(row, cJSON_GetObjectItemCaseSensitive(tables, "Wifi_Radio_Config"))
    {
        const char *band = ovs_str(row, "freq_band");
        if (band && !strcmp(band, a->profile.band))
            radio_uuid = row->string;
    }
    const char *radio_mac = NULL;
    cJSON_ArrayForEach(row, cJSON_GetObjectItemCaseSensitive(tables, "Wifi_Radio_State"))
    {
        const char *ref = ovs_str(row, "radio_config");
        if (radio_uuid && ref && !strcmp(ref, radio_uuid))
            radio_mac = ovs_str(row, "mac");
    }
    em_device_view *view = em_malloc(sizeof(*view));
    uint8_t ruid[6];
    if (!radio_mac || !em_parse_mac(radio_mac, ruid) || em_device_view_from_rows(tables, view) != EM_OK) {
        free(view);
        return false;
    }
    for (size_t i = 0; a->multi_bss && i < a->profile.nslots; i++) /* a GRE parent's AP (spec 8.6) */
        if (em_profile_parent_slot(&a->profile, a->profile.slots[i].if_name))
            em_device_view_parent_ap(view, a->profile.slots[i].if_name);
    const em_radio_view *radio = em_view_radio(view, ruid);
    if (!radio) {
        free(view);
        return false;
    }
    /* the represented BSSes: the bound fronthaul, then managed slots */
    em_radio_view rep = *radio;
    rep.nbss = 0;
    bool primary = false;
    for (size_t i = 0; i < radio->nbss; i++) {
        const em_bss_view *b = &radio->bss[i];
        if (!strcmp(b->if_name, a->profile.fronthaul_if)) {
            const cJSON *st = vif_state_row(a, b->if_name);
            if (radio->enabled && st && em_row_wpa2_psk(st) && radio->has_channel) {
                rep.bss[rep.nbss++] = *b;
                primary = true;
            }
        }
    }
    for (size_t k = 0; a->multi_bss && k < a->profile.nslots; k++)
        for (size_t i = 0; i < radio->nbss; i++)
            if (!strcmp(radio->bss[i].if_name, a->profile.slots[k].if_name))
                rep.bss[rep.nbss++] = radio->bss[i];
    /* the capabilities are fixed for one pod source (spec 2.4): a new OVSDB generation (the
     * pod recreated, maybe with another radio) is a new source with a new session, and its
     * capabilities are taken again */
    int source = em_ovsdb_generation(a->ovs);
    bool fresh = !a->caps_fixed || a->caps_generation != source;
    /* a cold pod has the radio but no BSS yet: the radio on the channel the profile would create
     * the BSS on. Spec 9: a fronthaul already reported whose BSS is not up for a moment (its radio
     * between two channels, as a station it shares the radio with roams) stays on the channel it
     * was reported on, so that a move from one channel to another changes its capabilities once */
    int channel = primary ? radio->channel : !fresh ? a->channel : a->profile.channel;
    /* spec 9 (bound_radios): the fronthaul, then each of the profile's further radios the pod has
     * brought up, on its observed channel; the fronthaul carries one BSS plus its radio's slots, a
     * further radio its own slots and at least one; each its own Operational/Configured radio,
     * with the managed slots on it */
    em_capability_radio reported[1 + EXTRA_RADIOS];
    size_t nreported = 0;
    uint8_t front_bss = (uint8_t)(1 + em_profile_slots_on(&a->profile, a->profile.band, a->multi_bss).nslots);
    reported[nreported++] = (em_capability_radio){radio, channel, front_bss, 0};
    for (size_t k = 0; k < a->nradio_scopes; k++) {
        radio_scope *rs = &a->radio_scopes[k];
        const em_radio_view *other = NULL;
        for (size_t i = 0; i < view->nradios && !other; i++)
            if (!strcmp(view->radios[i].band, rs->band) && memcmp(view->radios[i].ruid, radio->ruid, 6))
                other = &view->radios[i];
        /* between two channels for a moment (its station roaming), a radio already reported stays
         * on its channel, so that a move changes its capabilities once (bound_radios' held) */
        bool held = other && !other->has_channel && !fresh && rs->bound;
        int on = held ? rs->channel : other && other->has_channel ? other->channel : 0;
        /* a radio the profile places (its declared channel, a fronthaul with no station): EMOSA
         * writes that channel with the radio's slots, and reports it from the start */
        for (size_t pr = 0; other && pr < a->profile.nradios; pr++)
            if (!strcmp(a->profile.radio_bands[pr], rs->band) && a->profile.radio_channels[pr])
                on = a->profile.radio_channels[pr];
        rs->bound = other && on && em_operating_class(rs->band, on);
        if (!rs->bound)
            continue; /* not brought up yet, or on a channel not mapped (DFS): reported once it is */
        rs->represented = *other;
        rs->represented.channel = on;
        rs->represented.has_channel = true;
        rs->represented.nbss = 0;
        for (size_t s = 0; a->multi_bss && s < a->profile.nslots; s++)
            for (size_t i = 0; i < other->nbss; i++)
                if (!strcmp(other->bss[i].if_name, a->profile.slots[s].if_name))
                    rs->represented.bss[rs->represented.nbss++] = other->bss[i];
        rs->channel = on;
        rs->caps_index = nreported;
        size_t own = em_profile_slots_on(&a->profile, rs->band, a->multi_bss).nslots;
        reported[nreported++] = (em_capability_radio){&rs->represented, on, (uint8_t)(own ? own : 1), 0};
    }
    em_capability_radio before[1 + EXTRA_RADIOS]; /* the same radios as advertised so far */
    /* a new generation of a source already taken is compared with the maxima it advertised
     * (spec 2.5): a continuation keeps them */
    bool new_source = fresh && a->caps_fixed;
    for (size_t k = 0; k < nreported; k++) {
        /* its maximum EIRP: the source's own, raised by a higher reported power (spec 2.5) */
        const em_radio_view *r = reported[k].radio;
        bool has_kept = false;
        int8_t kept = 0;
        for (size_t i = 0; a->caps_fixed && i < a->nreported; i++)
            if (!memcmp(a->reported[i].ruid, r->ruid, 6)) {
                has_kept = true;
                kept = a->reported[i].max_eirp;
            }
        reported[k].max_eirp = em_advertised_eirp(has_kept, kept, r->has_tx_power, r->tx_power);
        before[k] = reported[k];
        if (has_kept)
            before[k].max_eirp = kept;
    }
    em_tlv_list caps;
    if (em_capabilities_tlvs(reported, nreported, &caps) != EM_OK) {
        free(view);
        return false;
    }
    if (new_source) {
        const char *why = fresh_reason(a, view, tables, &caps);
        if (!why) { /* the same pod source: the session goes on (a provisioned one is kept) */
            fresh = false;
            a->caps_generation = source;
            a->reconnections_continued++;
            em_attempts_continue(&a->at, source);
        } else { /* another source: its own maxima, a fresh session */
            count(&a->reconnections_fresh, why);
            fresh_log(a, why, &caps);
            if (!em_start_marker_used()) /* (with the marker: the same start, its own) */
                new_start(a, "a fresh session");
            em_tlv_list_free(&caps);
            for (size_t k = 0; k < nreported; k++) {
                const em_radio_view *r = reported[k].radio;
                reported[k].max_eirp = em_advertised_eirp(false, 0, r->has_tx_power, r->tx_power);
                before[k] = reported[k];
            }
            if (em_capabilities_tlvs(reported, nreported, &caps) != EM_OK) {
                free(view);
                return false;
            }
        }
    }
    int8_t max_eirp = reported[0].max_eirp;
    if (fresh) {
        if (a->caps_fixed) { /* the previous source's */
            if (!same_tlvs(&caps, &a->caps))
                LOG("a new pod source with other radio capabilities: taken for it");
            em_tlv_list_free(&a->caps);
            free(a->inventory.value);
            a->inventory = (em_tlv){0};
        }
        a->caps = caps;
        a->max_eirp = max_eirp;
        em_inventory_tlv(view, radio, "mac80211_hwsim", &a->inventory);
        a->caps_fixed = true;
        a->caps_generation = source;
    } else if (same_tlvs(&caps, &a->caps)) {
        em_tlv_list_free(&caps);
    } else if (further_radios_changed(&a->caps, &caps)) {
        /* spec 9: a further radio's channel follows the pod's station (and the radio appears once
         * the pod brings it up): its capabilities are taken again. A channel move keeps a
         * provisioned session (spec 2.5: its new operable channel reported); a radio that came or
         * went, or a session not provisioned yet, starts afresh with new M1s. */
        LOG("a further radio moved channel or came and went: capabilities taken");
        bool keeps = a->at.state == S_PROVISIONING && a->at.window > 0 && same_identity(&a->caps, &caps);
        if (keeps)
            channel_moves_kept(a, &caps);
        em_tlv_list_free(&a->caps);
        a->caps = caps;
        if (!keeps && a->at.state != S_NONE && a->at.state != S_FAILED && a->at.state != S_SOURCE_LOST &&
            a->at.state != S_INCOMPATIBLE)
            close_session(a, S_SOURCE_LOST);
    } else if (power_rose(before, nreported, &a->caps)) {
        /* spec 2.5: a radio reports more power than the maximum EIRP it advertised (the source began
         * before the pod reported any): its capabilities are taken again with the higher maximum,
         * and the session that has the old ones ends, the next starting afresh */
        for (size_t k = 0; k < nreported; k++)
            if (reported[k].max_eirp != before[k].max_eirp) {
                char *mac = em_mac_str(reported[k].radio->ruid);
                LOG("a radio reports more power than it advertised: capabilities taken (%s %d -> %d dBm)",
                    mac ? mac : "?", before[k].max_eirp, reported[k].max_eirp);
                free(mac);
            }
        em_tlv_list_free(&a->caps);
        a->caps = caps;
        a->max_eirp = max_eirp;
        if (a->at.state != S_NONE && a->at.state != S_FAILED && a->at.state != S_SOURCE_LOST &&
            a->at.state != S_INCOMPATIBLE)
            close_session(a, S_SOURCE_LOST);
    } else if (a->at.state == S_PROVISIONING && a->at.window > 0 && same_identity(&a->caps, &caps)) {
        /* spec 2.5, 9: the fronthaul radio's channel moved (the station it shares the radio with
         * follows its parent, as under a gateway's automatic channel selection): a provisioned
         * session is kept, as for a further radio, its new operable channel reported */
        LOG("the fronthaul radio moved channel: capabilities taken");
        channel_moves_kept(a, &caps);
        em_tlv_list_free(&a->caps);
        a->caps = caps;
    } else {
        /* spec 2.5: the fronthaul radio's channel moved under a session not provisioned yet, or
         * another radio is in its place: another pod source, a fresh session with these
         * capabilities, never a refusal (that would make a channel move an outage) */
        const char *why = same_identity(&a->caps, &caps) ? "fronthaul_moved" : "capabilities_changed";
        count(&a->reconnections_fresh, why);
        fresh_log(a, why, &caps);
        if (!em_start_marker_used())
            new_start(a, "a fresh session");
        em_tlv_list_free(&a->caps);
        a->caps = caps;
        a->max_eirp = max_eirp;
        free(a->inventory.value);
        a->inventory = (em_tlv){0};
        em_inventory_tlv(view, radio, "mac80211_hwsim", &a->inventory);
        if (a->at.state != S_NONE && a->at.state != S_FAILED && a->at.state != S_SOURCE_LOST &&
            a->at.state != S_INCOMPATIBLE)
            close_session(a, S_SOURCE_LOST);
    }
    a->nreported = nreported; /* each radio's maximum EIRP, as its capabilities advertise it */
    for (size_t k = 0; k < nreported; k++) {
        memcpy(a->reported[k].ruid, reported[k].radio->ruid, 6);
        a->reported[k].max_eirp = reported[k].max_eirp;
    }
    /* station ages: since EMOSA first saw each one, on any radio's BSSes the agent shows */
    double t = now();
    size_t kept = 0;
    struct first_seen seen[256];
    for (size_t r = 0; r <= a->nradio_scopes; r++) {
        const em_radio_view *shown = !r ? &rep : a->radio_scopes[r - 1].bound ? &a->radio_scopes[r - 1].represented : NULL;
        for (size_t i = 0; shown && i < shown->nbss; i++)
            for (size_t k = 0; k < shown->bss[i].nstations && kept < 256; k++) {
                double at = t;
                for (size_t j = 0; j < a->nfirst; j++)
                    if (!memcmp(a->first_seen[j].mac, shown->bss[i].stations[k], 6))
                        at = a->first_seen[j].at;
                memcpy(seen[kept].mac, shown->bss[i].stations[k], 6);
                seen[kept++].at = at;
            }
    }
    memcpy(a->first_seen, seen, kept * sizeof(seen[0]));
    a->nfirst = kept;
    /* the pod's backhaul stations: the one cm uses as a Multi-AP uplink is its EasyMesh
     * backhaul (option 1), reported; each is a backhaul STA radio of the pod (spec §8.3) */
    const char *names[EM_UPLINK_BANDS + 1], *in_use = NULL;
    size_t nnames = station_names(a, names);
    a->has_backhaul = false;
    /* a wireless backhaul station: a Multi-AP (4-address) uplink, or a 3-address station under a
     * GRE tunnel (a GRE child, or the bootstrap/GTP path). The medium is 802.11 either way, so the
     * pod reports it as its Wi-Fi backhaul (spec 8.6). Under a GRE tunnel the station whose own
     * gretap cm uses (cm's g-<station>) comes first: after a GRE move the previous station may
     * still be associated (until make-before-break), and it is not the pod's backhaul. */
    for (int strict = 1; strict >= 0 && !a->has_backhaul; strict--) {
        for (size_t i = 0; i < nnames && !a->has_backhaul; i++) {
            em_uplink_state state;
            em_uplink_state_of(tables, names[i], &state);
            char gretap[40];
            bool own = em_format(gretap, sizeof(gretap), "g-%s", names[i]) && !strcmp(state.in_use, gretap);
            if ((!strcmp(state.kind, "multi-ap") || (!strcmp(state.kind, "gre") && (own || !strict))) &&
                em_view_backhaul(view, names[i], &a->backhaul)) {
                a->has_backhaul = true;
                in_use = names[i];
            }
        }
    }
    a->nradios = em_backhaul_radios(tables, names, nnames, in_use, a->radios, EM_UPLINK_BANDS + 1);
    /* radio points into *view: its fields are read before view goes */
    a->has_observed = radio->has_channel;
    a->observed_channel = radio->has_channel ? radio->channel : 0;
    a->tx_power = radio->has_tx_power ? radio->tx_power : 0;
    a->view = *view;
    free(view);
    note_aps(a);
    a->represented = rep;
    a->has_primary = primary;
    backhaul_links(a, t);
    a->channel = channel;
    a->generation = em_ovsdb_generation(a->ovs);
    a->revision = em_ovsdb_revision(a->ovs);
    a->refreshed_at = t;
    a->available = true;
    /* the pod source as of this refresh (spec 2.5: what a new generation must match to continue) */
    EM_FORMAT_FIXED(a->source_serial, sizeof(a->source_serial), "%s", a->view.serial);
    EM_FORMAT_FIXED(a->source_node, sizeof(a->source_node), "%s", a->view.has_node_id ? a->view.node_id : "");
    if (!em_start_instance(tables, a->source_instance))
        a->source_instance[0] = '\0';
    a->has_source = true;
    return true;
}

static bool source_current(agent *a) { return a->available && now() < a->refreshed_at + LEASE; }

static em_reason topology_tlvs(agent *a, bool r1, em_tlv_list *out)
{
    em_station_age ages[256];
    double t = now();
    for (size_t i = 0; i < a->nfirst; i++) {
        memcpy(ages[i].mac, a->first_seen[i].mac, 6);
        ages[i].seconds = em_age_at(t, a->first_seen[i].at); /* as of this response */
    }
    em_topology_radio shown[1 + EXTRA_RADIOS];
    size_t nshown = shown_radios(a, shown);
    return em_topology_radios_tlvs(a->al, a->controller, shown, nshown, ages, a->nfirst, r1,
                                   a->has_backhaul ? &a->backhaul : NULL, a->links, a->nlinks, out);
}

/* -- operations (the AP scope: emosa.reconcile.Engine over PodBackend) ----------------- */

/* the pod has an operation in an active state (the fronthaul write in flight) */
static bool active_op(agent *a)
{
    const cJSON *op;
    bool active = false;
    cJSON_ArrayForEach(op, em_journal_operations_view(a->journal)) active = active || em_state_active(em_state_of(op));
    return active;
}

/* a write of the controller's configuration in flight on any of the pod's radios (spec 9) */
static bool writing(agent *a)
{
    bool active = active_op(a);
    for (size_t k = 0; k < a->nradio_scopes && !active; k++) {
        const cJSON *op;
        cJSON_ArrayForEach(op, em_journal_operations_view(a->radio_scopes[k].journal)) active =
            active || em_state_active(em_state_of(op));
    }
    return active;
}

/* spec 3.10 (PodBackend.auth_reload): each AP's write wm has taken, its oftag back, guarded (the
 * second full reload of the pod's authenticator) */
static void auth_reload(agent *a)
{
    char names[8][16];
    size_t n = 0;
    cJSON *ops = em_ovsdb_ready(a->ovs)
                     ? em_auth_reload_ops(&a->profile, a->multi_bss, em_ovsdb_tables(a->ovs), names, 8, &n)
                     : NULL;
    if (!ops)
        return;
    int counts[16];
    for (size_t i = 0; i < n; i++) {
        counts[2 * i] = -1;
        counts[2 * i + 1] = 1;
    }
    cJSON *results = transact(a, ops);
    em_reason r = results ? em_check_results(results, counts, 2 * n) : EM_OUTCOME_UNKNOWN;
    if (r == EM_OK) {
        a->auth_reloads += (unsigned)n;
        for (size_t i = 0; i < n; i++)
            LOG("auth reload: %s oftag restored (wm restarts nas, eapd)", names[i]);
    } else {
        WARN("auth reload: %s", em_reason_name(r));
    }
    cJSON_Delete(results);
    cJSON_Delete(ops);
}

/* spec 3.10 (PodBackend.bridge_ports): each AP of EMOSA's that wm has taken and that is not in its
 * bridge, its Interface and Port into it and its oftag marked (a full reload with the port there) */
static void bridge_port(agent *a)
{
    char names[EM_BRIDGE_PORTS_MAX][16];
    int counts[EM_BRIDGE_PORTS_MAX * 7];
    size_t n = 0, ncounts = 0;
    cJSON *ops = em_ovsdb_ready(a->ovs) ? em_bridge_port_ops(&a->profile, a->multi_bss, em_ovsdb_tables(a->ovs),
                                                             names, EM_BRIDGE_PORTS_MAX, &n, counts, &ncounts)
                                        : NULL;
    if (!ops)
        return;
    cJSON *results = transact(a, ops);
    em_reason r = results ? em_check_results(results, counts, ncounts) : EM_OUTCOME_UNKNOWN;
    if (r == EM_OK) {
        a->bridge_ports += (unsigned)n;
        for (size_t i = 0; i < n; i++)
            LOG("bridge port: %s into its bridge (wm restarts nas, eapd)", names[i]);
    } else {
        WARN("bridge port: %s", em_reason_name(r));
    }
    cJSON_Delete(results);
    cJSON_Delete(ops);
}

/* spec 3.10 (PodBackend.bridge_mode): a converted pod's bridge mode as its cloud runs it, when it
 * lacks any of it, in one guarded write (the home APs' oftags marked: a full reload after) */
static void bridge_mode(agent *a)
{
    char done[256];
    int counts[EM_BRIDGE_MODE_COUNTS];
    size_t ncounts = 0;
    cJSON *ops = em_ovsdb_ready(a->ovs) ? em_bridge_mode_ops(&a->profile, a->multi_bss, em_ovsdb_tables(a->ovs), done,
                                                             sizeof(done), counts, &ncounts)
                                        : NULL;
    if (!ops)
        return;
    cJSON *results = transact(a, ops);
    em_reason r = results ? em_check_results(results, counts, ncounts) : EM_OUTCOME_UNKNOWN;
    if (r == EM_OK) {
        a->bridge_modes++;
        LOG("bridge mode: %s (wm restarts nas, eapd)", done);
    } else {
        WARN("bridge mode: %s", em_reason_name(r));
    }
    cJSON_Delete(results);
    cJSON_Delete(ops);
}

/* spec 3.10 (PodBackend.phy_netdev): a tap Inet row for each radio carrying a home AP whose VIFs are
 * all EMOSA's, with no VIF or Inet row on its own netdev, so nm sets that netdev up; with the
 * fleet's phy_netdev_up false, those rows removed */
static void phy_netdev(agent *a)
{
    char done[160];
    int counts[EM_PHY_NETDEV_COUNTS];
    size_t ncounts = 0;
    cJSON *ops = em_ovsdb_ready(a->ovs) ? em_phy_netdev_ops(&a->profile, a->multi_bss, em_ovsdb_tables(a->ovs),
                                                            a->phy_netdev_up, done, sizeof(done), counts, &ncounts)
                                        : NULL;
    if (!ops)
        return;
    cJSON *results = transact(a, ops);
    em_reason r = results ? em_check_results(results, counts, ncounts) : EM_OUTCOME_UNKNOWN;
    if (r == EM_OK) {
        a->phy_writes++;
        LOG("phy netdev: %s (nm sets the link)", done);
    } else {
        WARN("phy netdev: %s", em_reason_name(r));
    }
    cJSON_Delete(results);
    cJSON_Delete(ops);
}

static void renew(agent *a, const char *why);

static void reconcile(agent *a)
{
    /* spec 2.5: a kept reconnection whose pod no longer has the configuration (its rows missing
     * or changed under the same start of its OpenSync): the session ends at once, and the fresh
     * session's M2 writes it, rather than the unserved rule's 60 s */
    if (a->check_drift && source_current(a)) {
        a->check_drift = false;
        bool drifted = em_journal_count(a->journal) && em_engine_drifted(&a->ap_engine);
        for (size_t k = 0; k < a->nradio_scopes && !drifted; k++)
            drifted = em_journal_count(a->radio_scopes[k].journal) && em_engine_drifted(&a->radio_scopes[k].engine);
        if (drifted) {
            a->reconnections_config_lost++;
            /* spec 3.10: a re-seed within the window (with the start marker, its own new start;
             * the configuration lost under the same one is not) */
            if (!em_start_marker_used())
                new_start(a, "its configuration lost");
            renew(a, "the pod's configuration is gone after its reconnection");
        }
    }
    if (em_journal_count(a->journal))
        em_engine_reconcile(&a->ap_engine);
    for (size_t k = 0; k < a->nradio_scopes; k++) /* each further radio's own scope (spec 9) */
        if (em_journal_count(a->radio_scopes[k].journal))
            em_engine_reconcile(&a->radio_scopes[k].engine);
}

/* the WSC exchange's authority: the pod's scope context is the one M1 was sent in */
static bool wsc_bound(void *ctx)
{
    agent *a = ctx;
    int generation;
    char schema[65], token[65];
    return a->context_live && em_ap_context(&a->ap, &generation, schema, token) &&
           generation == a->context_generation && !strcmp(schema, a->context_schema) &&
           !strcmp(token, a->context_token);
}

/* One authenticated M2 set into one durable operation (emosa.wire.operation_bridge):
 * its credentials into the secret store, the operation with its WSC receipt into the
 * journal, then the engine's guarded transaction. */
static const char *operate(agent *a, const em_message *m, const em_m2_result *m2)
{
    if (a->op_of_session[0]) { /* the same exchange again: its operation stands */
        cJSON_Delete(em_engine_execute(&a->ap_engine, a->op_of_session));
        return "wsc_operation";
    }
    if (!wsc_bound(a)) {
        a->context_live = false; /* the exchange is closed: a fresh attempt is needed */
        return "wsc_rejected";
    }
    size_t primary = 0;
    while (primary < m2->count && strcmp(m2->bss[primary].role, "fronthaul"))
        primary++;
    if (primary == m2->count)
        return "wsc_rejected";
    char ref[64], refs[8][64];
    size_t nrefs = 0;
    EM_FORMAT_FIXED(ref, sizeof(ref), "wsc-%s", a->exchange_id);
    if (em_vault_persist_received(&a->vault, ref, m2->bss[primary].passphrase) != EM_OK)
        return "wsc_rejected";
    EM_FORMAT_FIXED(refs[nrefs++], sizeof(refs[0]), "%s", ref);
    cJSON *additional = a->multi_bss ? cJSON_CreateArray() : NULL;
    /* each role fills the profile's slots of that role on this radio in the order received; the
     * rest are left out, their passphrases not stored (spec §3.4; a slot on a further radio is
     * that radio's own M2's, spec 9) */
    const em_profile own = em_profile_slots_on(&a->profile, a->profile.band, true);
    size_t free_fronthaul = 0, free_backhaul = 0;
    for (size_t k = 0; k < own.nslots; k++) {
        if (!strcmp(own.slots[k].role, "fronthaul"))
            free_fronthaul++;
        else if (!strcmp(own.slots[k].role, "backhaul"))
            free_backhaul++;
    }
    for (size_t i = 0, index = 1; i < m2->count && a->multi_bss; i++) {
        if (i == primary || nrefs == 8)
            continue;
        size_t *free_slots = !strcmp(m2->bss[i].role, "fronthaul") ? &free_fronthaul : &free_backhaul;
        if (*free_slots == 0)
            continue; /* left out: no slot of its role */
        (*free_slots)--;
        EM_FORMAT_FIXED(refs[nrefs], sizeof(refs[0]), "wsc-%s-%u", a->exchange_id, (unsigned)(index++ & 7));
        if (em_vault_persist_received(&a->vault, refs[nrefs], m2->bss[i].passphrase) != EM_OK)
            break;
        cJSON *b = cJSON_CreateObject();
        cJSON_AddStringToObject(b, "role", m2->bss[i].role);
        cJSON_AddStringToObject(b, "ssid", m2->bss[i].ssid);
        cJSON_AddStringToObject(b, "secret_ref", refs[nrefs++]);
        cJSON_AddItemToArray(additional, b);
    }
    cJSON *intent = em_ap_intent_record(a->pod_id, m2->bss[primary].ssid, ref, additional);
    em_reason why = EM_OK;
    cJSON *plan = em_ap_backend().plan(&a->ap, intent, &why), *op = NULL;
    if (plan && wsc_bound(a)) {
        /* the request fingerprint: the M2 set's WSC TLVs (keyed, private) */
        cJSON *wsc = cJSON_CreateArray();
        for (size_t i = 0; i < m->ntlvs; i++)
            if (m->tlvs[i].kind == 0x11) {
                char *hex = em_hex(m->tlvs[i].value, m->tlvs[i].len);
                cJSON_AddItemToArray(wsc, cJSON_CreateString(hex));
                free(hex);
            }
        char request_fp[65], *controller = em_hex(a->controller, 6), *local = em_hex(a->al, 6);
        char *ruid = em_hex(a->represented.ruid, 6), bssid_hex[13] = "";
        em_vault_fingerprint(&a->vault, wsc, request_fp);
        cJSON_Delete(wsc);
        uint8_t bssid[6];
        if (a->ap.has_bssid && em_parse_mac(a->ap.bssid, bssid)) {
            char *h = em_hex(bssid, 6);
            EM_FORMAT_FIXED(bssid_hex, sizeof(bssid_hex), "%s", h);
            free(h);
        } else {
            EM_FORMAT_FIXED(bssid_hex, sizeof(bssid_hex), "%s", ruid); /* a cold pod: the radio MAC */
        }
        cJSON *receipt = cJSON_CreateObject();
        cJSON_AddStringToObject(receipt, "scope", "owned_simulation_wsc_component");
        cJSON_AddStringToObject(receipt, "process_id", em_journal_process_id(a->journal));
        cJSON_AddStringToObject(receipt, "exchange_id", a->exchange_id);
        cJSON_AddStringToObject(receipt, "m1_sha256", a->m1_sha256);
        cJSON_AddStringToObject(receipt, "request_fingerprint", request_fp);
        cJSON_AddStringToObject(receipt, "controller_al", controller);
        cJSON_AddStringToObject(receipt, "agent_al", local);
        cJSON_AddStringToObject(receipt, "ruid", ruid);
        cJSON_AddStringToObject(receipt, "bssid", bssid_hex);
        cJSON_AddStringToObject(receipt, "ingress", a->interface);
        cJSON_AddNumberToObject(receipt, "link_generation", 1);
        cJSON_AddNumberToObject(receipt, "database_generation", a->context_generation);
        cJSON_AddStringToObject(receipt, "schema_fingerprint", a->context_schema);
        cJSON_AddNumberToObject(receipt, "first_mid", m->mid);
        cJSON_AddNumberToObject(receipt, "bss_count", (double)nrefs);
        cJSON_AddStringToObject(receipt, "pod_id", a->pod_id);
        cJSON_AddStringToObject(receipt, "radio_id", "radio-1");
        cJSON_AddStringToObject(receipt, "bss_id", "bss-1");
        char source[64];
        EM_FORMAT_FIXED(source, sizeof(source), "wsc-component:%s", controller);
        op = em_engine_request(&a->ap_engine, intent, source, a->exchange_id, a->run_id, AP_DEADLINE,
                               "wsc-component", receipt, &why);
        cJSON_Delete(receipt);
        free(controller);
        free(local);
        free(ruid);
    } else if (plan) {
        why = EM_NOT_READY; /* "component source binding changed" */
    }
    cJSON_Delete(plan);
    cJSON_Delete(intent);
    if (!op) {
        for (size_t i = 0; i < nrefs; i++) /* never journaled: the credentials go too */
            em_vault_forget(&a->vault, refs[i]);
        a->context_live = false;
        LOG("M2 not applied: %s", em_reason_name(why));
        return "wsc_rejected";
    }
    const char *id = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(op, "operation_id"));
    if (!id) /* the engine's own record: always has one */
        id = "";
    EM_FORMAT_FIXED(a->op_of_session, sizeof(a->op_of_session), "%s", id);
    EM_FORMAT_FIXED(a->ap_engine.wsc_op, sizeof(a->ap_engine.wsc_op), "%s", id);
    a->ap_engine.wsc_guard = wsc_bound;
    a->ap_engine.wsc_ctx = a;
    cJSON *done = em_engine_execute(&a->ap_engine, id);
    const cJSON *reason = cJSON_GetObjectItemCaseSensitive(done, "reason");
    LOG("operation %s (%s): %s %s", id, m2->bss[primary].ssid, em_state_of(done),
        cJSON_IsString(reason) ? reason->valuestring : "");
    cJSON_Delete(done);
    cJSON_Delete(op);
    return "wsc_operation";
}

/* a further radio's WSC exchange authority: its radio's scope context is the one its M1 went in */
static bool radio_wsc_bound(void *ctx)
{
    radio_scope *rs = ctx;
    int generation;
    char schema[65], token[65];
    return rs->context_live && em_ap_context(&rs->ap, &generation, schema, token) &&
           generation == rs->context_generation && !strcmp(schema, rs->context_schema) &&
           !strcmp(token, rs->context_token);
}

/* A further radio's authenticated M2 set into its own scope (spec 9; operation_bridge with a
 * RadioBackend): the set's BSSes fitted to that radio's slots, the first fronthaul with the others
 * (only the order within a role matters), no primary BSS; a RadioIntent and its WSC receipt. */
static const char *radio_operate(agent *a, radio_scope *rs, const em_message *m, const em_m2_result *m2)
{
    if (rs->op_of_session[0]) { /* the same exchange again: its operation stands */
        cJSON_Delete(em_engine_execute(&rs->engine, rs->op_of_session));
        return "radio_wsc_operation";
    }
    if (!radio_wsc_bound(rs)) {
        rs->context_live = false;
        return "radio_wsc_rejected";
    }
    const em_profile own = em_profile_slots_on(&a->profile, rs->band, a->multi_bss);
    size_t free_fronthaul = 0, free_backhaul = 0;
    for (size_t k = 0; k < own.nslots; k++) {
        if (!strcmp(own.slots[k].role, "fronthaul"))
            free_fronthaul++;
        else if (!strcmp(own.slots[k].role, "backhaul"))
            free_backhaul++;
    }
    char refs[8][64];
    size_t nrefs = 0;
    cJSON *additional = cJSON_CreateArray();
    for (size_t i = 0; i < m2->count && nrefs < 8; i++) {
        size_t *free_slots = !strcmp(m2->bss[i].role, "fronthaul") ? &free_fronthaul : &free_backhaul;
        if (*free_slots == 0)
            continue; /* left out: no slot of its role on this radio */
        (*free_slots)--;
        EM_FORMAT_FIXED(refs[nrefs], sizeof(refs[0]), "wsc-%s-%u", rs->exchange_id, (unsigned)(nrefs + 1));
        if (em_vault_persist_received(&a->vault, refs[nrefs], m2->bss[i].passphrase) != EM_OK)
            break;
        cJSON *b = cJSON_CreateObject();
        cJSON_AddStringToObject(b, "role", m2->bss[i].role);
        cJSON_AddStringToObject(b, "ssid", m2->bss[i].ssid);
        cJSON_AddStringToObject(b, "secret_ref", refs[nrefs++]);
        cJSON_AddItemToArray(additional, b);
    }
    cJSON *intent = em_radio_intent_record(a->pod_id, rs->band, additional);
    em_reason why = EM_OK;
    cJSON *plan = em_ap_backend().plan(&rs->ap, intent, &why), *op = NULL;
    if (plan && radio_wsc_bound(rs)) {
        cJSON *wsc = cJSON_CreateArray();
        for (size_t i = 0; i < m->ntlvs; i++)
            if (m->tlvs[i].kind == 0x11) {
                char *hex = em_hex(m->tlvs[i].value, m->tlvs[i].len);
                cJSON_AddItemToArray(wsc, cJSON_CreateString(hex));
                free(hex);
            }
        char request_fp[65], radio_id[16], *controller = em_hex(a->controller, 6), *local = em_hex(a->al, 6);
        char *ruid = em_hex(rs->ruid, 6);
        em_vault_fingerprint(&a->vault, wsc, request_fp);
        cJSON_Delete(wsc);
        em_radio_id(rs->band, radio_id);
        cJSON *receipt = cJSON_CreateObject();
        cJSON_AddStringToObject(receipt, "scope", "owned_simulation_wsc_component");
        cJSON_AddStringToObject(receipt, "process_id", em_journal_process_id(rs->journal));
        cJSON_AddStringToObject(receipt, "exchange_id", rs->exchange_id);
        cJSON_AddStringToObject(receipt, "m1_sha256", rs->m1_sha256);
        cJSON_AddStringToObject(receipt, "request_fingerprint", request_fp);
        cJSON_AddStringToObject(receipt, "controller_al", controller);
        cJSON_AddStringToObject(receipt, "agent_al", local);
        cJSON_AddStringToObject(receipt, "ruid", ruid);
        cJSON_AddStringToObject(receipt, "bssid", ruid); /* the radio: no BSS of its own */
        cJSON_AddStringToObject(receipt, "ingress", a->interface);
        cJSON_AddNumberToObject(receipt, "link_generation", 1);
        cJSON_AddNumberToObject(receipt, "database_generation", rs->context_generation);
        cJSON_AddStringToObject(receipt, "schema_fingerprint", rs->context_schema);
        cJSON_AddNumberToObject(receipt, "first_mid", m->mid);
        cJSON_AddNumberToObject(receipt, "bss_count", (double)m2->count);
        cJSON_AddStringToObject(receipt, "pod_id", a->pod_id);
        cJSON_AddStringToObject(receipt, "radio_id", radio_id);
        cJSON_AddStringToObject(receipt, "bss_id", radio_id); /* a radio scope's receipt names the radio */
        char source[64];
        EM_FORMAT_FIXED(source, sizeof(source), "wsc-component:%s", controller);
        op = em_engine_request(&rs->engine, intent, source, rs->exchange_id, a->run_id, AP_DEADLINE,
                               "wsc-component", receipt, &why);
        cJSON_Delete(receipt);
        free(controller);
        free(local);
        free(ruid);
    } else if (plan) {
        why = EM_NOT_READY; /* "component source binding changed" */
    }
    cJSON_Delete(plan);
    cJSON_Delete(intent);
    if (!op) {
        for (size_t i = 0; i < nrefs; i++) /* never journaled: the credentials go too */
            em_vault_forget(&a->vault, refs[i]);
        rs->context_live = false;
        LOG("M2 of the %s radio not applied: %s", rs->band, em_reason_name(why));
        return "radio_wsc_rejected";
    }
    const char *id = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(op, "operation_id"));
    if (!id)
        id = "";
    EM_FORMAT_FIXED(rs->op_of_session, sizeof(rs->op_of_session), "%s", id);
    EM_FORMAT_FIXED(rs->engine.wsc_op, sizeof(rs->engine.wsc_op), "%s", id);
    rs->engine.wsc_guard = radio_wsc_bound;
    rs->engine.wsc_ctx = rs;
    cJSON *done = em_engine_execute(&rs->engine, id);
    const cJSON *reason = cJSON_GetObjectItemCaseSensitive(done, "reason");
    LOG("operation %s (the %s radio, %zu of its set mapped): %s %s", id, rs->band, nrefs, em_state_of(done),
        cJSON_IsString(reason) ? reason->valuestring : "");
    cJSON_Delete(done);
    cJSON_Delete(op);
    return "radio_wsc_operation";
}

/* -- client steering and the probe watch (emosa.agent.steering, .probe_watch) --------- */

static const char *start_steering(void *ctx, const em_steering_request *r, uint16_t mid)
{
    agent *a = ctx;
    (void)mid;
    if (!a->steering_on)
        return "steering_off";
    return em_steering_start(&a->steering, r, mid);
}

static void watch_ask(void *ctx, const uint8_t (*stations)[6], size_t n)
{
    agent *a = ctx;
    if (a->telemetry_on)
        em_watch_ask(&a->watch, stations, n);
}

/* -- Backhaul Steering: the uplink scope carries the move out ------------------------ */

/* the band of an IEEE 802.11 Annex E global operating class, or NULL */
static const char *band_of_operating_class(int operating_class)
{
    if (operating_class >= 81 && operating_class <= 84)
        return "2.4G";
    if (operating_class >= 115 && operating_class <= 130)
        return "5G";
    if (operating_class >= 131 && operating_class <= 137)
        return "6G";
    return NULL;
}

static const char *bh_executor(void *ctx, const char *bssid, int operating_class, int channel)
{
    agent *a = ctx;
    return em_uplink_steer(&a->uplink, bssid, band_of_operating_class(operating_class), channel);
}

static int bh_outcome(void *ctx, const char *bssid, const char **why)
{
    agent *a = ctx;
    return em_uplink_steering_outcome(&a->uplink, bssid, why);
}

/* -- the onboarding session (spec §2.5) ---------------------------------------------- */

static void end_radio_exchanges(agent *a);

/* the session's own state ends (the lifecycle decides what comes next) */
static void end_session(agent *a)
{
    a->context_live = false;
    em_early_close(&a->early);
    a->ap_engine.wsc_guard = NULL;
    if (a->reporting_live)
        em_reporting_close(&a->reporting);
    a->reporting_live = false;
    if (a->bh_live)
        em_bh_close(&a->bh); /* a move under way is answered by the next session */
    a->bh_live = false;
    if (a->has_m1)
        em_m1_free(&a->m1);
    a->has_m1 = false;
    end_radio_exchanges(a);
    a->control_ready = false;
}

/* an attempt that failed between ticks: the next tick begins its back-off */
static void close_session(agent *a, em_session_state end)
{
    end_session(a);
    a->at.state = end;
}

static void renew(agent *a, const char *why)
{
    LOG("%s: fresh M1", why);
    end_session(a);
    em_attempts_renew(&a->at);
}

static void start_session(agent *a)
{
    a->nadmission_issues = 0;
    a->op_of_session[0] = 0;
    a->nclients = 0;
    a->last_topology[0] = 0;
    a->reannounce = (em_reannounce){0};
    a->topology_responses = 0;
    em_reassembler_free(a->assembly);
    a->assembly = em_reassembler_new(NULL, NULL, 5.0, 8, 65536, 16384, 16);
}

static void search(agent *a)
{
    em_binding b = {0};
    memcpy(b.local_al, a->al, 6);
    uint8_t profile2[4] = {0, 0, 0, 0};
    em_frames f;
    uint16_t mid = next_mid(a);
    a->search_mids[(a->at.searches - 1) % 3] = mid;
    if (em_search_frames(&b, 0, 1, profile2, a->r1 ? EM_SET_R1 : EM_SET_61, mid, &f) == EM_OK) {
        send_frames(a, &f);
        count(&a->counts, "search_sent");
    }
}

static em_m1_device m1_device(agent *a)
{
    em_m1_device d = {0};
    char seed[128];
    uint8_t digest[32];
    EM_FORMAT_FIXED(seed, sizeof(seed), "emosa-agent:%s", a->serial); /* serial: at most 64 (schema) */
    SHA256((const uint8_t *)seed, strlen(seed), digest);
    memcpy(d.uuid, digest, 16);
    memcpy(d.al_mac, a->al, 6);
    d.authentication_types = 0x20;
    d.encryption_types = 8;
    d.connection_types = 1;
    d.configuration_methods = 0x0280;
    d.wps_state = 2;
    em_buf_put(&d.manufacturer, "OpenSync via EMOSA", 18);
    em_buf_put(&d.model_name, "OpenSync pod", 12);
    const char *fw = a->view.has_firmware ? a->view.firmware : "6.6";
    em_buf_put(&d.model_number, fw, strlen(fw) > 32 ? 32 : strlen(fw));
    size_t sl = strlen(a->serial) > 32 ? 32 : strlen(a->serial);
    em_buf_put(&d.serial_number, a->serial, sl);
    memcpy(d.primary_device_type, "\x00\x06\x00\x50\xf2\x04\x00\x01", 8);
    em_buf_put(&d.device_name, a->serial, sl);
    d.rf_band = 1;
    d.device_password_id = 4;
    d.os_version = 1;
    return d;
}

static void free_device(em_m1_device *d)
{
    em_buf_free(&d->manufacturer);
    em_buf_free(&d->model_name);
    em_buf_free(&d->model_number);
    em_buf_free(&d->serial_number);
    em_buf_free(&d->device_name);
}

/* the AP Radio Basic Capabilities of the k-th reported radio (0: the fronthaul) */
static em_radio_caps radio_caps_at(agent *a, size_t k)
{
    em_radio_caps r = {0};
    const em_tlv *basic = NULL;
    for (size_t i = 0, seen = 0; i < a->caps.count && !basic; i++)
        if (a->caps.tlvs[i].kind == 0x85 && seen++ == k)
            basic = &a->caps.tlvs[i];
    if (!basic)
        return r;
    /* the agent's own TLV (em_capabilities_tlvs): every class of the radio's band */
    const uint8_t *v = basic->value;
    size_t at = 8;
    memcpy(r.ruid, v, 6);
    r.max_bss = v[6];
    for (size_t j = 0; j < v[7] && j < sizeof(r.classes) / sizeof(r.classes[0]) && at + 3 <= basic->len &&
                       at + 3 + (size_t)v[at + 2] <= basic->len;
         j++) {
        em_basic_class *c = &r.classes[r.nclasses++];
        c->operating_class = v[at];
        c->max_eirp_dbm = (int8_t)v[at + 1];
        c->nnon_operable = v[at + 2] < sizeof(c->non_operable) ? v[at + 2] : sizeof(c->non_operable);
        memcpy(c->non_operable, v + at + 3, c->nnon_operable);
        at += 3 + (size_t)v[at + 2];
    }
    return r;
}

static em_radio_caps radio_caps(agent *a) { return radio_caps_at(a, 0); }

/* a further radio's exchange ends with the session: no M1 transcript is reused */
static void end_radio_exchanges(agent *a)
{
    for (size_t k = 0; k < a->nradio_scopes; k++) {
        radio_scope *rs = &a->radio_scopes[k];
        if (rs->has_m1)
            em_m1_free(&rs->m1);
        rs->has_m1 = rs->started = rs->awaiting = rs->context_live = false;
        rs->op_of_session[0] = 0;
        rs->engine.wsc_guard = NULL;
    }
}

/* a further radio's M1 is out and its M2 not yet taken */
static bool radio_awaiting(const agent *a)
{
    for (size_t k = 0; k < a->nradio_scopes; k++)
        if (a->radio_scopes[k].awaiting)
            return true;
    return false;
}

/* Spec 9 (EasyMesh 6.1 §7.1: one M1 per radio): once the fronthaul is provisioned, each further
 * radio the agent reports sends its own M1 in turn, one exchange awaiting an M2 at a time. A
 * failed start ends the attempt, as a failed admission does (a fresh one follows). */
static void next_radio(agent *a)
{
    if (radio_awaiting(a))
        return;
    radio_scope *rs = NULL;
    for (size_t k = 0; k < a->nradio_scopes && !rs; k++)
        if (a->radio_scopes[k].bound && !a->radio_scopes[k].started)
            rs = &a->radio_scopes[k];
    if (!rs)
        return;
    rs->started = true;
    em_m1_device d = m1_device(a);
    d.rf_band = strcmp(rs->band, "2.4G") ? 2 : 1; /* WSC RF Bands: 2 for 5 GHz */
    em_reason r = em_m1_create(&d, NULL, &rs->m1);
    free_device(&d);
    rs->context_live = r == EM_OK && em_ap_context(&rs->ap, &rs->context_generation, rs->context_schema, rs->context_token);
    em_radio_caps caps = radio_caps_at(a, rs->caps_index);
    if (r != EM_OK || !rs->context_live || caps.max_bss == 0) {
        if (r == EM_OK)
            em_m1_free(&rs->m1);
        close_session(a, S_FAILED);
        count(&a->counts, "radio_m1_failed");
        return;
    }
    rs->has_m1 = true;
    memcpy(rs->ruid, caps.ruid, 6);
    em_uuid4_hex(rs->exchange_id);
    em_sha256_hex(rs->m1.message.data, rs->m1.message.len, rs->m1_sha256);
    em_binding b = a->control.binding;
    em_frames f;
    if (em_m1_frames(&b, &caps, &rs->m1, a->r1 ? EM_SET_R1 : EM_SET_61, next_mid(a), &f) == EM_OK)
        send_frames(a, &f);
    rs->awaiting = true;
    count(&a->counts, "radio_m1_sent");
}

/* A further radio's M2 (its AP Radio Identifier names it): into that radio's own scope. A set
 * refused for good (spec 3.4) ends its wait: a fresh M1 would get the same set. */
static void radio_m2(agent *a, radio_scope *rs, const em_message *m)
{
    em_binding b = a->control.binding;
    em_radio_caps caps = radio_caps_at(a, rs->caps_index);
    em_m2_result result;
    em_reason r = em_receive_m2(&b, &caps, &rs->m1, m, true, a->shared_session, &result);
    if (r != EM_OK || result.teardown) {
        LOG("M2 of the %s radio rejected: %s", rs->band, r != EM_OK ? em_reason_name(r) : "teardown");
        if (r == EM_OK || r == EM_INVALID_INPUT || r == EM_UNSUPPORTED_OPERATION) /* EM_OK: a teardown */
            rs->awaiting = false;
        count(&a->counts, "radio_wsc_rejected");
        em_m2_result_clear(&result);
        return;
    }
    const char *outcome = radio_operate(a, rs, m, &result);
    em_m2_result_clear(&result);
    if (!strcmp(outcome, "radio_wsc_operation"))
        rs->awaiting = false;
    count(&a->counts, outcome);
}

static void send_early(agent *a)
{
    uint16_t mid = next_mid(a);
    send_message(a, a->controller, 0x8043, mid, a->caps.tlvs, a->caps.count, false);
    em_early_sent(&a->early, mid, now());
}

static void early_tick(agent *a)
{
    bool same = source_current(a) && a->generation == a->early_generation && a->revision == a->early_revision;
    if (em_early_tick(&a->early, now(), same))
        send_early(a);
}

/* The accepted channel policy's record, with the pod State it was accepted on. */
static bool keep_channel_policy(void *ctx, const cJSON *record)
{
    agent *a = ctx;
    cJSON *kept = cJSON_Duplicate(record, true);
    char context[96];
    EM_FORMAT_FIXED(context, sizeof(context), "%s/%d", em_journal_process_id(a->journal), a->generation);
    cJSON_AddStringToObject(kept, "context", context);
    bool ok = em_channel_store_save(a->channel_store, kept);
    cJSON_Delete(kept);
    return ok;
}

/* The agent's first session after it starts: the kept policy is back to the radio's
 * default (a reboot resets it, spec §8.2), recorded rather than trusted. */
static void reset_channel_policy(agent *a)
{
    char *controller = em_hex(a->controller, 6), *local = em_hex(a->al, 6);
    cJSON *record = cJSON_CreateObject();
    cJSON_AddStringToObject(record, "status", "reboot_default");
    cJSON_AddStringToObject(record, "controller", controller);
    cJSON_AddStringToObject(record, "local_al", local);
    cJSON_AddArrayToObject(record, "radios"); /* no radio's preferences kept (spec 9: each its own) */
    if (!em_channel_store_save(a->channel_store, record))
        WARN("channel policy record not reset");
    cJSON_Delete(record);
    free(controller);
    free(local);
}

static void admitted(agent *a)
{
    em_binding b = {0};
    memcpy(b.local_al, a->al, 6);
    memcpy(b.controller_al, a->controller, 6);
    if (!a->r1) { /* EasyMesh 6.1: the Early AP Capability Report before M1, retried until acknowledged */
        memset(&a->early, 0, sizeof(a->early)); /* the session's report coordinator */
        a->early_generation = a->generation;
        a->early_revision = a->revision;
        em_early_start(&a->early, now());
        send_early(a);
    }
    em_m1_device d = m1_device(a);
    em_reason r = em_m1_create(&d, NULL, &a->m1);
    free_device(&d);
    /* the exchange's authority: the scope context it starts in (WscComponentBridge.start) */
    a->context_live = r == EM_OK && em_ap_context(&a->ap, &a->context_generation, a->context_schema, a->context_token);
    if (r != EM_OK || !a->context_live) {
        if (r == EM_OK)
            em_m1_free(&a->m1);
        close_session(a, S_FAILED);
        count(&a->counts, r == EM_OK ? "rejected_NOT_READY" : "m1_failed");
        return;
    }
    a->has_m1 = true;
    em_uuid4_hex(a->exchange_id);
    em_sha256_hex(a->m1.message.data, a->m1.message.len, a->m1_sha256);
    em_radio_caps caps = radio_caps(a);
    em_frames f;
    if (em_m1_frames(&b, &caps, &a->m1, a->r1 ? EM_SET_R1 : EM_SET_61, next_mid(a), &f) == EM_OK)
        send_frames(a, &f);
    a->at.state = S_AWAITING_M2;
    em_reporting_start(&a->reporting, a->policy_store, a->controller, a->al, a->represented.ruid, now());
    /* every radio the agent reports for this source (spec 9): its policy may name each */
    for (size_t k = 0; k < a->nreported && k < 4; k++)
        memcpy(a->reporting.radio_ids[a->reporting.nradio_ids++], a->reported[k].ruid, 6);
    a->reporting_live = a->policy_store != NULL;
    if (a->uplink_on) /* Backhaul Steering: the pod has a Multi-AP backhaul station to move */
        em_bh_start(&a->bh, &a->bh_shared, a->controller, a->al, bh_executor, bh_outcome, a);
    a->bh_live = a->uplink_on;
    memset(&a->control, 0, sizeof(a->control));
    a->control.binding = b;
    memcpy(a->control.binding.sources[0], a->controller, 6);
    a->control.binding.nsources = 1;
    if (a->channel_store) {
        if (a->at.starts == 1)
            reset_channel_policy(a);
        a->control.keep_channel_policy = keep_channel_policy;
        a->control.keep_ctx = a;
    }
    a->control_ready = true;
    count(&a->counts, a->r1 ? "m1_sent" : "early_then_m1_sent");
}

static void reply(agent *a, uint16_t type, uint16_t mid, const em_tlv *tlvs, size_t n)
{
    send_message(a, a->controller, type, mid, tlvs, n, false);
}

/* -- backhaul link metrics (spec §8.5; emosa.wire.link_metrics.BackhaulLinkMetrics) ------- */

typedef struct {
    uint8_t al[6], local[6], neighbor[6];
    uint16_t media;
    bool has_tx, has_rx;
    double tx[3], rx[3]; /* (packet errors, packets, rate Mbit/s), (packet errors, packets, SNR dB) */
} backhaul_pair;

/* (packet errors, packets, value) from a station's measurement in its status form, all three known */
static bool measured(const cJSON *seen, const char *errors, const char *packets, const char *value, double out[3])
{
    const char *keys[3] = {errors, packets, value};
    for (int i = 0; i < 3; i++) {
        const cJSON *x = cJSON_GetObjectItemCaseSensitive(seen, keys[i]);
        if (!cJSON_IsNumber(x))
            return false;
        out[i] = x->valuedouble;
    }
    return true;
}

static const cJSON *measurement(const cJSON *stations, const uint8_t mac[6])
{
    char *key = em_mac_str(mac);
    const cJSON *seen = key ? cJSON_GetObjectItemCaseSensitive(stations, key) : NULL;
    free(key);
    return cJSON_IsObject(seen) ? seen : NULL;
}

/* the Wi-Fi backhaul neighbors and their links' measurements: the parent's from its measurement of
 * this pod's station (directions swapped, while current), each child's from this pod's own */
static size_t backhaul_pairs(agent *a, backhaul_pair *out, size_t cap)
{
    size_t n = 0;
    if (!a->peers_on)
        return 0;
    em_peers_read(&a->peers, now());
    double freshness = a->pod_metrics_on ? a->freshness : 240;
    if (a->has_backhaul) {
        const em_peer *p = em_peers_upstream(&a->peers, a->backhaul.station.parent);
        if (p && n < cap) {
            backhaul_pair *x = &out[n++];
            memset(x, 0, sizeof(*x));
            memcpy(x->al, p->al, 6);
            memcpy(x->local, a->backhaul.station.mac, 6);
            memcpy(x->neighbor, a->backhaul.station.parent, 6);
            x->media = strcmp(a->backhaul.band, "5G") ? 0x0103 : 0x0104;
            const cJSON *seen = measurement(p->measured, a->backhaul.station.mac);
            const cJSON *at = cJSON_GetObjectItemCaseSensitive(seen, "measured_at");
            if (seen && !(wall() - (cJSON_IsNumber(at) ? at->valuedouble : 0) > freshness)) {
                x->has_tx = measured(seen, "rx_errors", "rx_frames", "rx_rate_mbps", x->tx);
                x->has_rx = measured(seen, "tx_errors", "tx_frames", "snr_db", x->rx);
            }
        }
    }
    cJSON *stats = a->pod_metrics_on ? em_pod_stats_status(&a->stats) : NULL;
    const cJSON *stations = cJSON_GetObjectItemCaseSensitive(stats, "stations");
    /* a child on a backhaul BSS of any of the pod's radios, its link of that radio's band */
    em_topology_radio shown[1 + EXTRA_RADIOS];
    size_t nshown = shown_radios(a, shown);
    for (size_t k = 0; k < a->peers.n && n < cap; k++) {
        const em_peer *p = &a->peers.peers[k];
        const em_bss_view *b = NULL;
        const em_radio_view *on_radio = NULL;
        for (size_t r = 0; p->has_station && !b && r < nshown; r++)
            for (size_t i = 0; !b && i < shown[r].radio->nbss; i++)
                if (shown[r].radio->bss[i].backhaul && !memcmp(shown[r].radio->bss[i].bssid, p->parent, 6)) {
                    b = &shown[r].radio->bss[i];
                    on_radio = shown[r].radio;
                }
        bool on = false;
        for (size_t s = 0; b && s < b->nstations; s++)
            on = on || !memcmp(b->stations[s], p->station, 6);
        if (on) {
            backhaul_pair *x = &out[n++];
            memset(x, 0, sizeof(*x));
            memcpy(x->al, p->al, 6);
            memcpy(x->local, b->bssid, 6);
            memcpy(x->neighbor, p->station, 6);
            x->media = strcmp(on_radio->band, "5G") ? 0x0103 : 0x0104;
            const cJSON *seen = measurement(stations, p->station);
            x->has_tx = seen && measured(seen, "tx_errors", "tx_frames", "tx_rate_mbps", x->tx);
            x->has_rx = seen && measured(seen, "rx_errors", "rx_frames", "snr_db", x->rx);
        }
    }
    cJSON_Delete(stats);
    return n;
}

static uint32_t counter(double v) { return v <= 0 ? 0 : v >= 4294967295.0 ? 4294967295u : (uint32_t)v; }
static uint16_t short_of(double v, double cap) { return v <= 0 ? 0 : v >= cap ? (uint16_t)cap : (uint16_t)v; }

static void put16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)(v >> 8); p[1] = (uint8_t)v; }
static void put32(uint8_t *p, uint32_t v)
{
    for (int i = 0; i < 4; i++)
        p[i] = (uint8_t)(v >> (24 - 8 * i));
}

/* A Link Metric Query for the Wi-Fi backhaul's neighbors, answered at once with the same MID; false
 * when this cannot answer it (another neighbor, nothing measured, an invalid query): nothing sent */
static bool backhaul_link_metrics(agent *a, const em_message *m)
{
    const em_tlv *q = NULL;
    size_t nq = 0;
    for (size_t i = 0; i < m->ntlvs; i++)
        if (m->tlvs[i].kind == 0x08) {
            q = &m->tlvs[i];
            nq++;
        }
    if (nq != 1 || q->len < 1 || q->value[0] > 1 || q->len != (q->value[0] ? 8 : 2) || q->value[q->len - 1] > 2)
        return false;
    uint8_t direction = q->value[q->len - 1];
    const uint8_t *neighbor = q->value[0] ? q->value + 1 : NULL;
    backhaul_pair pairs[24];
    size_t n = backhaul_pairs(a, pairs, 24), count = 0;
    uint8_t values[48][41];
    em_tlv tlvs[48];
    for (size_t i = 0; i < n; i++) {
        const backhaul_pair *p = &pairs[i];
        if (neighbor && memcmp(p->al, neighbor, 6))
            continue;
        if ((direction == 0 || direction == 2) && p->has_tx) {
            uint8_t *v = values[count];
            memcpy(v, a->al, 6);
            memcpy(v + 6, p->al, 6);
            memcpy(v + 12, p->local, 6);
            memcpy(v + 18, p->neighbor, 6);
            put16(v + 24, p->media);
            v[26] = 0; /* no IEEE 802.1 bridge */
            put32(v + 27, counter(p->tx[0]));
            put32(v + 31, counter(p->tx[1]));
            put16(v + 35, short_of(p->tx[2], 65535));
            put16(v + 37, 100);   /* link availability */
            put16(v + 39, 65535); /* 1905: the PHY rate is unspecified for IEEE 802.11 */
            tlvs[count++] = (em_tlv){0x09, 41, v};
        }
        if ((direction == 1 || direction == 2) && p->has_rx) {
            uint8_t *v = values[count];
            memcpy(v, a->al, 6);
            memcpy(v + 6, p->al, 6);
            memcpy(v + 12, p->local, 6);
            memcpy(v + 18, p->neighbor, 6);
            put16(v + 24, p->media);
            put32(v + 26, counter(p->rx[0]));
            put32(v + 30, counter(p->rx[1]));
            v[34] = (uint8_t)short_of(p->rx[2], 254); /* the SNR (spec §8.5) */
            tlvs[count++] = (em_tlv){0x0A, 35, v};
        }
    }
    if (!count)
        return false;
    reply(a, 0x0006, m->mid, tlvs, count);
    return true;
}

/* the radios the control procedures answer for (spec 9), the fronthaul first: each on its one
 * operable channel at its operated power, a further radio on a channel of its class; a radio whose
 * power the pod does not report (OpenSync leaves it unset when the radio's VIFs disagree) at its
 * advertised maximum EIRP, a bound (pod.operated) */
static bool station_holds(const em_radio_view *r)
{
    /* a station on the radio connected to a parent holds its channel (spec 2.5) */
    for (size_t i = 0; i < r->nuplinks; i++)
        if (r->uplinks[i].has_parent)
            return true;
    return false;
}

static void control_radios(agent *a)
{
    em_control *c = &a->control;
    c->nradios = 0;
    em_control_radio *f = &c->radios[c->nradios++];
    bool front_bound = !a->represented.has_tx_power;
    /* on the channel its capabilities report (spec 9: held for a moment between two) */
    *f = (em_control_radio){.op_class = 81, .channel = a->channel,
                            .tx_power_dbm = front_bound ? a->max_eirp : a->tx_power,
                            .max_eirp_dbm = a->max_eirp, .measured = true, .bound = front_bound,
                            .held = station_holds(&a->represented)};
    memcpy(f->ruid, a->represented.ruid, 6);
    c->nfurther = 0;
    for (size_t k = 0; k < a->nradio_scopes && c->nradios < 4; k++) {
        const radio_scope *rs = &a->radio_scopes[k];
        if (!rs->bound)
            continue;
        if (c->nfurther < sizeof(c->further) / sizeof(c->further[0]))
            c->further[c->nfurther++] = &rs->represented; /* its BSSes and their stations */
        int8_t eirp = 20;
        for (size_t i = 0; i < a->nreported; i++)
            if (!memcmp(a->reported[i].ruid, rs->represented.ruid, 6))
                eirp = a->reported[i].max_eirp;
        uint8_t op_class = em_operating_class(rs->band, rs->channel); /* 115 for 44, 125 for 149 */
        bool bound = !rs->represented.has_tx_power;
        em_control_radio *x = &c->radios[c->nradios++];
        *x = (em_control_radio){.op_class = op_class, .channel = rs->channel,
                                .tx_power_dbm = bound ? eirp : rs->represented.tx_power,
                                .max_eirp_dbm = eirp, .measured = op_class != 0, .bound = bound,
                                .held = station_holds(&rs->represented)};
        memcpy(x->ruid, rs->represented.ruid, 6);
    }
}

static void handle_message(agent *a, const em_message *m)
{
    char label[48];
    if (m->message_type == 0x0008 && a->at.state == S_DISCOVERING) {
        em_advertisement adv;
        bool mid_ok = false;
        for (unsigned i = 0; i < a->at.searches && i < 3; i++)
            mid_ok = mid_ok || a->search_mids[i] == m->mid;
        em_message_set set = a->r1 ? EM_SET_R1 : EM_SET_61;
        if (em_parse_response(m, set, &adv) != EM_OK || !mid_ok || !em_response_usable(&adv, set)) {
            count(&a->counts, "rejected_INVALID_INPUT");
            return;
        }
        /* the session's admission (non_dpp_admission): a Response outside the contract
         * ends it as incompatible, until a renewal */
        a->nadmission_issues = em_admission_issues(&adv, set, a->admission_issues);
        if (a->nadmission_issues) {
            a->at.state = S_INCOMPATIBLE;
            count(&a->counts, "response_incompatible");
            LOG("controller incompatible: %s", a->admission_issues[0]);
            return;
        }
        admitted(a);
        return;
    }
    if (m->message_type == 0x0009) {
        if (a->at.state != S_AWAITING_M2 && a->at.state != S_PROVISIONING) {
            count(&a->counts, "wsc_before_admission");
            return;
        }
        /* spec 9: an M2 for the radio its AP Radio Identifier names: a further radio's exchange,
         * else the fronthaul's (whose exchange checks the radio again) */
        for (size_t i = 0; i < m->ntlvs; i++) {
            if (m->tlvs[i].kind != 0x82 || m->tlvs[i].len != 6)
                continue;
            for (size_t k = 0; k < a->nradio_scopes; k++) {
                radio_scope *rs = &a->radio_scopes[k];
                if (rs->has_m1 && !memcmp(rs->ruid, m->tlvs[i].value, 6)) {
                    radio_m2(a, rs, m);
                    return;
                }
            }
            break;
        }
        em_binding b = a->control.binding;
        em_radio_caps caps = radio_caps(a);
        em_m2_result result;
        em_reason r = em_receive_m2(&b, &caps, &a->m1, m, a->multi_bss, a->shared_session, &result);
        if (r != EM_OK || result.teardown) {
            LOG("M2 rejected: %s", r != EM_OK ? em_reason_name(r) : "teardown");
            count(&a->counts, "wsc_rejected");
            em_m2_result_clear(&result);
            return;
        }
        const char *outcome = operate(a, m, &result);
        em_m2_result_clear(&result); /* the passphrases are in the vault now, or refused */
        count(&a->counts, outcome);
        if (!strcmp(outcome, "wsc_operation") && a->at.state != S_PROVISIONING) {
            a->at.state = S_PROVISIONING;
            em_reannounce_provisioned(&a->reannounce, a->topology_responses);
        }
        return;
    }
    if (!a->control_ready) {
        EM_FORMAT_FIXED(label, sizeof(label), "unsupported_message_%04x", m->message_type);
        count(&a->counts, label);
        return;
    }
    if (m->message_type == 0x0002) {
        em_tlv_list list;
        if (topology_tlvs(a, a->r1, &list) == EM_OK) {
            reply(a, 0x0003, m->mid, list.tlvs, list.count);
            em_tlv_list_free(&list);
            a->topology_responses++; /* the reports' counter, as the reference's coordinator */
        }
        return;
    }
    if (m->message_type == 0x8000) {
        bool error = false; /* an Error Code or security companion is no clean receipt */
        for (size_t i = 0; i < m->ntlvs; i++)
            error = error || m->tlvs[i].kind == 0xA3 || m->tlvs[i].kind == 0xBC;
        em_early_ack(&a->early, m->mid, error, now());
        return;
    }
    if ((m->message_type == 0x8014 || m->message_type == 0x800F) && a->at.state != S_PROVISIONING) {
        EM_FORMAT_FIXED(label, sizeof(label), "unsupported_message_%04x", m->message_type);
        count(&a->counts, label);
        return;
    }
    if (m->message_type == 0x8003 && a->reporting_live) {
        /* the Multi-AP Policy: persisted, then acknowledged (emosa.wire.reporting_policy) */
        em_metric_source source = metric_source(a);
        em_frames out = {0};
        em_reason error;
        const char *result = em_reporting_policy(&a->reporting, m, now(), a->at.state == S_PROVISIONING, &source,
                                                 next_mid_cb, a, &out, &error);
        send_frames(a, &out);
        if (result) {
            count(&a->counts, result);
        } else {
            EM_FORMAT_FIXED(label, sizeof(label), "rejected_%s", em_reason_name(error));
            count(&a->counts, label);
        }
        return;
    }
    if (m->message_type == 0x800B && a->at.state == S_PROVISIONING && a->reporting_live) {
        em_metric_source source = metric_source(a);
        em_frames out = {0};
        em_reason error;
        const char *result = em_reporting_query(&a->reporting, m, true, &source, &out, &error);
        send_frames(a, &out);
        if (result) {
            count(&a->counts, result);
        } else {
            EM_FORMAT_FIXED(label, sizeof(label), "rejected_%s", em_reason_name(error));
            count(&a->counts, label);
        }
        return;
    }
    /* channel and client steering; the unassociated query from the pod's probes */
    a->control.stats = a->telemetry_on ? &a->stats : NULL;
    a->control.watch = a->watch_on ? watch_ask : NULL;
    a->control.watch_ctx = a;
    a->control.radio = &a->represented;
    a->control.tx_power_dbm = a->tx_power;
    a->control.max_eirp_dbm = a->max_eirp;
    control_radios(a);
    a->control.previous_mid = a->mid;
    a->control.executor = start_steering;
    a->control.executor_ctx = a;
    em_frames out = {0};
    em_reason error;
    unsigned bound_before = a->control.bound_reported;
    const char *result = em_control_handle(&a->control, m, &out, &error);
    a->mid = a->control.previous_mid;
    for (size_t k = 0; a->control.bound_reported != bound_before && k < a->control.nradios; k++) {
        const em_control_radio *x = &a->control.radios[k];
        if (!x->bound)
            continue;
        /* logged and counted each time it is used (spec 9) */
        char *mac = em_mac_str(x->ruid);
        LOG("radio %s: no transmit power from the pod, its advertised maximum %d dBm reported as a bound", mac,
            x->tx_power_dbm);
        free(mac);
        count(&a->counts, "operating_power_bound_reported");
    }
    if (result || error != EM_OK) {
        send_frames(a, &out);
        if (result) {
            count(&a->counts, result);
        } else {
            EM_FORMAT_FIXED(label, sizeof(label), "rejected_%s", em_reason_name(error));
            count(&a->counts, label);
        }
        return;
    }
    em_frames_free(&out);
    if (m->message_type == 0x8001) {
        /* AP Capability Report: the capability TLVs (no cipher suites) and the inventory */
        em_tlv tlvs[33];
        size_t n = 0;
        for (size_t i = 0; i < a->caps.count; i++)
            if (a->caps.tlvs[i].kind != 0xED)
                tlvs[n++] = a->caps.tlvs[i];
        tlvs[n++] = a->inventory;
        reply(a, 0x8002, m->mid, tlvs, n);
        count(&a->counts, "ap_capability_report_sent");
    } else if (m->message_type == 0x8009) {
        const em_tlv *info = NULL;
        for (size_t i = 0; i < m->ntlvs; i++)
            if (m->tlvs[i].kind == 0x90)
                info = info ? NULL : &m->tlvs[i];
        if (!info || info->len != 12) {
            count(&a->counts, "rejected_INVALID_INPUT");
            return;
        }
        bool associated = false;
        for (size_t i = 0; i < a->represented.nbss; i++)
            for (size_t k = 0; k < a->represented.bss[i].nstations; k++)
                associated = associated || !memcmp(a->represented.bss[i].stations[k], info->value + 6, 6);
        uint8_t result_code = 1, error_value[7];
        error_value[0] = associated ? 3 : 2; /* associated, frame unavailable / not associated */
        memcpy(error_value + 1, info->value + 6, 6);
        em_tlv tlvs[3] = {*info, {0x91, 1, &result_code}, {0xA3, 7, error_value}};
        reply(a, 0x800A, m->mid, tlvs, 3);
        count(&a->counts, "client_capability_unavailable_report");
    } else if (m->message_type == 0x8027) {
        /* one Backhaul STA Radio Capabilities TLV (RUID, MAC-included flag, STA MAC when known)
         * for each radio of the pod with a backhaul station, enabled or not, the one in use
         * first (EasyMesh 6.1, 9.3; spec §8.3): the bands the controller may move the pod to */
        uint8_t v[EM_UPLINK_BANDS + 1][13];
        em_tlv t[EM_UPLINK_BANDS + 1];
        for (size_t i = 0; i < a->nradios; i++) {
            memcpy(v[i], a->radios[i].ruid, 6);
            v[i][6] = a->radios[i].has_station ? 0x80 : 0x00;
            memcpy(v[i] + 7, a->radios[i].station, 6);
            t[i] = (em_tlv){0xCB, a->radios[i].has_station ? 13 : 7, v[i]};
        }
        reply(a, 0x8028, m->mid, a->nradios ? t : NULL, a->nradios);
        count(&a->counts, "backhaul_sta_capability_report_sent");
    } else if (m->message_type == 0x8019 && a->bh_live) {
        /* the move goes to the uplink scope (emosa.wire.backhaul_steering) */
        uint8_t stations[1][6];
        size_t n = 0;
        if (a->has_backhaul)
            memcpy(stations[n++], a->backhaul.station.mac, 6);
        em_frames moves = {0};
        em_reason refusal;
        const char *moved = em_bh_handle(&a->bh, m, now(), (const uint8_t(*)[6])stations, n, &moves, &refusal);
        send_frames(a, &moves);
        if (moved) {
            count(&a->counts, moved);
        } else {
            EM_FORMAT_FIXED(label, sizeof(label), "rejected_%s", em_reason_name(refusal));
            count(&a->counts, label);
        }
    } else if (m->message_type == 0x8019) {
        const em_tlv *request = NULL;
        for (size_t i = 0; i < m->ntlvs; i++)
            if (m->tlvs[i].kind == 0x9E)
                request = request ? NULL : &m->tlvs[i];
        if (!request || request->len != 14) {
            count(&a->counts, "rejected_INVALID_INPUT");
            return;
        }
        uint8_t response[13], error_code[7];
        memcpy(response, request->value, 12);
        response[12] = 0x01;  /* failure: EMOSA does not move the pod's backhaul */
        error_code[0] = 0x06; /* the Error Code TLV of a failure: association failed, the station */
        memcpy(error_code + 1, request->value, 6);
        em_tlv t[2] = {{0x9F, 13, response}, {0xA3, 7, error_code}};
        reply(a, 0x8000, m->mid, NULL, 0);
        reply(a, 0x801A, m->mid, t, 2);
        count(&a->counts, "backhaul_steering_refused");
        em_log(EM_LOG_INFO, "emosa.wire.backhaul_steering",
               "backhaul steering refused (code 0x06): no session to move the backhaul in");
    } else if (m->message_type == 0x0005) {
        if (backhaul_link_metrics(a, m))
            count(&a->counts, "backhaul_link_metric_response_sent");
        else
            count(&a->counts, "neighbor_measurement_unavailable");
    } else if (m->message_type == 0x800B) {
        count(&a->counts, "ap_measurements_unavailable");
    } else {
        EM_FORMAT_FIXED(label, sizeof(label), "unsupported_message_%04x", m->message_type);
        count(&a->counts, label);
    }
}

static void receive_frame(agent *a, const uint8_t *frame, size_t len)
{
    if (len < 22)
        return;
    uint16_t type = (uint16_t)(frame[16] << 8 | frame[17]);
    bool from_controller = !memcmp(frame + 6, a->controller, 6);
    if (from_controller)
        em_renew_contact(&a->renewals, now());
    if (from_controller && type == 0x0002)
        em_renew_topology_query(&a->renewals, now());
    if (from_controller && type == 0x000A) {
        count(&a->counts, "renew_received");
        renew(a, "AP-Autoconfiguration Renew from the controller");
        return;
    }
    if (a->at.state == S_NONE || a->at.state == S_FAILED || a->at.state == S_SOURCE_LOST ||
        a->at.state == S_INCOMPATIBLE)
        return;
    /* not from this agent's controller to this agent: dropped before the rate budget */
    if (memcmp(frame, a->al, 6) || !from_controller) {
        count(&a->counts, "foreign_frame");
        return;
    }
    double t = now();
    a->tokens = a->tokens + (t - a->token_time) * 16;
    if (a->tokens > 32)
        a->tokens = 32;
    a->token_time = t;
    if (a->tokens < 1) {
        count(&a->counts, "rate_limited");
        return;
    }
    a->tokens -= 1;
    if (!source_current(a)) {
        count(&a->counts, "source_unavailable");
        return;
    }
    em_message *m = NULL;
    em_reason r = em_reassembler_feed(a->assembly, frame, len, a->interface, &m);
    if (r != EM_OK) {
        char label[48];
        EM_FORMAT_FIXED(label, sizeof(label), "rejected_%s", em_reason_name(r));
        count(&a->counts, label);
        return;
    }
    if (!m)
        return;
    if (m->relay) {
        count(&a->counts, "rejected_UNSUPPORTED_OPERATION");
    } else {
        handle_message(a, m);
    }
    em_message_free(m);
}

/* notifications: observed BSS changes and client joins/leaves (spec §2.4) */
static void notify(agent *a)
{
    char topo[512] = "";
    size_t off = 0;
    /* every BSS the agent shows, on any of its radios (spec 9) */
    em_topology_radio shown[1 + EXTRA_RADIOS];
    size_t nshown = shown_radios(a, shown);
    for (size_t r = 0; r < nshown; r++)
        for (size_t i = 0; i < shown[r].radio->nbss && off + 80 < sizeof(topo); i++) {
            char *mac = em_mac_str(shown[r].radio->bss[i].bssid);
            /* at most 17 + 32 + 2 characters: the loop leaves 80 */
            EM_FORMAT_FIXED(topo + off, sizeof(topo) - off, "%s/%s;", mac, shown[r].radio->bss[i].ssid);
            off += strlen(topo + off);
            free(mac);
        }
    if (a->has_backhaul && off + 40 < sizeof(topo)) { /* a moved backhaul is a topology change */
        char *parent = em_mac_str(a->backhaul.station.parent);
        EM_FORMAT_FIXED(topo + off, sizeof(topo) - off, "bh:%s;", parent);
        free(parent);
    }
    em_tlv al = {0x01, 6, a->al};
    /* and once after a kept reconnection (spec 2.5: the pod's uplink may have moved) */
    if (strcmp(topo, a->last_topology) || a->notify_due) {
        send_message(a, EM_MULTICAST, 0x0001, next_mid(a), &al, 1, true);
        EM_FORMAT_FIXED(a->last_topology, sizeof(a->last_topology), "%s", topo);
        a->notify_due = false;
        count(&a->counts, "observed_topology_notification");
    }
    if (em_reannounce_due(&a->reannounce, a->topology_responses)) {
        a->nclients = 0; /* announce every client again once the controller knows the BSS */
        count(&a->counts, "clients_reannounced");
    }
    /* joins, then leaves */
    for (size_t r = 0; r < nshown; r++)
        for (size_t i = 0; i < shown[r].radio->nbss; i++)
            for (size_t k = 0; k < shown[r].radio->bss[i].nstations; k++) {
                const uint8_t *bssid = shown[r].radio->bss[i].bssid, *mac = shown[r].radio->bss[i].stations[k];
                bool known = false;
                for (size_t j = 0; j < a->nclients && !known; j++)
                    known = !memcmp(a->clients[j].bssid, bssid, 6) && !memcmp(a->clients[j].mac, mac, 6);
                if (known || a->nclients == 256)
                    continue;
                uint8_t v[13];
                memcpy(v, mac, 6);
                memcpy(v + 6, bssid, 6);
                v[12] = 0x80;
                em_tlv tlvs[2] = {al, {0x92, 13, v}};
                send_message(a, EM_MULTICAST, 0x0001, next_mid(a), tlvs, 2, true);
                memcpy(a->clients[a->nclients].bssid, bssid, 6);
                memcpy(a->clients[a->nclients++].mac, mac, 6);
                count(&a->counts, "client_join_notification");
            }
    for (size_t j = 0; j < a->nclients;) {
        bool present = false;
        for (size_t r = 0; r < nshown && !present; r++)
            for (size_t i = 0; i < shown[r].radio->nbss && !present; i++)
                if (!memcmp(shown[r].radio->bss[i].bssid, a->clients[j].bssid, 6))
                    for (size_t k = 0; k < shown[r].radio->bss[i].nstations && !present; k++)
                        present = !memcmp(shown[r].radio->bss[i].stations[k], a->clients[j].mac, 6);
        if (present) {
            j++;
            continue;
        }
        uint8_t v[13];
        memcpy(v, a->clients[j].mac, 6);
        memcpy(v + 6, a->clients[j].bssid, 6);
        v[12] = 0x00;
        em_tlv tlvs[2] = {al, {0x92, 13, v}};
        send_message(a, EM_MULTICAST, 0x0001, next_mid(a), tlvs, 2, true);
        a->clients[j] = a->clients[--a->nclients];
        count(&a->counts, "client_leave_notification");
        count(&a->counts, "disassociation_statistics_unavailable");
    }
}

/* spec 2.5: the radios whose operable channel moved in a kept session: an unsolicited Channel
 * Preference Report naming their new channel, then the Operating Channel Report (once every
 * radio's operating parameters are measured; until then they stay due) */
static void report_channel_moves(agent *a)
{
    a->control.radio = &a->represented;
    a->control.tx_power_dbm = a->tx_power;
    a->control.max_eirp_dbm = a->max_eirp;
    control_radios(a);
    a->control.previous_mid = a->mid;
    em_frames out = {0};
    unsigned bound_before = a->control.bound_reported;
    em_reason r = em_control_moved(&a->control, (const uint8_t(*)[6])a->channel_moves, a->nchannel_moves, &out);
    a->mid = a->control.previous_mid;
    if (r != EM_OK) {
        em_frames_free(&out);
        return;
    }
    send_frames(a, &out);
    a->nchannel_moves = 0;
    count(&a->counts, "channel_preference_report_unsolicited");
    count(&a->counts, "operating_channel_report_sent");
    for (unsigned k = bound_before; k < a->control.bound_reported; k++)
        count(&a->counts, "operating_power_bound_reported");
}

static void session_tick(agent *a)
{
    double t = now();
    em_attempt_step step = em_attempts_tick(&a->at, t, source_current(a), a->generation);
    if (step.held)
        count(&a->counts, "reconnection_held");
    if (step.kept) { /* spec 2.5: its pod reconnected, the session kept: a Topology Notification */
        count(&a->counts, "reconnection_kept");
        a->notify_due = a->check_drift = true;
    }
    if (step.start)
        start_session(a);
    if (step.ended) {
        count(&a->counts, step.ended);
        if (step.window_passed)
            count(&a->counts, "reconnection_window_passed");
        end_session(a);
        return;
    }
    if (step.search)
        search(a);
    if (a->at.state == S_NONE || a->at.away) /* held: nothing that needs the pod (spec 2.5) */
        return;
    early_tick(a);
    if (a->bh_live || a->bh_shared.pending) { /* a started Backhaul Steering move is answered */
        em_frames out = {0};
        if (a->bh_live) {
            /* a success names the backhaul STA the pod is on now, as the uplink scope read it */
            const em_uplink_state *facts = a->uplink_on ? em_uplink_facts(&a->uplink) : NULL;
            uint8_t associated[6];
            bool known = facts && em_parse_mac(facts->mac, associated);
            em_bh_tick(&a->bh, t, source_current(a), known ? associated : NULL, &out);
        }
        send_frames(a, &out);
    }
    if (a->reporting_live) { /* the due-report schedule, in every admitted state */
        em_metric_source source = metric_source(a);
        em_frames out = {0};
        em_reporting_tick(&a->reporting, t, a->at.state == S_PROVISIONING, &source, next_mid_cb, a, &out);
        send_frames(a, &out);
    }
    if (a->at.state == S_PROVISIONING)
        next_radio(a); /* spec 9: the further radios' own M1s, in turn */
    if (a->at.state == S_PROVISIONING && a->nchannel_moves && source_current(a))
        report_channel_moves(a);
    if (a->at.state == S_PROVISIONING)
        notify(a);
}

/* -- status ---------------------------------------------------------------------------- */

static cJSON *pod_facts(agent *a)
{
    if (!source_current(a))
        return cJSON_CreateNull();
    cJSON *o = cJSON_CreateObject(), *bsses = cJSON_CreateArray(), *stations = cJSON_CreateArray();
    char *text;
    cJSON_AddStringToObject(o, "serial", a->view.serial);
    cJSON_AddStringToObject(o, "node_id", a->view.has_node_id ? a->view.node_id : "");
    cJSON_AddStringToObject(o, "firmware", a->view.has_firmware ? a->view.firmware : "");
    cJSON_AddStringToObject(o, "radio", a->represented.if_name);
    text = em_mac_str(a->represented.ruid);
    cJSON_AddStringToObject(o, "ruid", text);
    free(text);
    const em_bss_view *primary = a->has_primary ? &a->represented.bss[0] : NULL;
    if (primary) {
        text = em_mac_str(primary->bssid);
        cJSON_AddStringToObject(o, "bssid", text);
        free(text);
        cJSON_AddStringToObject(o, "ssid", primary->ssid);
    } else {
        cJSON_AddNullToObject(o, "bssid");
        cJSON_AddNullToObject(o, "ssid");
    }
    cJSON_AddNumberToObject(o, "channel", a->channel);
    /* the radio's channel as wm shows it, and whether the reported one differs */
    cJSON_AddItemToObject(o, "observed_channel",
                          a->has_observed ? cJSON_CreateNumber(a->observed_channel) : cJSON_CreateNull());
    cJSON_AddBoolToObject(o, "channel_mismatch", a->has_observed && a->observed_channel != a->channel);
    /* spec 9: every radio the agent binds and reports (the fronthaul first) */
    em_topology_radio shown[1 + EXTRA_RADIOS];
    size_t nshown = shown_radios(a, shown);
    cJSON *radios = cJSON_AddArrayToObject(o, "radios");
    for (size_t r = 0; r < nshown; r++) {
        cJSON *x = cJSON_CreateObject();
        text = em_mac_str(shown[r].radio->ruid);
        cJSON_AddStringToObject(x, "ruid", text);
        free(text);
        cJSON_AddStringToObject(x, "band", shown[r].radio->band);
        cJSON_AddNumberToObject(x, "channel", shown[r].channel);
        cJSON_AddItemToArray(radios, x);
    }
    for (size_t r = 0; r < nshown; r++)
        for (size_t i = 0; i < shown[r].radio->nbss; i++) {
            const em_bss_view *b = &shown[r].radio->bss[i];
            cJSON *x = cJSON_CreateObject();
            cJSON_AddStringToObject(x, "role", b->backhaul ? "backhaul" : "fronthaul");
            text = em_mac_str(b->bssid);
            cJSON_AddStringToObject(x, "bssid", text);
            free(text);
            cJSON_AddStringToObject(x, "ssid", b->ssid);
            cJSON_AddItemToArray(bsses, x);
            for (size_t k = 0; k < b->nstations; k++) {
                text = em_mac_str(b->stations[k]);
                cJSON_AddItemToArray(stations, cJSON_CreateString(text));
                free(text);
            }
        }
    cJSON_AddItemToObject(o, "bsses", bsses);
    cJSON_AddItemToObject(o, "stations", stations);
    if (a->has_backhaul) {
        cJSON *b = cJSON_AddObjectToObject(o, "backhaul");
        cJSON_AddStringToObject(b, "station", a->backhaul.station.if_name);
        text = em_mac_str(a->backhaul.station.mac);
        cJSON_AddStringToObject(b, "mac", text);
        free(text);
        text = em_mac_str(a->backhaul.station.parent);
        cJSON_AddStringToObject(b, "parent", text);
        free(text);
        cJSON_AddStringToObject(b, "band", a->backhaul.band);
        cJSON_AddNumberToObject(b, "channel", a->backhaul.channel);
    } else {
        cJSON_AddNullToObject(o, "backhaul");
    }
    cJSON *neighbors = cJSON_AddArrayToObject(o, "backhaul_neighbors");
    for (size_t i = 0; i < a->nlinks; i++) {
        cJSON *x = cJSON_CreateObject();
        text = em_mac_str(a->links[i].local);
        cJSON_AddStringToObject(x, "interface", text);
        free(text);
        text = em_mac_str(a->links[i].al);
        cJSON_AddStringToObject(x, "al", text);
        free(text);
        cJSON_AddItemToArray(neighbors, x);
    }
    cJSON_AddNumberToObject(o, "ovsdb_generation", a->generation);
    cJSON_AddNumberToObject(o, "ovsdb_revision", (double)em_ovsdb_revision(a->ovs));
    return o;
}

/* a journal's operations as the status gives them; a radio scope's with the SSIDs its slots map */
static void operations_json(em_journal *journal, cJSON *ops, bool radio)
{
    const cJSON *op;
    cJSON_ArrayForEach(op, em_journal_operations_view(journal))
    {
        cJSON *x = cJSON_CreateObject();
        const cJSON *intent = cJSON_GetObjectItemCaseSensitive(op, "intent");
        cJSON_AddItemToObject(x, "operation_id", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "operation_id"), 1));
        cJSON_AddItemToObject(x, "state", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "state"), 1));
        cJSON_AddItemToObject(x, "reason", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "reason"), 1));
        const cJSON *ssid = cJSON_GetObjectItemCaseSensitive(intent, "ssid");
        cJSON_AddItemToObject(x, "ssid", ssid ? cJSON_Duplicate(ssid, 1) : cJSON_CreateNull());
        cJSON_AddNumberToObject(x, "attempts", cJSON_GetArraySize(cJSON_GetObjectItemCaseSensitive(op, "attempts")));
        cJSON_AddItemToObject(x, "application_evidence",
                              cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "application_evidence"), 1));
        if (radio) {
            cJSON *bsses = cJSON_AddArrayToObject(x, "bsses");
            const cJSON *b;
            cJSON_ArrayForEach(b, cJSON_GetObjectItemCaseSensitive(intent, "additional"))
            {
                const char *s = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(b, "ssid"));
                cJSON_AddItemToArray(bsses, cJSON_CreateString(s ? s : ""));
            }
        }
        cJSON_AddItemToArray(ops, x);
    }
}

static cJSON *status(agent *a)
{
    cJSON *o = cJSON_CreateObject(), *session = cJSON_CreateObject(), *ops = cJSON_CreateArray();
    cJSON_AddStringToObject(o, "implementation", "c-lab-prototype");
    cJSON_AddStringToObject(o, "pod_id", a->pod_id);
    cJSON_AddStringToObject(o, "profile", a->profile.id);
    char *text = em_mac_str(a->al);
    cJSON_AddStringToObject(o, "agent_al", text);
    free(text);
    text = em_mac_str(a->controller);
    cJSON_AddStringToObject(o, "controller_al", text);
    free(text);
    cJSON_AddItemToObject(o, "pod", pod_facts(a));
    cJSON_AddBoolToObject(o, "report_source_available", source_current(a));
    if (a->pod_refused) /* spec 3.10: the pod's schema is not the profile's generation */
        cJSON_AddStringToObject(o, "pod_refused", "SCHEMA_MISMATCH");
    else
        cJSON_AddNullToObject(o, "pod_refused");
    /* spec 2.5: new generations that continued the pod source, and those that were another, by
     * reason */
    cJSON *reconnections = cJSON_AddObjectToObject(o, "reconnections");
    cJSON_AddNumberToObject(reconnections, "continued", a->reconnections_continued);
    if (a->reconnections_config_lost)
        cJSON_AddNumberToObject(reconnections, "config_lost", a->reconnections_config_lost);
    cJSON_AddItemToObject(reconnections, "fresh", counters_json(&a->reconnections_fresh, ""));
    if (a->profile.legacy) { /* spec 3.10: a legacy pod's starts as its agent saw them (its start
                              * markers, else its sessions) and the markers it wrote, */
        cJSON_AddNumberToObject(o, "session_starts", a->session_starts);
        cJSON *marker = cJSON_AddObjectToObject(o, "start_marker");
        cJSON_AddBoolToObject(marker, "on", em_start_marker_used());
        cJSON_AddNumberToObject(marker, "writes", a->start_writes);
        if (a->start_pending_since > 0)
            cJSON_AddNumberToObject(marker, "pending_since", a->start_pending_since);
        else
            cJSON_AddNullToObject(marker, "pending_since");
        if (a->start_error[0])
            cJSON_AddStringToObject(marker, "last_error", a->start_error);
        else
            cJSON_AddNullToObject(marker, "last_error");
        /* and the oftags restored after an AP's write (each the second full reload of its authenticator) */
        cJSON_AddNumberToObject(o, "auth_reloads", a->auth_reloads);
        cJSON_AddNumberToObject(o, "bridge_ports", a->bridge_ports); /* the APs put into their bridge */
        cJSON_AddNumberToObject(o, "bridge_mode_writes", a->bridge_modes); /* the pod's bridge mode written */
        /* the home APs' radios' own netdevs: the setting, the writes, and each such radio's port_state
         * as nm reports it (null: no row) */
        cJSON *phy = cJSON_AddObjectToObject(o, "phy_netdev_up");
        cJSON_AddBoolToObject(phy, "on", a->phy_netdev_up);
        cJSON_AddNumberToObject(phy, "writes", a->phy_writes);
        cJSON *ports = cJSON_AddObjectToObject(phy, "port_state");
        if (em_ovsdb_ready(a->ovs))
            em_phy_netdev_ports(&a->profile, a->multi_bss, em_ovsdb_tables(a->ovs), ports);
    }
    cJSON_AddStringToObject(session, "state", SESSION_NAMES[a->at.state]);
    cJSON *issues = cJSON_AddArrayToObject(session, "admission_issues");
    for (size_t i = 0; i < a->nadmission_issues; i++)
        cJSON_AddItemToArray(issues, cJSON_CreateString(a->admission_issues[i]));
    cJSON_AddItemToObject(session, "counts", counters_json(&a->counts, ""));
    cJSON *steering = cJSON_AddObjectToObject(session, "steering");
    cJSON_AddItemToObject(steering, "counts", counters_json(&a->counts, "client_steering_"));
    cJSON *recovery = cJSON_AddObjectToObject(session, "recovery");
    cJSON_AddNumberToObject(recovery, "attempts_started", a->at.starts);
    em_metric_source source = metric_source(a);
    cJSON_AddItemToObject(session, "reporting_policy",
                          a->reporting_live ? em_reporting_status(&a->reporting) : cJSON_CreateNull());
    cJSON_AddItemToObject(session, "ap_metrics",
                          a->reporting_live ? em_reporting_metrics_status(&a->reporting, &source) : cJSON_CreateNull());
    cJSON_AddItemToObject(session, "backhaul_steering", a->bh_live ? em_bh_status(&a->bh) : cJSON_CreateNull());
    cJSON *reports = cJSON_AddObjectToObject(session, "reports");
    cJSON *report_counts = em_early_counts(&a->early);
    if (a->topology_responses)
        cJSON_AddNumberToObject(report_counts, "topology_response_sent", a->topology_responses);
    cJSON_AddItemToObject(reports, "counts", report_counts);
    cJSON_AddBoolToObject(reports, "early_pending", a->early.pending);
    /* spec 9: each further radio's own exchange, in the order their M1s went */
    cJSON *exchanges = cJSON_AddArrayToObject(session, "radios");
    for (size_t k = 0; k < a->nradio_scopes; k++) {
        const radio_scope *rs = &a->radio_scopes[k];
        if (!rs->started)
            continue;
        cJSON *x = cJSON_CreateObject();
        text = em_mac_str(rs->ruid);
        cJSON_AddStringToObject(x, "ruid", text);
        free(text);
        cJSON_AddBoolToObject(x, "awaiting_m2", rs->awaiting);
        cJSON_AddBoolToObject(x, "closed", !rs->has_m1);
        cJSON_AddItemToArray(exchanges, x);
    }
    cJSON_AddItemToObject(o, "session", session);
    operations_json(a->journal, ops, false);
    cJSON_AddItemToObject(o, "operations", ops);
    /* spec 9: each further radio's scope, by band: its operations, each the BSSes of the radio's
     * own M2 set its slots map */
    cJSON *radio_operations = cJSON_AddObjectToObject(o, "radio_operations");
    for (size_t k = 0; k < a->nradio_scopes; k++)
        operations_json(a->radio_scopes[k].journal,
                        cJSON_AddArrayToObject(radio_operations, a->radio_scopes[k].band), true);
    if (a->uplink_on) {
        cJSON_AddItemToObject(o, "uplink", em_uplink_status(&a->uplink));
    } else if (a->wired_on) {
        cJSON_AddItemToObject(o, "uplink", em_wired_status(&a->wired));
    } else {
        cJSON *uplink = cJSON_AddObjectToObject(o, "uplink");
        cJSON_AddItemToObject(uplink, "station", a->profile.uplink_station ? cJSON_CreateString(a->profile.uplink_station)
                                                                           : cJSON_CreateNull());
        cJSON_AddStringToObject(uplink, "mode", "off");
    }
    cJSON *telemetry = cJSON_AddObjectToObject(o, "telemetry");
    if (a->telemetry_on) { /* {"mode", waiting, operation, subscribed} | the statistics' status */
        cJSON_AddStringToObject(telemetry, "mode", "mqtt");
        cJSON *setup = em_telemetry_status(&a->telemetry), *m;
        cJSON_ArrayForEach(m, setup) cJSON_AddItemToObject(telemetry, m->string, cJSON_Duplicate(m, 1));
        cJSON_Delete(setup);
        cJSON_AddBoolToObject(telemetry, "subscribed", em_mqtt_connected(a->mqtt));
        cJSON *stats = em_pod_stats_status(&a->stats);
        cJSON_ArrayForEach(m, stats) cJSON_AddItemToObject(telemetry, m->string, cJSON_Duplicate(m, 1));
        cJSON_Delete(stats);
    } else {
        cJSON_AddStringToObject(telemetry, "mode", "off");
    }
    /* spec 3.6: the pod's statistics rows, whoever wrote them (one that takes its radio off its
     * channel can delay its stations' associations); null while the pod is away */
    cJSON_AddItemToObject(o, "stats_config", source_current(a) && em_ovsdb_ready(a->ovs)
                                                 ? em_stats_config_status(em_ovsdb_tables(a->ovs))
                                                 : cJSON_CreateNull());
    cJSON *st = cJSON_AddObjectToObject(o, "steering");
    cJSON_AddStringToObject(st, "mode", a->steering_on ? "owm" : "off");
    if (a->steering_on) {
        cJSON *x = em_steering_status(&a->steering), *m;
        cJSON_ArrayForEach(m, x) cJSON_AddItemToObject(st, m->string, cJSON_Duplicate(m, 1));
        cJSON_Delete(x);
    }
    cJSON_AddItemToObject(o, "probe_watch", a->watch_on ? em_watch_status(&a->watch) : cJSON_CreateNull());
    cJSON *gre_parent = a->gre ? em_gre_status(a->gre) : cJSON_CreateNull();
    cJSON_AddItemToObject(o, "gre_parent", gre_parent);
    /* spec 8.3: each AP of the pod known, true while up (the fleet's children whose kept
     * backhaul target it is wait for it), its GRE parent APs among them */
    cJSON_AddItemToObject(o, "ap_up", ap_up_status(a, cJSON_GetObjectItemCaseSensitive(gre_parent, "ap_bssid")));
    cJSON_AddItemToObject(o, "gtp_station", a->gtp_on ? em_gtp_status(&a->gtp) : cJSON_CreateNull());
    cJSON_AddItemToObject(o, "radio_plan", a->plan_on ? em_plan_status(&a->plan) : cJSON_CreateNull());
    cJSON_AddItemToObject(o, "node_config", a->node_on ? em_node_status(&a->node) : cJSON_CreateNull());
    cJSON_AddNumberToObject(o, "writes", a->writes);
    cJSON_AddNumberToObject(o, "worker_pid", getpid());
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    cJSON_AddNumberToObject(o, "updated", (double)ts.tv_sec + (double)ts.tv_nsec / 1e9);
    return o;
}

static void write_status(agent *a)
{
    char summary[512];
    const cJSON *latest = em_journal_latest_view(a->journal);
    EM_FORMAT_FIXED(summary, sizeof(summary), "%s %d %zu %.32s", SESSION_NAMES[a->at.state], source_current(a),
             em_journal_count(a->journal), latest ? em_state_of(latest) : ""); /* a state: 32 at most here */
    bool changed = strcmp(summary, a->status_summary) != 0;
    if (changed)
        LOG("state: %s", summary);
    if (!changed && now() < a->status_written + 1)
        return;
    EM_FORMAT_FIXED(a->status_summary, sizeof(a->status_summary), "%s", summary);
    cJSON *s = status(a);
    /* the reference's layout (json.dumps indent=2, sorted keys): tools read either file alike */
    char *text = em_json_dumps(s, EM_JSON_INDENT2, true), path[512];
    if (!text) /* a pod's text that is not UTF-8: still a status, in cJSON's own layout */
        text = cJSON_Print(s);
    size_t n = strlen(text);
    text = em_realloc(text, n + 2);
    memcpy(text + n, "\n", 2);
    if (!em_format(path, sizeof(path), "%s/status.json", a->status_dir) || !em_write_file(path, text, false))
        LOG("status: not written to %s", a->status_dir);
    free(text);
    cJSON_Delete(s);
    a->status_written = now();
}

/* With a run directory (spec §6): the status is written there, and <state_dir>/status.json
 * is a link to it, so readers keep one path and the state directory is not written once a
 * second. The link is made again only when it names something else. */
static bool status_link(const agent *a)
{
    char target[EM_STATE_DIR_MAX + 16], link[EM_STATE_DIR_MAX + 16], tmp[EM_STATE_DIR_MAX + 24],
        now[EM_STATE_DIR_MAX + 16];
    EM_FORMAT_FIXED(target, sizeof(target), "%s/status.json", a->status_dir); /* EM_STATE_DIR_MAX */
    EM_FORMAT_FIXED(link, sizeof(link), "%s/status.json", a->state_dir);
    EM_FORMAT_FIXED(tmp, sizeof(tmp), "%s/status.json.link", a->state_dir);
    if (!em_mkdirs(a->status_dir, 0700))
        return false;
    ssize_t n = readlink(link, now, sizeof(now) - 1);
    if (n >= 0) {
        now[n] = 0;
        if (!strcmp(now, target))
            return true;
    }
    (void)unlink(tmp);
    return !symlink(target, tmp) && !rename(tmp, link);
}

/* -- configuration and main --------------------------------------------------------- */

static const char *cfg_str(const cJSON *c, const char *key)
{
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(c, key));
}

static bool configure(agent *a, const char *path, const char *profiles)
{
    char *text = em_read_file(path, 1 << 20, NULL);
    if (!text)
        return false;
    a->config = cJSON_Parse(text);
    free(text);
    const cJSON *c = a->config;
    /* the reference's contract (schemas/agent-config.schema.json), as validate("agent-config") */
    em_schema *schema = em_schema_load(em_schema_directory(), "agent-config");
    char where[256] = "";
    bool valid = schema && c && em_schema_valid(schema, c, where, sizeof(where));
    em_schema_free(schema);
    if (!valid) {
        FAIL("invalid agent-config contract: %s", schema ? where : "schema unavailable (EMOSA_SCHEMAS)");
        return false;
    }
    a->pod_id = cfg_str(c, "pod_id");
    a->run_id = cfg_str(c, "run_id") ? cfg_str(c, "run_id") : cfg_str(c, "pod_id");
    a->serial = cfg_str(c, "serial");
    a->interface = cfg_str(c, "interface");
    a->state_dir = cfg_str(c, "state_dir");
    a->status_dir = cfg_str(c, "run_dir") ? cfg_str(c, "run_dir") : a->state_dir;
    if (cfg_str(c, "run_dir")) { /* the fleet's other agents, beside this one (spec §8.5) */
        em_peers_init(&a->peers, cfg_str(c, "run_dir"), a->pod_id);
        a->peers_on = true;
    }
    if (a->state_dir)
        load_aps(a); /* the pod's APs seen by an earlier run */
    a->profile_id = cfg_str(c, "profile") ? cfg_str(c, "profile") : "opensync-lab-hwsim-6.6.1-v1";
    const char *set = cfg_str(c, "message_set"), *m2 = cfg_str(c, "m2_session");
    a->r1 = set && !strcmp(set, "r1");
    a->multi_bss = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(c, "multi_bss"));
    a->phy_netdev_up = !cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(c, "phy_netdev_up")); /* on by default */
    a->shared_session = m2 && !strcmp(m2, "shared");
    const cJSON *window = cJSON_GetObjectItemCaseSensitive(c, "topology_query_window");
    a->topology_query_window = cJSON_IsNumber(window) ? window->valuedouble : 0;
    a->underlay = cfg_str(c, "underlay");
    const char *steer = cfg_str(cJSON_GetObjectItemCaseSensitive(c, "steering"), "mode");
    a->steering_on = !steer || strcmp(steer, "off");
    /* telemetry: the pod publishes to the broker, the agent reads the local one */
    const cJSON *telemetry = cJSON_GetObjectItemCaseSensitive(c, "telemetry");
    const char *mode = cfg_str(telemetry, "mode");
    a->telemetry_on = mode && strcmp(mode, "off");
    if (a->telemetry_on) {
        em_reason why;
        if (strcmp(mode, "mqtt") ||
            !em_telemetry_intent_from(cfg_str(c, "pod_id"), cfg_str(c, "serial"), telemetry, &a->telemetry.intent, &why)) {
            FAIL("telemetry: mode mqtt needs a broker and valid intervals");
            return false;
        }
        const char *subscribe = cfg_str(telemetry, "subscribe") ? cfg_str(telemetry, "subscribe") : "127.0.0.1:1883";
        const char *colon = strrchr(subscribe, ':');
        if (!colon || colon == subscribe || (size_t)(colon - subscribe) >= sizeof(a->subscribe_host))
            return false;
        memcpy(a->subscribe_host, subscribe, (size_t)(colon - subscribe));
        a->subscribe_host[colon - subscribe] = 0;
        char *end;
        long port = strtol(colon + 1, &end, 10);
        if (end == colon + 1 || *end || port < 1 || port > 65535)
            return false;
        a->subscribe_port = (int)port;
        /* a statistic is current for three reporting periods after its publication */
        int publish = a->telemetry.intent.publish_interval ? a->telemetry.intent.publish_interval : 60;
        a->freshness = 3.0 * a->telemetry.intent.reporting_interval + publish;
    }
    if (!a->pod_id || !a->serial || !a->interface || !a->state_dir || strlen(a->state_dir) > EM_STATE_DIR_MAX ||
        !a->status_dir || strlen(a->status_dir) > EM_STATE_DIR_MAX ||
        !em_parse_mac(cfg_str(c, "al_mac") ? cfg_str(c, "al_mac") : "", a->al) ||
        !em_parse_mac(cfg_str(c, "controller_al") ? cfg_str(c, "controller_al") : "", a->controller))
        return false;
    char profile[1024];
    if (!em_format(profile, sizeof(profile), "%s/%s.json", profiles, a->profile_id) ||
        em_profile_load(profile, &a->profile) != EM_OK)
        return false;
    if (a->profile.legacy) {
        /* spec 3.10: the pod's starts marked in its database (the agent's own token too, for a
         * schema without Node_Config), this one first */
        if (!em_new_session_start()) {
            FAIL("no randomness for the pod's session start");
            return false;
        }
        em_use_start_marker(true);
        a->session_starts = 1;
        /* spec 3.10: a legacy pod has no Multi-AP station (Wifi_Credential_Config without bssid,
         * priority or enabled): a GRE child's pin is its station's parent; its Ethernet uplink is
         * bridged by the patch pair (spec 8.4); its Band_Steering_Clients has not steering's or
         * the watch's columns */
        const char *legacy_uplink = cfg_str(cJSON_GetObjectItemCaseSensitive(c, "uplink"), "mode");
        if (legacy_uplink && strcmp(legacy_uplink, "off") && strcmp(legacy_uplink, "ethernet") &&
            strcmp(legacy_uplink, "gre")) {
            FAIL("uplink: %s is not served on a legacy pod", legacy_uplink);
            return false;
        }
        a->steering_on = false;
    }
    a->watch_on = a->telemetry_on && !a->profile.legacy;
    /* the uplink: option 1, the pod's backhaul station on the EasyMesh backhaul BSS */
    const cJSON *uplink = cJSON_GetObjectItemCaseSensitive(c, "uplink");
    const char *umode = cfg_str(uplink, "mode");
    /* a GRE child (spec 8.6) uses the uplink scope too: mode "gre", no configured upstream */
    bool ugre = umode && !strcmp(umode, "gre");
    a->uplink_on = ugre || (umode && !strcmp(umode, "multi-ap"));
    if (a->uplink_on) {
        a->uplink.station = cfg_str(uplink, "station") ? cfg_str(uplink, "station") : a->profile.uplink_station;
        /* the pod's backhaul station on each band; a station on the radio of its BSSes keeps
         * that radio's channel (spec §8.3) */
        for (size_t i = 0; i < a->profile.nuplink_bands && i < EM_UPLINK_BANDS; i++) {
            a->uplink.bands[i] = a->profile.uplink_bands[i];
            a->uplink.band_stations[i] = a->profile.uplink_band_stations[i];
            a->uplink.nbands = i + 1;
        }
        a->uplink.fixed_band = a->profile.band;
        a->uplink.legacy = a->profile.legacy; /* the station's parent is the pin (spec 3.10) */
        /* a failed switch's hold, bounded (spec 8.3): absent, the scope's defaults */
        const cJSON *backoff = cJSON_GetObjectItemCaseSensitive(uplink, "hold_backoff");
        const cJSON *backoff_cap = cJSON_GetObjectItemCaseSensitive(uplink, "hold_backoff_cap");
        a->uplink.hold_backoff = cJSON_IsNumber(backoff) ? backoff->valuedouble : 0;
        a->uplink.hold_backoff_cap = cJSON_IsNumber(backoff_cap) ? backoff_cap->valuedouble : 0;
        /* a kept target on another fleet pod: absent, the scope's defaults */
        const cJSON *tries = cJSON_GetObjectItemCaseSensitive(uplink, "kept_tries");
        const cJSON *kept_backoff = cJSON_GetObjectItemCaseSensitive(uplink, "kept_backoff");
        const cJSON *wait_cap = cJSON_GetObjectItemCaseSensitive(uplink, "parent_wait_cap");
        const cJSON *rearms = cJSON_GetObjectItemCaseSensitive(uplink, "kept_rearms");
        a->uplink.kept_tries_max = cJSON_IsNumber(tries) ? tries->valuedouble : 0;
        a->uplink.kept_backoff = cJSON_IsNumber(kept_backoff) ? kept_backoff->valuedouble : 0;
        a->uplink.parent_wait_cap = cJSON_IsNumber(wait_cap) ? wait_cap->valuedouble : 0;
        a->uplink.kept_rearms_max = cJSON_IsNumber(rearms) ? rearms->valuedouble : 0;
        const char *bssid = cfg_str(uplink, "bssid");
        uint8_t mac[6];
        if (!a->uplink.station) {
            FAIL("uplink: a station (profile or configuration) is required");
            return false;
        }
        /* a GRE child is steered onto a parent by BSSID and has none configured; a Multi-AP pod
         * needs its upstream BSSID from the start (spec 8.6) */
        if (!ugre && (!bssid || !em_parse_mac(bssid, mac))) {
            FAIL("uplink: bssid (the upstream backhaul BSS) is required for a multi-ap uplink");
            return false;
        }
        const char *credentials = cfg_str(uplink, "credentials");
        a->uplink.fixed_credentials = credentials && !strcmp(credentials, "config");
        if (a->uplink.fixed_credentials) {
            if (!cfg_str(uplink, "ssid") || !cfg_str(uplink, "secret_ref")) {
                FAIL("uplink: config credentials need ssid and secret_ref");
                return false;
            }
            /* the schema counts characters: a 32-character SSID can be longer in bytes */
            if (!em_copy(a->uplink.fixed_ssid, sizeof(a->uplink.fixed_ssid), cfg_str(uplink, "ssid")) ||
                !em_copy(a->uplink.fixed_ref, sizeof(a->uplink.fixed_ref), cfg_str(uplink, "secret_ref"))) {
                FAIL("uplink: ssid longer than 32 octets or secret_ref too long");
                return false;
            }
        }
    }
    /* a wired pod: its Ethernet uplink port into the fronthaul's bridge (spec §8.4) */
    a->wired_on = umode && !strcmp(umode, "ethernet");
    if (a->wired_on) {
        em_reason why;
        const char *bridge = cfg_str(a->profile.fronthaul_vif, "bridge");
        if (!bridge) {
            FAIL("uplink: the profile's fronthaul has no bridge");
            return false;
        }
        if (!em_wired_intent_from(cfg_str(c, "pod_id"), uplink, bridge, &a->wired.intent, &why)) {
            FAIL("uplink: the port and the bridge must be interface names");
            return false;
        }
        /* a legacy pod's bridging is the patch pair from its WAN bridge (spec 8.4) */
        a->wired.legacy = a->profile.legacy;
        if (!em_copy(a->wired.wan_bridge, sizeof(a->wired.wan_bridge), a->profile.wan_bridge)) {
            FAIL("profile: wan_bridge must be an interface name");
            return false;
        }
    }
    /* spec 3.10, the radio plan: the APs the profile does not name off, its radio on its channel */
    a->plan_on = a->profile.radio_plan;
    if (a->plan_on) {
        em_reason why;
        if (!em_plan_intent_from(cfg_str(c, "pod_id"), &a->profile, &a->plan.intent, &why)) {
            FAIL("profile: radio_plan needs the profile's VIF names, band, channel and ht_mode");
            return false;
        }
        if (a->telemetry_on) {
            /* a converted pod talks only to EMOSA, its plan clears qm's broker and the statistics rows
             * on every start; the pod publishes nothing (a fleet's telemetry is ignored) */
            WARN("telemetry: off on a converted pod (radio_plan emosa)");
            a->telemetry_on = a->watch_on = false;
        }
    }
    /* spec 4, the pods' modules: their settings in Node_Config, their reports from Node_State */
    const cJSON *modules = cJSON_GetObjectItemCaseSensitive(c, "modules");
    a->node_on = cJSON_GetArraySize(modules) > 0;
    if (a->node_on) {
        em_reason why;
        if (!em_node_intent_from(cfg_str(c, "pod_id"), modules, &a->node, &why)) {
            FAIL("modules: one to %d modules of one to %d settings, names and values as the schema has them",
                 EM_NODE_MODULES, EM_NODE_SETTINGS);
            return false;
        }
    }
    /* spec 3.10, the GTP station: a legacy pod's core makes none, and its seed has none */
    const cJSON *bootstrap = cJSON_GetObjectItemCaseSensitive(uplink, "bootstrap");
    a->gtp_on = cJSON_IsObject(bootstrap);
    if (a->gtp_on) {
        em_reason why;
        const char *station = cfg_str(uplink, "station") ? cfg_str(uplink, "station") : a->profile.uplink_station;
        if (!a->profile.legacy) {
            FAIL("uplink: bootstrap is a legacy pod's (spec 3.10)");
            return false;
        }
        if (!station || !em_gtp_intent_from(cfg_str(c, "pod_id"), station, bootstrap, &a->gtp.intent, &why)) {
            FAIL("uplink: bootstrap needs a station, an SSID of 1-32 octets, a secret_ref and a BSSID if any");
            return false;
        }
        /* the station's radio: its band in the profile's stations, else the fronthaul's */
        const char *band = a->profile.band;
        for (size_t i = 0; i < a->profile.nuplink_bands && i < 3; i++)
            if (a->profile.uplink_band_stations[i] && !strcmp(a->profile.uplink_band_stations[i], station))
                band = a->profile.uplink_bands[i];
        if (!band || !em_copy(a->gtp.band, sizeof(a->gtp.band), band)) {
            FAIL("uplink: bootstrap: the station's band");
            return false;
        }
        /* the station's guard (spec 3.10): unassociated this long, removed; written again after
         * the back-off (the box shortens both); GtpStation's defaults otherwise */
        const cJSON *unassociated = cJSON_GetObjectItemCaseSensitive(bootstrap, "unassociated_seconds");
        const cJSON *retry = cJSON_GetObjectItemCaseSensitive(bootstrap, "retry_seconds"), *s;
        a->gtp.unassociated = 300;
        a->gtp.retry[0] = 900;
        a->gtp.retry[1] = 1800;
        a->gtp.retry[2] = 3600;
        a->gtp.nretry = 3;
        bool guard_ok = true;
        if (unassociated)
            guard_ok = em_json_int(unassociated, &a->gtp.unassociated) && a->gtp.unassociated >= 1;
        if (retry) {
            a->gtp.nretry = 0;
            guard_ok = guard_ok && cJSON_IsArray(retry) && cJSON_GetArraySize(retry) >= 1;
            cJSON_ArrayForEach(s, retry)
            {
                int seconds = 0;
                if (!em_json_int(s, &seconds) || seconds < 1 || a->gtp.nretry >= 3) {
                    guard_ok = false;
                    break;
                }
                a->gtp.retry[a->gtp.nretry++] = seconds;
            }
        }
        if (!guard_ok) {
            FAIL("uplink: bootstrap's unassociated_seconds and retry_seconds are positive seconds");
            return false;
        }
        /* a converted pod: every station row guarded (spec 3.10) */
        a->gtp.guard_all = a->plan_on;
        /* the radio plan keeps, while the pod is wired, only the stations on the GTP's SSID */
        if (a->plan_on)
            (void)em_copy(a->plan.intent.gtp_ssid, sizeof(a->plan.intent.gtp_ssid), a->gtp.intent.ssid); /* 32 */
    }
    /* a GRE parent (spec 8.6): a tunnel per child of its parent AP, in the fronthaul's bridge */
    const char *parent = NULL;
    for (size_t i = 0; a->multi_bss && i < a->profile.nslots; i++) /* the first by name, as min() */
        if (em_profile_parent_slot(&a->profile, a->profile.slots[i].if_name) &&
            (!parent || strcmp(a->profile.slots[i].if_name, parent) < 0))
            parent = a->profile.slots[i].if_name;
    /* a former GRE parent (its profile no longer one, its tunnels' journal kept): the tunnels left on
     * its former parent AP, its backhaul slot, are removed (spec 8.6, a parent demoted) */
    const char *former = NULL;
    for (size_t i = 0; !parent && a->multi_bss && i < a->profile.nslots; i++) {
        const char *name = a->profile.slots[i].if_name, *role = a->profile.slots[i].role;
        if (name && role && !strcmp(role, "backhaul") && (!former || strcmp(name, former) < 0))
            former = name;
    }
    char journal_dir[EM_STATE_DIR_MAX + 16];
    struct stat st;
    EM_FORMAT_FIXED(journal_dir, sizeof(journal_dir), "%s/gre-parent", a->state_dir);
    bool demoted = former && stat(journal_dir, &st) == 0 && S_ISDIR(st.st_mode);
    if ((parent && a->underlay) || demoted) {
        const char *bridge = cfg_str(a->profile.fronthaul_vif, "bridge");
        if (!bridge) {
            FAIL("gre parent: the profile's fronthaul has no bridge");
            return false;
        }
        a->gre = em_calloc(1, sizeof(*a->gre));
        /* a former parent only removes: no child address is checked against its underlay */
        const char *underlay = a->underlay ? a->underlay : "169.254.0.0/16";
        if (!em_gre_bind(a->gre, cfg_str(c, "pod_id"), demoted ? former : parent, bridge, underlay)) {
            FAIL("gre parent: the parent AP, the bridge or the underlay is not usable");
            return false;
        }
        a->gre->withdrawn = demoted;
    }
    /* AP metrics from the pod's statistics: telemetry with the survey and a declared ESP */
    a->pod_metrics_on = a->telemetry_on && a->telemetry.intent.survey && a->profile.has_esp_be;
    return true;
}

static void deliver(void *ctx, const char *topic, const uint8_t *payload, size_t len, bool retained)
{
    agent *a = ctx;
    em_pod_stats_receive(&a->stats, topic, payload, len, retained);
}

/* the uplink may switch: the pod serves the controller's fronthaul, nothing is being written on
 * any of its radios */
static bool fronthaul_settled(void *ctx)
{
    agent *a = ctx;
    return source_current(a) && a->has_primary && !writing(a);
}

static void boot_id(char out[129])
{
    FILE *f = fopen("/proc/sys/kernel/random/boot_id", "r");
    out[0] = 0;
    if (f) {
        if (fgets(out, 129, f))
            out[strcspn(out, "\n")] = 0;
        (void)fclose(f); /* read only */
    }
}

/* the pod a configuration file is for, as its unit names it: /etc/emosa/POD.json */
static void config_instance(const char *path, char out[64])
{
    const char *base = path ? strrchr(path, '/') : NULL;
    base = base ? base + 1 : path ? path : "";
    size_t n = strlen(base);
    if (n > 5 && !strcmp(base + n - 5, ".json"))
        n -= 5;
    if (n >= 64)
        n = 0; /* no plain pod name: the agents' shared log */
    memcpy(out, base, n);
    out[n] = 0;
}

int main(int argc, char **argv)
{
    em_init();
    const char *profiles = getenv("EMOSA_PROFILES") ? getenv("EMOSA_PROFILES") : EMOSA_PROFILES_DIR;
    const char *config = NULL;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--version")) {
            (void)printf("emosa-agent-c %s (%s)\n", EMOSA_VERSION, EMOSA_REVISION);
            return 0;
        }
        if (!strcmp(argv[i], "--profiles") && i + 1 < argc)
            profiles = argv[++i];
        else if (argv[i][0] != '-')
            config = argv[i];
    }
    char instance[64];
    config_instance(config, instance);
    em_log_open("agent", instance);
    static agent a;
    if (!config || !configure(&a, config, profiles)) {
        FAIL("configuration %s or its profile unusable", config ? config : "(none)");
        (void)fprintf(stderr, "usage: emosa-agent-c CONFIG.json [--profiles DIR] | --version\n");
        return 2;
    }
    mkdir(a.state_dir, 0700);
    if (a.status_dir != a.state_dir && !status_link(&a)) {
        FAIL("run directory %s unusable", a.status_dir);
        return 1;
    }
    /* the AP scope's secrets and journal (a second writer is refused: one agent per pod) */
    char dir[600];
    EM_FORMAT_FIXED(dir, sizeof(dir), "%s/secrets", a.state_dir);
    if (em_vault_open(&a.vault, dir) != EM_OK) {
        FAIL("secret directory %s unusable (it must be 0700)", dir);
        return 1;
    }
    const char *schemas = em_schema_directory();
    a.schemas = (em_journal_schemas){em_schema_load(schemas, "operation"), em_schema_load(schemas, "event"),
                                     em_schema_load(schemas, "wsc-receipt")};
    EM_FORMAT_FIXED(dir, sizeof(dir), "%s/journal", a.state_dir);
    em_reason opened;
    a.journal = em_journal_open(dir, &a.schemas, &opened);
    if (!a.journal) {
        FAIL("journal %s: %s", dir, em_reason_name(opened));
        return 1;
    }
    if (signal(SIGTERM, on_signal) == SIG_ERR || signal(SIGINT, on_signal) == SIG_ERR ||
        signal(SIGPIPE, SIG_IGN) == SIG_ERR) {
        FAIL("signal handlers not installed");
        return 1;
    }
    const char *tables[] = {"AWLAN_Node", "Wifi_Radio_Config", "Wifi_Radio_State",
        "Wifi_VIF_Config", "Wifi_VIF_State", "Wifi_Associated_Clients", "Wifi_Inet_Config",
        "Wifi_Credential_Config", "Connection_Manager_Uplink", "Band_Steering_Config",
        "Band_Steering_Clients", "Wifi_VIF_Neighbors", "Wifi_Stats_Config",
        /* a GRE parent's children and tunnels (spec 8.6), their columns only */
        "DHCP_leased_IP:hwaddr,inet_addr",
        /* (with each interface as nm reports it, its link running or not: spec 3.10, phy_netdev) */
        "Wifi_Inet_State:if_name,inet_addr,if_type,enabled,network",
        "Wifi_Master_State:if_name,if_type,port_state,network_state",
        /* and, read only, whether an AP's bridge port carries its stations (spec 3.10, BRIDGE_MONITOR):
         * the bridge's fail mode and OpenSync's OpenFlow rules, each Interface as Open vSwitch opened it */
        "Bridge:name,ports,fail_mode,controller,other_config,status", "Port:name,interfaces,tag,trunks,vlan_mode",
        "Interface:name,type,ofport,error,link_state,admin_state", "Openflow_Config:bridge,table,priority,rule,action,token",
        "Openflow_State:bridge,openflow_config,success,token",
        /* the pods' modules (spec 4), only with modules configured: the last two (a legacy pod's
         * Node_Config always, its start marker: spec 3.10) */
        "Node_Config:module,key,value,persist", "Node_State:module,key,value"};
    /* (a legacy pod's Node_Config always: its start marker, spec 3.10) */
    size_t ntables = sizeof(tables) / sizeof(*tables) - (a.node_on ? 0 : a.profile.legacy ? 1 : 2);
    a.ovs = em_ovsdb_open(cfg_str(a.config, "ovsdb"), tables, ntables);
    if (!a.ovs) {
        FAIL("cannot listen on %s", cfg_str(a.config, "ovsdb"));
        return 1;
    }
    a.ap = (em_ap_scope){.ovs = a.ovs,
                         .profile = &a.profile,
                         .serial = a.serial,
                         .pod_id = a.pod_id,
                         .multi_bss = a.multi_bss,
                         .underlay = a.underlay,
                         .vault = &a.vault,
                         .transact = transact,
                         .transact_ctx = &a};
    em_engine_init(&a.ap_engine, a.journal, &a.vault, a.pod_id, em_ap_backend(), &a.ap, now);
    a.ap_engine.mapping = a.profile.mapping; /* the AP scope plans under the pod's profile (spec §5) */
    em_engine_recover(&a.ap_engine);
    /* spec 9: each of the profile's further radios is its own scope, configured by its own M2,
     * with its own journal (radio-<band>, as the reference's) */
    for (size_t k = 0; k < a.profile.nradios && k < EXTRA_RADIOS; k++) {
        radio_scope *rs = &a.radio_scopes[a.nradio_scopes];
        rs->band = a.profile.radio_bands[k];
        rs->ap = a.ap;
        rs->ap.band = rs->band;
        EM_FORMAT_FIXED(dir, sizeof(dir), "%s/radio-%s", a.state_dir, rs->band);
        rs->journal = em_journal_open(dir, &a.schemas, &opened);
        if (!rs->journal) {
            FAIL("journal %s: %s", dir, em_reason_name(opened));
            return 1;
        }
        em_engine_init(&rs->engine, rs->journal, &a.vault, a.pod_id, em_ap_backend(), &rs->ap, now);
        rs->engine.mapping = a.profile.mapping;
        em_engine_recover(&rs->engine);
        a.nradio_scopes++;
    }
    char boot[129], path[700];
    boot_id(boot);
    EM_FORMAT_FIXED(path, sizeof(path), "%s/reporting-policy.sqlite", a.state_dir);
    a.policy_store = em_policy_store_open(path, boot);
    if (!a.policy_store)
        WARN("reporting policy store %s unavailable: policies are acknowledged, not kept", path);
    EM_FORMAT_FIXED(path, sizeof(path), "%s/channel-policy.sqlite", a.state_dir);
    if (!(a.channel_store = em_channel_store_open(path)))
        WARN("channel policy store %s unavailable: selections are answered, not kept", path);
    if (a.telemetry_on) {
        a.telemetry.ovs = a.ovs;
        a.telemetry.serial = a.serial;
        a.telemetry.run_id = a.run_id;
        a.telemetry.transact = transact;
        a.telemetry.transact_ctx = &a;
        em_reason why = em_telemetry_open(&a.telemetry, a.state_dir, &a.schemas, &a.vault, now);
        if (why != EM_OK) {
            FAIL("telemetry journal: %s", em_reason_name(why));
            return 1;
        }
        em_pod_stats_init(&a.stats, a.telemetry.intent.topic, (unsigned)a.telemetry.intent.reporting_interval,
                          stats_clock, NULL);
        char client[160];
        EM_FORMAT_FIXED(client, sizeof(client), "emosa-agent-%s", a.pod_id); /* pod_id: at most 64 (schema) */
        a.mqtt = em_mqtt_open(a.subscribe_host, a.subscribe_port, a.telemetry.intent.topic, client, deliver, &a);
    }
    if (a.watch_on) {
        /* the stations the controller asks about, watched for their probe requests (§3.9) */
        a.watch = (em_watch_scope){.ovs = a.ovs, .serial = a.serial, .pod_id = a.pod_id, .run_id = a.run_id,
                                   .if_name = a.profile.fronthaul_if, .band = a.telemetry.intent.radio_type,
                                   .transact = transact, .transact_ctx = &a, .monotonic = now};
        em_reason why = em_watch_open(&a.watch, a.state_dir, &a.schemas, &a.vault);
        if (why != EM_OK) {
            FAIL("probe watch journal: %s", em_reason_name(why));
            return 1;
        }
    }
    if (a.steering_on) {
        a.steering = (em_steering_scope){.ovs = a.ovs, .serial = a.serial, .pod_id = a.pod_id, .run_id = a.run_id,
                                         .transact = transact, .transact_ctx = &a, .monotonic = now};
        em_reason why = em_steering_scope_open(&a.steering, a.state_dir, &a.schemas, &a.vault);
        if (why != EM_OK) {
            FAIL("steering journal: %s", em_reason_name(why));
            return 1;
        }
    }
    if (a.uplink_on) {
        a.uplink.ovs = a.ovs;
        a.uplink.serial = a.serial;
        a.uplink.pod_id = a.pod_id;
        a.uplink.run_id = a.run_id;
        a.uplink.vault = &a.vault;
        a.uplink.transact = transact;
        a.uplink.transact_ctx = &a;
        a.uplink.monotonic = now;
        a.uplink.ap_journal = a.journal;
        for (size_t k = 0; k < a.nradio_scopes && k < 2; k++) /* spec 9: a further radio's set */
            a.uplink.radio_journals[a.uplink.nradio_journals++] = a.radio_scopes[k].journal;
        a.uplink.settled = fronthaul_settled;
        a.uplink.settled_ctx = &a;
        a.uplink.loops = a.peers_on ? upstream_loops : NULL;
        a.uplink.loops_ctx = &a;
        a.uplink.is_parent = a.peers_on ? upstream_is_parent : NULL; /* spec 8.6 fleet parent set */
        a.uplink.is_parent_ctx = &a;
        a.uplink.ap_state = a.peers_on ? upstream_ap_state : NULL; /* spec 8.3 parent's AP */
        a.uplink.ap_state_ctx = &a;
        const char *ubssid = cfg_str(cJSON_GetObjectItemCaseSensitive(a.config, "uplink"), "bssid");
        em_reason why = em_uplink_open(&a.uplink, a.state_dir, &a.schemas, ubssid ? ubssid : "");
        if (why != EM_OK) {
            FAIL("uplink journal: %s", em_reason_name(why));
            return 1;
        }
    }
    if (a.gre) {
        a.gre->ovs = a.ovs;
        a.gre->serial = a.serial;
        a.gre->run_id = a.run_id;
        a.gre->transact = transact;
        a.gre->transact_ctx = &a;
        em_reason why = em_gre_open(a.gre, a.state_dir, &a.schemas, &a.vault, now);
        if (why != EM_OK) {
            FAIL("gre parent journal: %s", em_reason_name(why));
            return 1;
        }
    }
    if (a.wired_on) {
        a.wired.ovs = a.ovs;
        a.wired.serial = a.serial;
        a.wired.run_id = a.run_id;
        a.wired.transact = transact;
        a.wired.transact_ctx = &a;
        em_reason why = em_wired_open(&a.wired, a.state_dir, &a.schemas, &a.vault, now);
        if (why != EM_OK) {
            FAIL("wired uplink journal: %s", em_reason_name(why));
            return 1;
        }
    }
    if (a.gtp_on) {
        a.gtp.ovs = a.ovs;
        a.gtp.serial = a.serial;
        a.gtp.run_id = a.run_id;
        a.gtp.vault = &a.vault;
        a.gtp.transact = transact;
        a.gtp.transact_ctx = &a;
        em_reason why = em_gtp_open(&a.gtp, a.state_dir, &a.schemas, now);
        if (why != EM_OK) {
            FAIL("gtp station journal: %s", em_reason_name(why));
            return 1;
        }
    }
    if (a.plan_on) {
        a.plan.ovs = a.ovs;
        a.plan.serial = a.serial;
        a.plan.run_id = a.run_id;
        a.plan.transact = transact;
        a.plan.transact_ctx = &a;
        em_reason why = em_plan_open(&a.plan, a.state_dir, &a.schemas, &a.vault, now);
        if (why != EM_OK) {
            FAIL("radio plan journal: %s", em_reason_name(why));
            return 1;
        }
    }
    if (a.node_on) {
        a.node.ovs = a.ovs;
        a.node.serial = a.serial;
        a.node.run_id = a.run_id;
        a.node.transact = transact;
        a.node.transact_ctx = &a;
        em_reason why = em_node_open(&a.node, a.state_dir, &a.schemas, &a.vault, now);
        if (why != EM_OK) {
            FAIL("node config journal: %s", em_reason_name(why));
            return 1;
        }
    }
    /* EMOSA_NETNS (/etc/default/emosa): the agents' interfaces in a namespace of their own */
    if (em_ethernet_open_in(&a.eth, getenv("EMOSA_NETNS"), a.interface, a.al) != EM_OK) {
        FAIL("cannot open the 1905 interface %s with MAC %s", a.interface, cfg_str(a.config, "al_mac"));
        return 1;
    }
    uint16_t seed;
    RAND_bytes((uint8_t *)&seed, sizeof(seed));
    a.mid = seed;
    a.tokens = 32;
    a.token_time = now();
    em_renew_init(&a.renewals, a.token_time, a.topology_query_window);
    a.at.window = RECONNECT_WINDOW;
    LOG("EMOSA C agent %s (%s) for %s: AL %s, controller %s, OVSDB %s, %s", EMOSA_VERSION, EMOSA_REVISION, a.pod_id,
        cfg_str(a.config, "al_mac"), cfg_str(a.config, "controller_al"), cfg_str(a.config, "ovsdb"),
        a.r1 ? "r1" : "easymesh-6.1");
    double next_refresh = 0;
    uint8_t frame[1600];
    while (!stopping) {
        int fds[4];
        size_t n = em_ovsdb_fds(a.ovs, fds, 3);
        struct pollfd p[6];
        for (size_t i = 0; i < n; i++)
            p[i] = (struct pollfd){fds[i], POLLIN, 0};
        p[n++] = (struct pollfd){a.eth.fd, POLLIN, 0};
        short events;
        int mfd = a.mqtt ? em_mqtt_fd(a.mqtt, &events) : -1;
        if (mfd >= 0)
            p[n++] = (struct pollfd){mfd, events, 0};
        poll(p, n, 200);
        if (a.mqtt)
            em_mqtt_pump(a.mqtt, now());
        if (!em_ovsdb_pump(a.ovs))
            WARN("pod OVSDB connection lost");
        double t = now();
        bool refreshed = t >= next_refresh;
        if (refreshed) {
            if (next_refresh && t - next_refresh > REFRESH_PERIOD)
                WARN("pod state refresh %.1f s late", t - next_refresh);
            if (!refresh(&a))
                a.available = a.available && t < a.refreshed_at + LEASE;
            next_refresh = t + REFRESH_PERIOD; /* from the start, as the lease counts */
        }
        session_tick(&a);
        if (t >= a.next_discovery) {
            em_tlv tlvs[2] = {{0x01, 6, a.al}, {0x02, 6, a.al}};
            send_message(&a, EM_MULTICAST, 0x0000, next_mid(&a), tlvs, 2, false);
            a.next_discovery = t + DISCOVERY_PERIOD;
        }
        bool unserved = a.at.state == S_PROVISIONING && source_current(&a) && !a.has_primary && !active_op(&a);
        const char *reasons[4];
        /* an M1 out without its M2: the fronthaul's, or a further radio's (spec 9) */
        bool awaiting = a.at.state == S_AWAITING_M2 || (a.at.state == S_PROVISIONING && radio_awaiting(&a));
        size_t nreasons = em_renew_check(&a.renewals, t, unserved, awaiting, a.at.state == S_PROVISIONING, reasons);
        for (size_t i = 0; i < nreasons; i++)
            renew(&a, !strcmp(reasons[i], "unserved") ? "provisioned, but the pod serves no BSS: fresh M1"
                      : !strcmp(reasons[i], "no_m2") ? "no M2 for 30s after M1: fresh attempt"
                      : !strcmp(reasons[i], "no_topology_query")
                          ? "provisioned, no Topology Query in topology_query_window: fresh attempt"
                          : "no message from the controller for 130s: fresh attempt");
        size_t len;
        while ((len = em_ethernet_receive(&a.eth, frame, sizeof(frame))) > 0)
            receive_frame(&a, frame, len);
        /* spec 3.10: a legacy pod whose start is not marked yet (the refresh writes it) is no
         * scope's to read: reconcile and the scopes wait for the marker */
        bool marked = !(em_ovsdb_ready(a.ovs) && em_start_pending(em_ovsdb_tables(a.ovs)));
        if (refreshed && marked && source_current(&a))
            reconcile(&a);
        if (refreshed && source_current(&a) && a.profile.legacy && !writing(&a)) {
            bridge_port(&a); /* spec 3.10: an AP of EMOSA's into its bridge, its oftag marked */
            bridge_mode(&a); /* spec 3.10: the converted pod's bridge mode, its home APs' oftags marked */
            phy_netdev(&a);  /* spec 3.10: the home APs' radios' own netdevs up through nm */
            auth_reload(&a); /* spec 3.10: an AP's write taken by wm, its oftag back (a full reload) */
        }
        /* the scopes step on the refresh cadence, also while the pod is away: a write
         * in flight times out */
        bool step = refreshed && marked;
        if (step && a.uplink_on)
            em_uplink_tick(&a.uplink);
        if (step && a.wired_on)
            em_wired_tick(&a.wired);
        if (step && a.gtp_on)
            em_gtp_tick(&a.gtp);
        if (step && a.plan_on)
            em_plan_tick(&a.plan);
        if (step && a.node_on)
            em_node_tick(&a.node);
        if (step && a.gre)
            em_gre_tick(a.gre);
        if (step && a.telemetry_on)
            em_telemetry_tick(&a.telemetry);
        if (step && a.steering_on)
            em_steering_tick(&a.steering);
        if (step && a.watch_on)
            em_watch_tick(&a.watch);
        write_status(&a);
    }
    write_status(&a);
    end_radio_exchanges(&a);
    em_engine_free(&a.ap_engine);
    em_journal_close(a.journal);
    for (size_t k = 0; k < a.nradio_scopes; k++) {
        em_engine_free(&a.radio_scopes[k].engine);
        em_journal_close(a.radio_scopes[k].journal);
    }
    if (a.telemetry_on) {
        em_mqtt_close(a.mqtt);
        em_telemetry_close(&a.telemetry);
    }
    if (a.watch_on)
        em_watch_close(&a.watch);
    if (a.steering_on)
        em_steering_scope_close(&a.steering);
    if (a.uplink_on)
        em_uplink_close(&a.uplink);
    if (a.wired_on)
        em_wired_close(&a.wired);
    if (a.gtp_on)
        em_gtp_close(&a.gtp);
    if (a.plan_on)
        em_plan_close(&a.plan);
    if (a.node_on)
        em_node_close(&a.node);
    if (a.gre) {
        em_gre_close(a.gre);
        free(a.gre);
    }
    em_policy_store_close(a.policy_store);
    em_channel_store_close(a.channel_store);
    em_ethernet_close(&a.eth);
    em_ovsdb_close(a.ovs);
    em_peers_free(&a.peers);
    LOG("stopped");
    return 0;
}
