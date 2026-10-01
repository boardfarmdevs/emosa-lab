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
#include "jschema.h"
#include "mqtt.h"
#include "ovs.h"
#include "ovsdb.h"
#include "reporting.h"
#include "scope.h"
#include "scope_ap.h"
#include "scope_steering.h"
#include "scope_telemetry.h"
#include "scope_uplink.h"
#include "scope_watch.h"
#include "stats.h"
#include "southbound.h"
#include "vault.h"
#include "view.h"
#include "wsc.h"

#define REFRESH_PERIOD 0.5
#define LEASE 1.5
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

static void logf_(const char *level, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
static void logf_(const char *level, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    (void)fprintf(stderr, "%s emosa.agent: ", level);
    (void)vfprintf(stderr, fmt, ap);
    (void)fputc('\n', stderr);
    va_end(ap);
}
#define LOG(...) logf_("INFO", __VA_ARGS__)
#define WARN(...) logf_("WARNING", __VA_ARGS__)

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


typedef struct {
    /* configuration */
    cJSON *config;
    const char *pod_id, *serial, *interface, *state_dir, *profile_id;
    uint8_t al[6], controller[6];
    bool r1, multi_bss, shared_session, steering_on;
    em_profile profile;
    /* I/O */
    em_ovsdb *ovs;
    em_ethernet eth;
    uint16_t mid;
    /* the report source */
    bool available, caps_fixed;
    double refreshed_at;
    int generation;
    unsigned long revision; /* the pod database's, at the last refresh */
    em_device_view view;
    em_radio_view represented; /* the radio with only the BSSes the agent represents */
    bool has_primary;
    int channel, tx_power;
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
    em_bh_shared bh_shared;     /* Backhaul Steering under way: the agent's, across sessions */
    em_bh_coordinator bh;
    bool bh_live;
    em_backhaul backhaul;       /* the pod's EasyMesh backhaul, when it is one */
    bool has_backhaul;
    em_early early; /* the Early AP Capability Report awaiting its Ack */
    int early_generation; /* the pod State it was built from */
    unsigned long early_revision;
    char status_summary[512];
    double status_written;
} agent;

static uint16_t next_mid(agent *a) { return ++a->mid; }
static uint16_t next_mid_cb(void *ctx) { return next_mid(ctx); }

/* what an AP Metrics Response is built from (spec §3.8) */
static em_metric_source metric_source(agent *a)
{
    return (em_metric_source){&a->represented, a->pod_metrics_on ? &a->stats : NULL, a->profile.esp_be,
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

static bool wpa2_psk_row(const cJSON *row)
{
    const char *akm[8];
    size_t n = ovs_strings(row, "wpa_key_mgmt", akm, 8);
    return ovs_true(row, "wpa") && n == 1 && !strcmp(akm[0], "wpa-psk") &&
           ovs_true(row, "rsn_pairwise_ccmp") && ovs_map_size(row, "security") == 0 &&
           !ovs_true(row, "wpa_pairwise_tkip") && !ovs_true(row, "wpa_pairwise_ccmp") &&
           ovs_map_size(row, "wpa_psks") == 1;
}

static bool refresh(agent *a)
{
    const cJSON *tables = em_ovsdb_tables(a->ovs), *row;
    if (!em_ovsdb_ready(a->ovs))
        return false;
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
            if (radio->enabled && st && wpa2_psk_row(st) && radio->has_channel) {
                rep.bss[rep.nbss++] = *b;
                primary = true;
            }
        }
    }
    for (size_t k = 0; a->multi_bss && k < a->profile.nslots; k++)
        for (size_t i = 0; i < radio->nbss; i++)
            if (!strcmp(radio->bss[i].if_name, a->profile.slots[k].if_name))
                rep.bss[rep.nbss++] = radio->bss[i];
    int channel = primary ? radio->channel : a->profile.channel;
    int8_t max_eirp = a->caps_fixed ? a->max_eirp
                      : (radio->has_tx_power && radio->tx_power > 0 && radio->tx_power <= 127)
                          ? (int8_t)radio->tx_power : 20;
    em_tlv_list caps;
    uint8_t max_bss = (uint8_t)(1 + (a->multi_bss ? a->profile.nslots : 0));
    if (em_capability_tlvs(radio, channel, max_bss, max_eirp, &caps) != EM_OK) {
        free(view);
        return false;
    }
    if (!a->caps_fixed) {
        a->caps = caps;
        a->max_eirp = max_eirp;
        em_inventory_tlv(view, radio, "mac80211_hwsim", &a->inventory);
        a->caps_fixed = true;
    } else {
        bool same = caps.count == a->caps.count;
        for (size_t i = 0; same && i < caps.count; i++)
            same = caps.tlvs[i].kind == a->caps.tlvs[i].kind && caps.tlvs[i].len == a->caps.tlvs[i].len &&
                   !memcmp(caps.tlvs[i].value, a->caps.tlvs[i].value, caps.tlvs[i].len);
        em_tlv_list_free(&caps);
        if (!same) {
            WARN("pod source unavailable: pod radio identity or channel changed");
            free(view);
            return false;
        }
    }
    /* station ages: since EMOSA first saw each one */
    double t = now();
    size_t kept = 0;
    struct first_seen seen[256];
    for (size_t i = 0; i < rep.nbss; i++)
        for (size_t k = 0; k < rep.bss[i].nstations && kept < 256; k++) {
            double at = t;
            for (size_t j = 0; j < a->nfirst; j++)
                if (!memcmp(a->first_seen[j].mac, rep.bss[i].stations[k], 6))
                    at = a->first_seen[j].at;
            memcpy(seen[kept].mac, rep.bss[i].stations[k], 6);
            seen[kept++].at = at;
        }
    memcpy(a->first_seen, seen, kept * sizeof(seen[0]));
    a->nfirst = kept;
    /* the backhaul station, reported while it is the pod's EasyMesh backhaul (option 1) */
    a->has_backhaul = false;
    if (a->uplink_on) {
        em_uplink_state state;
        em_uplink_state_of(tables, a->uplink.station, &state);
        a->has_backhaul = !strcmp(state.kind, "multi-ap") && em_view_backhaul(view, a->uplink.station, &a->backhaul);
    }
    a->view = *view;
    free(view);
    a->represented = rep;
    a->has_primary = primary;
    a->channel = channel;
    a->tx_power = radio->has_tx_power ? radio->tx_power : 0;
    a->generation = em_ovsdb_generation(a->ovs);
    a->revision = em_ovsdb_revision(a->ovs);
    a->refreshed_at = t;
    a->available = true;
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
    return em_topology_tlvs(a->al, a->controller, &a->represented, a->channel, ages, a->nfirst, r1,
                            a->has_backhaul ? &a->backhaul : NULL, out);
}

/* -- operations (the AP scope: emosa.reconcile.Engine over PodBackend) ----------------- */

/* the pod has an operation in an active state (the fronthaul write in flight) */
static bool active_op(agent *a)
{
    cJSON *ops = em_journal_operations(a->journal, NULL), *op;
    bool active = false;
    cJSON_ArrayForEach(op, ops) active = active || em_state_active(em_state_of(op));
    cJSON_Delete(ops);
    return active;
}

static void reconcile(agent *a)
{
    if (em_journal_count(a->journal))
        em_engine_reconcile(&a->ap_engine);
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
    for (size_t i = 0, index = 1; i < m2->count && a->multi_bss; i++) {
        if (i == primary || nrefs == 8)
            continue;
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

static const char *bh_executor(void *ctx, const char *bssid)
{
    agent *a = ctx;
    return em_uplink_steer(&a->uplink, bssid);
}

static int bh_outcome(void *ctx, const char *bssid, const char **why)
{
    agent *a = ctx;
    return em_uplink_steering_outcome(&a->uplink, bssid, why);
}

/* -- the onboarding session (spec §2.5) ---------------------------------------------- */

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

static em_radio_caps radio_caps(agent *a)
{
    em_radio_caps r = {0};
    const em_tlv *basic = &a->caps.tlvs[1]; /* 0x85 */
    memcpy(r.ruid, basic->value, 6);
    r.max_bss = basic->value[6];
    r.nclasses = 1;
    r.classes[0].operating_class = basic->value[8];
    r.classes[0].max_eirp_dbm = (int8_t)basic->value[9];
    r.classes[0].nnon_operable = basic->value[10];
    memcpy(r.classes[0].non_operable, basic->value + 11, basic->value[10]);
    return r;
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
    cJSON_AddArrayToObject(record, "preferences");
    cJSON_AddNullToObject(record, "power_limit_dbm");
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
    em_reporting_start(&a->reporting, a->policy_store, a->controller, a->al, a->represented.ruid);
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
        em_binding b = a->control.binding;
        em_radio_caps caps = radio_caps(a);
        em_m2_result result;
        em_reason r = em_receive_m2(&b, &caps, &a->m1, m, a->multi_bss, a->shared_session, &result);
        if (r != EM_OK || result.teardown) {
            LOG("M2 rejected: %s", r != EM_OK ? em_reason_name(r) : "teardown");
            count(&a->counts, "wsc_rejected");
            return;
        }
        const char *outcome = operate(a, m, &result);
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
    a->control.watch = a->telemetry_on ? watch_ask : NULL;
    a->control.watch_ctx = a;
    a->control.radio = &a->represented;
    a->control.tx_power_dbm = a->tx_power;
    a->control.max_eirp_dbm = a->max_eirp;
    a->control.previous_mid = a->mid;
    a->control.executor = start_steering;
    a->control.executor_ctx = a;
    em_frames out = {0};
    em_reason error;
    const char *result = em_control_handle(&a->control, m, &out, &error);
    a->mid = a->control.previous_mid;
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
        /* Backhaul STA Radio Capabilities (RUID, MAC-included flag, STA MAC) of the station
         * that is the pod's EasyMesh backhaul; none while its uplink is GRE */
        uint8_t v[13];
        size_t n = 0;
        if (a->has_backhaul) {
            memcpy(v, a->backhaul.ruid, 6);
            v[6] = 0x80;
            memcpy(v + 7, a->backhaul.station.mac, 6);
            n = 1;
        }
        em_tlv t = {0xCB, 13, v};
        reply(a, 0x8028, m->mid, n ? &t : NULL, n);
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
        uint8_t response[13];
        memcpy(response, request->value, 12);
        response[12] = 0x01; /* failure: EMOSA does not move the pod's backhaul */
        em_tlv t = {0x9F, 13, response};
        reply(a, 0x8000, m->mid, NULL, 0);
        reply(a, 0x801A, m->mid, &t, 1);
        count(&a->counts, "backhaul_steering_refused");
    } else if (m->message_type == 0x0005) {
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
    for (size_t i = 0; i < a->represented.nbss && off + 80 < sizeof(topo); i++) {
        char *mac = em_mac_str(a->represented.bss[i].bssid);
        /* at most 17 + 32 + 2 characters: the loop leaves 80 */
        EM_FORMAT_FIXED(topo + off, sizeof(topo) - off, "%s/%s;", mac, a->represented.bss[i].ssid);
        off += strlen(topo + off);
        free(mac);
    }
    if (a->has_backhaul && off + 40 < sizeof(topo)) { /* a moved backhaul is a topology change */
        char *parent = em_mac_str(a->backhaul.station.parent);
        EM_FORMAT_FIXED(topo + off, sizeof(topo) - off, "bh:%s;", parent);
        free(parent);
    }
    em_tlv al = {0x01, 6, a->al};
    if (strcmp(topo, a->last_topology)) {
        send_message(a, EM_MULTICAST, 0x0001, next_mid(a), &al, 1, true);
        EM_FORMAT_FIXED(a->last_topology, sizeof(a->last_topology), "%s", topo);
        count(&a->counts, "observed_topology_notification");
    }
    if (em_reannounce_due(&a->reannounce, a->topology_responses)) {
        a->nclients = 0; /* announce every client again once the controller knows the BSS */
        count(&a->counts, "clients_reannounced");
    }
    /* joins, then leaves */
    for (size_t i = 0; i < a->represented.nbss; i++)
        for (size_t k = 0; k < a->represented.bss[i].nstations; k++) {
            const uint8_t *bssid = a->represented.bss[i].bssid, *mac = a->represented.bss[i].stations[k];
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
        for (size_t i = 0; i < a->represented.nbss && !present; i++)
            if (!memcmp(a->represented.bss[i].bssid, a->clients[j].bssid, 6))
                for (size_t k = 0; k < a->represented.bss[i].nstations && !present; k++)
                    present = !memcmp(a->represented.bss[i].stations[k], a->clients[j].mac, 6);
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

static void session_tick(agent *a)
{
    double t = now();
    em_attempt_step step = em_attempts_tick(&a->at, t, source_current(a), a->generation);
    if (step.start)
        start_session(a);
    if (step.ended) {
        count(&a->counts, step.ended);
        end_session(a);
        return;
    }
    if (step.search)
        search(a);
    if (a->at.state == S_NONE)
        return;
    early_tick(a);
    if (a->bh_live || a->bh_shared.pending) { /* a started Backhaul Steering move is answered */
        em_frames out = {0};
        if (a->bh_live)
            em_bh_tick(&a->bh, t, source_current(a), &out);
        send_frames(a, &out);
    }
    if (a->reporting_live) { /* the due-report schedule, in every admitted state */
        em_metric_source source = metric_source(a);
        em_frames out = {0};
        em_reporting_tick(&a->reporting, t, a->at.state == S_PROVISIONING, &source, next_mid_cb, a, &out);
        send_frames(a, &out);
    }
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
    for (size_t i = 0; i < a->represented.nbss; i++) {
        const em_bss_view *b = &a->represented.bss[i];
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
    cJSON_AddNumberToObject(o, "ovsdb_generation", a->generation);
    cJSON_AddNumberToObject(o, "ovsdb_revision", (double)em_ovsdb_revision(a->ovs));
    return o;
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
    cJSON_AddItemToObject(o, "session", session);
    cJSON *journal = em_journal_operations(a->journal, NULL), *op;
    cJSON_ArrayForEach(op, journal)
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
        cJSON_AddItemToArray(ops, x);
    }
    cJSON_Delete(journal);
    cJSON_AddItemToObject(o, "operations", ops);
    if (a->uplink_on) {
        cJSON_AddItemToObject(o, "uplink", em_uplink_status(&a->uplink));
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
    cJSON *st = cJSON_AddObjectToObject(o, "steering");
    cJSON_AddStringToObject(st, "mode", a->steering_on ? "owm" : "off");
    if (a->steering_on) {
        cJSON *x = em_steering_status(&a->steering), *m;
        cJSON_ArrayForEach(m, x) cJSON_AddItemToObject(st, m->string, cJSON_Duplicate(m, 1));
        cJSON_Delete(x);
    }
    cJSON_AddItemToObject(o, "probe_watch", a->telemetry_on ? em_watch_status(&a->watch) : cJSON_CreateNull());
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
    cJSON *latest = em_journal_latest(a->journal);
    EM_FORMAT_FIXED(summary, sizeof(summary), "%s %d %zu %s", SESSION_NAMES[a->at.state], source_current(a),
             em_journal_count(a->journal), latest ? em_state_of(latest) : "");
    cJSON_Delete(latest);
    bool changed = strcmp(summary, a->status_summary) != 0;
    if (changed)
        LOG("state: %s", summary);
    if (!changed && now() < a->status_written + 1)
        return;
    EM_FORMAT_FIXED(a->status_summary, sizeof(a->status_summary), "%s", summary);
    cJSON *s = status(a);
    char *text = cJSON_Print(s), path[512];
    size_t n = strlen(text);
    text = em_realloc(text, n + 2);
    memcpy(text + n, "\n", 2);
    if (!em_format(path, sizeof(path), "%s/status.json", a->state_dir) || !em_write_file(path, text, false))
        LOG("status: not written to %s", a->state_dir);
    free(text);
    cJSON_Delete(s);
    a->status_written = now();
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
        (void)fprintf(stderr, "invalid agent-config contract: %s\n", schema ? where : "schema unavailable (EMOSA_SCHEMAS)");
        return false;
    }
    a->pod_id = cfg_str(c, "pod_id");
    a->run_id = cfg_str(c, "run_id") ? cfg_str(c, "run_id") : cfg_str(c, "pod_id");
    a->serial = cfg_str(c, "serial");
    a->interface = cfg_str(c, "interface");
    a->state_dir = cfg_str(c, "state_dir");
    a->profile_id = cfg_str(c, "profile") ? cfg_str(c, "profile") : "opensync-lab-hwsim-6.6.1-v1";
    const char *set = cfg_str(c, "message_set"), *m2 = cfg_str(c, "m2_session");
    a->r1 = set && !strcmp(set, "r1");
    a->multi_bss = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(c, "multi_bss"));
    a->shared_session = m2 && !strcmp(m2, "shared");
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
            (void)fprintf(stderr, "telemetry: mode mqtt needs a broker and valid intervals\n");
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
        !em_parse_mac(cfg_str(c, "al_mac") ? cfg_str(c, "al_mac") : "", a->al) ||
        !em_parse_mac(cfg_str(c, "controller_al") ? cfg_str(c, "controller_al") : "", a->controller))
        return false;
    char profile[1024];
    if (!em_format(profile, sizeof(profile), "%s/%s.json", profiles, a->profile_id) ||
        em_profile_load(profile, &a->profile) != EM_OK)
        return false;
    /* the uplink: option 1, the pod's backhaul station on the EasyMesh backhaul BSS */
    const cJSON *uplink = cJSON_GetObjectItemCaseSensitive(c, "uplink");
    const char *umode = cfg_str(uplink, "mode");
    a->uplink_on = umode && !strcmp(umode, "multi-ap");
    if (a->uplink_on) {
        a->uplink.station = cfg_str(uplink, "station") ? cfg_str(uplink, "station") : a->profile.uplink_station;
        const char *bssid = cfg_str(uplink, "bssid");
        uint8_t mac[6];
        if (!a->uplink.station || !bssid || !em_parse_mac(bssid, mac)) {
            (void)fprintf(stderr, "uplink: a station (profile or configuration) and bssid (the upstream backhaul BSS) are required\n");
            return false;
        }
        const char *credentials = cfg_str(uplink, "credentials");
        a->uplink.fixed_credentials = credentials && !strcmp(credentials, "config");
        if (a->uplink.fixed_credentials) {
            if (!cfg_str(uplink, "ssid") || !cfg_str(uplink, "secret_ref")) {
                (void)fprintf(stderr, "uplink: config credentials need ssid and secret_ref\n");
                return false;
            }
            /* the schema counts characters: a 32-character SSID can be longer in bytes */
            if (!em_copy(a->uplink.fixed_ssid, sizeof(a->uplink.fixed_ssid), cfg_str(uplink, "ssid")) ||
                !em_copy(a->uplink.fixed_ref, sizeof(a->uplink.fixed_ref), cfg_str(uplink, "secret_ref"))) {
                (void)fprintf(stderr, "uplink: ssid longer than 32 octets or secret_ref too long\n");
                return false;
            }
        }
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

/* the uplink may switch: the pod serves the controller's fronthaul, nothing is being written */
static bool fronthaul_settled(void *ctx)
{
    agent *a = ctx;
    return source_current(a) && a->has_primary && !active_op(a);
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

int main(int argc, char **argv)
{
    em_init();
    const char *profiles = getenv("EMOSA_PROFILES") ? getenv("EMOSA_PROFILES") : "/usr/share/emosa/profiles";
    const char *config = NULL;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--profiles") && i + 1 < argc)
            profiles = argv[++i];
        else if (argv[i][0] != '-')
            config = argv[i];
    }
    static agent a;
    if (!config || !configure(&a, config, profiles)) {
        (void)fprintf(stderr, "usage: emosa-agent-c CONFIG.json [--profiles DIR] (configuration or profile unusable)\n");
        return 2;
    }
    mkdir(a.state_dir, 0700);
    /* the AP scope's secrets and journal (a second writer is refused: one agent per pod) */
    char dir[600];
    EM_FORMAT_FIXED(dir, sizeof(dir), "%s/secrets", a.state_dir);
    if (em_vault_open(&a.vault, dir) != EM_OK) {
        (void)fprintf(stderr, "secret directory %s unusable (it must be 0700)\n", dir);
        return 1;
    }
    const char *schemas = em_schema_directory();
    a.schemas = (em_journal_schemas){em_schema_load(schemas, "operation"), em_schema_load(schemas, "event"),
                                     em_schema_load(schemas, "wsc-receipt")};
    EM_FORMAT_FIXED(dir, sizeof(dir), "%s/journal", a.state_dir);
    em_reason opened;
    a.journal = em_journal_open(dir, &a.schemas, &opened);
    if (!a.journal) {
        (void)fprintf(stderr, "journal %s: %s\n", dir, em_reason_name(opened));
        return 1;
    }
    if (signal(SIGTERM, on_signal) == SIG_ERR || signal(SIGINT, on_signal) == SIG_ERR ||
        signal(SIGPIPE, SIG_IGN) == SIG_ERR) {
        (void)fprintf(stderr, "signal handlers not installed\n");
        return 1;
    }
    static const char *const tables[] = {"AWLAN_Node", "Wifi_Radio_Config", "Wifi_Radio_State",
        "Wifi_VIF_Config", "Wifi_VIF_State", "Wifi_Associated_Clients", "Wifi_Inet_Config",
        "Wifi_Credential_Config", "Connection_Manager_Uplink", "Band_Steering_Config",
        "Band_Steering_Clients", "Wifi_VIF_Neighbors", "Wifi_Stats_Config"};
    a.ovs = em_ovsdb_open(cfg_str(a.config, "ovsdb"), tables, sizeof(tables) / sizeof(*tables));
    if (!a.ovs) {
        (void)fprintf(stderr, "cannot listen on %s\n", cfg_str(a.config, "ovsdb"));
        return 1;
    }
    a.ap = (em_ap_scope){.ovs = a.ovs, .profile = &a.profile, .serial = a.serial, .pod_id = a.pod_id,
                         .multi_bss = a.multi_bss, .vault = &a.vault, .transact = transact, .transact_ctx = &a};
    em_engine_init(&a.ap_engine, a.journal, &a.vault, a.pod_id, em_ap_backend(), &a.ap, now);
    em_engine_recover(&a.ap_engine);
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
            (void)fprintf(stderr, "telemetry journal: %s\n", em_reason_name(why));
            return 1;
        }
        em_pod_stats_init(&a.stats, a.telemetry.intent.topic, (unsigned)a.telemetry.intent.reporting_interval,
                          stats_clock, NULL);
        char client[160];
        EM_FORMAT_FIXED(client, sizeof(client), "emosa-agent-%s", a.pod_id); /* pod_id: at most 64 (schema) */
        a.mqtt = em_mqtt_open(a.subscribe_host, a.subscribe_port, a.telemetry.intent.topic, client, deliver, &a);
        /* the stations the controller asks about, watched for their probe requests (§3.9) */
        a.watch = (em_watch_scope){.ovs = a.ovs, .serial = a.serial, .pod_id = a.pod_id, .run_id = a.run_id,
                                   .if_name = a.profile.fronthaul_if, .band = a.telemetry.intent.radio_type,
                                   .transact = transact, .transact_ctx = &a, .monotonic = now};
        why = em_watch_open(&a.watch, a.state_dir, &a.schemas, &a.vault);
        if (why != EM_OK) {
            (void)fprintf(stderr, "probe watch journal: %s\n", em_reason_name(why));
            return 1;
        }
    }
    if (a.steering_on) {
        a.steering = (em_steering_scope){.ovs = a.ovs, .serial = a.serial, .pod_id = a.pod_id, .run_id = a.run_id,
                                         .transact = transact, .transact_ctx = &a, .monotonic = now};
        em_reason why = em_steering_scope_open(&a.steering, a.state_dir, &a.schemas, &a.vault);
        if (why != EM_OK) {
            (void)fprintf(stderr, "steering journal: %s\n", em_reason_name(why));
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
        a.uplink.settled = fronthaul_settled;
        a.uplink.settled_ctx = &a;
        em_reason why = em_uplink_open(&a.uplink, a.state_dir, &a.schemas,
                                       cfg_str(cJSON_GetObjectItemCaseSensitive(a.config, "uplink"), "bssid"));
        if (why != EM_OK) {
            (void)fprintf(stderr, "uplink journal: %s\n", em_reason_name(why));
            return 1;
        }
    }
    if (em_ethernet_open(&a.eth, a.interface, a.al) != EM_OK) {
        (void)fprintf(stderr, "cannot open the 1905 interface %s with MAC %s\n", a.interface, cfg_str(a.config, "al_mac"));
        return 1;
    }
    uint16_t seed;
    RAND_bytes((uint8_t *)&seed, sizeof(seed));
    a.mid = seed;
    a.tokens = 32;
    a.token_time = now();
    em_renew_init(&a.renewals, a.token_time);
    LOG("EMOSA C agent for %s: AL %s, controller %s, OVSDB %s, %s", a.pod_id, cfg_str(a.config, "al_mac"),
        cfg_str(a.config, "controller_al"), cfg_str(a.config, "ovsdb"), a.r1 ? "r1" : "easymesh-6.1");
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
            next_refresh = now() + REFRESH_PERIOD;
        }
        session_tick(&a);
        if (t >= a.next_discovery) {
            em_tlv tlvs[2] = {{0x01, 6, a.al}, {0x02, 6, a.al}};
            send_message(&a, EM_MULTICAST, 0x0000, next_mid(&a), tlvs, 2, false);
            a.next_discovery = t + DISCOVERY_PERIOD;
        }
        bool unserved = a.at.state == S_PROVISIONING && source_current(&a) && !a.has_primary && !active_op(&a);
        const char *reasons[3];
        size_t nreasons = em_renew_check(&a.renewals, t, unserved, a.at.state == S_AWAITING_M2, reasons);
        for (size_t i = 0; i < nreasons; i++)
            renew(&a, !strcmp(reasons[i], "unserved") ? "provisioned, but the pod serves no BSS: fresh M1"
                      : !strcmp(reasons[i], "no_m2") ? "no M2 for 30s after M1: fresh attempt"
                                                     : "no message from the controller for 130s: fresh attempt");
        size_t len;
        while ((len = em_ethernet_receive(&a.eth, frame, sizeof(frame))) > 0)
            receive_frame(&a, frame, len);
        if (refreshed && source_current(&a))
            reconcile(&a);
        /* the scopes step on the refresh cadence, also while the pod is away: a write
         * in flight times out */
        if (refreshed && a.uplink_on)
            em_uplink_tick(&a.uplink);
        if (refreshed && a.telemetry_on)
            em_telemetry_tick(&a.telemetry);
        if (refreshed && a.steering_on)
            em_steering_tick(&a.steering);
        if (refreshed && a.telemetry_on)
            em_watch_tick(&a.watch);
        write_status(&a);
    }
    write_status(&a);
    em_engine_free(&a.ap_engine);
    em_journal_close(a.journal);
    if (a.telemetry_on) {
        em_mqtt_close(a.mqtt);
        em_telemetry_close(&a.telemetry);
        em_watch_close(&a.watch);
    }
    if (a.steering_on)
        em_steering_scope_close(&a.steering);
    if (a.uplink_on)
        em_uplink_close(&a.uplink);
    em_policy_store_close(a.policy_store);
    em_channel_store_close(a.channel_store);
    em_ethernet_close(&a.eth);
    em_ovsdb_close(a.ovs);
    LOG("stopped");
    return 0;
}
