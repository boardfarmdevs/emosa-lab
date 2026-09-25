/* emosa-agent-c: the EMOSA virtual EasyMesh agent for one OpenSync pod (a lab prototype;
 * see c/README.md and spec/design.md). Same configuration and status file as the Python
 * agent (emosa.agent.pod); one owner thread, a poll loop (spec/design.md §5.1).
 *
 *   emosa-agent-c CONFIG.json [--profiles DIR]
 *
 * Not yet here (the lab README lists them): the durable journal, the uplink and
 * telemetry scopes, link and AP metrics answers, the Early AP Capability Report retries. */
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
#include "cmdu.h"
#include "control.h"
#include "ethernet.h"
#include "ovs.h"
#include "ovsdb.h"
#include "southbound.h"
#include "view.h"
#include "wsc.h"

#define REFRESH_PERIOD 0.5
#define LEASE 1.5
#define DISCOVERY_PERIOD 60
#define CONTROLLER_TIMEOUT 130
#define UNSERVED_RENEW 60
#define AP_DEADLINE 120
#define STEER_APPLY 10
#define STEER_GENTLE 8
#define STEER_MARGIN 5
#define MAX_OPS 32

static volatile sig_atomic_t stopping;
static void on_signal(int sig) { (void)sig; stopping = 1; }

static double now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static void logf_(const char *level, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "%s emosa.agent: ", level);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
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
        snprintf(c->items[c->n].name, sizeof(c->items[c->n].name), "%s", name);
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

typedef enum { S_NONE, S_DISCOVERING, S_AWAITING_M2, S_PROVISIONING, S_FAILED, S_SOURCE_LOST } session_state;
static const char *const SESSION_NAMES[] = {"recovering", "discovering", "awaiting_m2", "provisioning", "failed", "source_lost"};

typedef struct {
    char id[37], state[24], reason[32], ssid[33];
    unsigned attempts;
    double deadline;
    /* the intent, kept to confirm application (passphrases stay in memory only) */
    char key[64];
    size_t nextra;
    struct {
        char role[10], ssid[33], key[64];
    } extra[8];
} operation;

typedef struct {
    bool active;
    char phase[12];
    em_steering_intent intent;
    em_steering_rows created;
    char op_id[37];
    double opened, kicked;
} steering_job;

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
    em_device_view view;
    em_radio_view represented; /* the radio with only the BSSes the agent represents */
    bool has_primary;
    int channel, tx_power;
    int8_t max_eirp;
    em_tlv_list caps;
    em_tlv inventory;
    size_t nfirst;
    struct {
        uint8_t mac[6];
        double at;
    } first_seen[256];
    /* the session */
    session_state state;
    int session_generation;
    unsigned starts, failures;
    double next_start;
    em_reassembler *assembly;
    unsigned searches;
    double next_search, discovery_deadline;
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
    long topology_mark;
    bool reannounced;
    unsigned topology_responses;
    double tokens, token_time, last_contact, next_discovery, unserved_since;
    counters counts;
    /* operations */
    size_t nops;
    operation ops[MAX_OPS];
    unsigned writes;
    steering_job job;
    counters steering_counts;
    cJSON *steering_history;
    char status_summary[512];
    double status_written;
} agent;

static uint16_t next_mid(agent *a) { return ++a->mid; }

static void new_id(char out[37])
{
    uint8_t b[16];
    RAND_bytes(b, sizeof(b));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    snprintf(out, 37, "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
             b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11], b[12],
             b[13], b[14], b[15]);
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

static em_ovs_session session_of(agent *a)
{
    return (em_ovs_session){em_ovsdb_tables(a->ovs), em_ovsdb_generation(a->ovs), transact, a};
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
    em_device_view *view = malloc(sizeof(*view));
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
    typeof(a->first_seen[0]) seen[256];
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
    a->view = *view;
    free(view);
    a->represented = rep;
    a->has_primary = primary;
    a->channel = channel;
    a->tx_power = radio->has_tx_power ? radio->tx_power : 0;
    a->generation = em_ovsdb_generation(a->ovs);
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
        ages[i].seconds = (long)(t - a->first_seen[i].at);
    }
    return em_topology_tlvs(a->al, a->controller, &a->represented, a->channel, ages, a->nfirst, r1,
                            NULL, out);
}

/* -- operations ---------------------------------------------------------------------- */

static operation *active_op(agent *a)
{
    for (size_t i = 0; i < a->nops; i++)
        if (!strcmp(a->ops[i].state, "CONFIG_COMMITTED") || !strcmp(a->ops[i].state, "INDETERMINATE"))
            return &a->ops[i];
    return NULL;
}

static const char *resolve(void *ctx, const char *ref)
{
    operation *op = ctx;
    if (!strcmp(ref, "primary"))
        return op->key;
    for (size_t i = 0; i < op->nextra; i++) {
        char name[32];
        snprintf(name, sizeof(name), "extra-%zu", i);
        if (!strcmp(ref, name))
            return op->extra[i].key;
    }
    return NULL;
}

/* observed applied: the pod's State shows every BSS of the intent (ssid, WPA2-PSK, key) */
static bool bss_applied(agent *a, const char *if_name, const char *ssid, const char *key, bool backhaul)
{
    const cJSON *st = vif_state_row(a, if_name);
    const char *mode = st ? ovs_str(st, "mode") : NULL, *have = st ? ovs_str(st, "ssid") : NULL;
    const char *keys[4];
    if (!st || !mode || strcmp(mode, "ap") || !ovs_true(st, "enabled") || !have || strcmp(have, ssid) ||
        !wpa2_psk_row(st) || ovs_map_keys(st, "wpa_psks", keys, 4) != 1)
        return false;
    const char *value = ovs_map_get(st, "wpa_psks", keys[0]), *multi_ap = ovs_str(st, "multi_ap");
    bool is_backhaul = multi_ap && !strcmp(multi_ap, "backhaul_bss");
    return value && !strcmp(value, key) && is_backhaul == backhaul;
}

static void reconcile(agent *a)
{
    operation *op = active_op(a);
    if (!op)
        return;
    bool applied = bss_applied(a, a->profile.fronthaul_if, op->ssid, op->key, false);
    size_t used[8] = {0};
    for (size_t k = 0; applied && k < op->nextra; k++) {
        bool found = false;
        for (size_t i = 0; !found && i < a->profile.nslots; i++)
            if (!used[i] && !strcmp(a->profile.slots[i].role, op->extra[k].role)) {
                used[i] = 1;
                found = bss_applied(a, a->profile.slots[i].if_name, op->extra[k].ssid, op->extra[k].key,
                                    !strcmp(op->extra[k].role, "backhaul"));
            }
        applied = found;
    }
    if (applied) {
        strcpy(op->state, "OBSERVED_APPLIED");
        op->reason[0] = 0;
        LOG("operation %s: OBSERVED_APPLIED", op->id);
    } else if (now() >= op->deadline) {
        strcpy(op->state, "TIMED_OUT");
        strcpy(op->reason, "APPLY_TIMEOUT");
        WARN("operation %s: TIMED_OUT", op->id);
    }
}

/* one authenticated M2 set: an operation and its guarded transaction */
static const char *operate(agent *a, const em_m2_result *m2)
{
    if (a->op_of_session[0])
        return "wsc_operation"; /* the same exchange again: its operation stands */
    operation *op = a->nops < MAX_OPS ? &a->ops[a->nops++] : &a->ops[MAX_OPS - 1];
    memset(op, 0, sizeof(*op));
    new_id(op->id);
    strcpy(a->op_of_session, op->id);
    size_t primary = 0;
    while (primary < m2->count && strcmp(m2->bss[primary].role, "fronthaul"))
        primary++;
    snprintf(op->ssid, sizeof(op->ssid), "%s", m2->bss[primary].ssid);
    snprintf(op->key, sizeof(op->key), "%s", m2->bss[primary].passphrase);
    em_ap_intent intent = {.ssid = op->ssid, .secret_ref = "primary", .has_additional = a->multi_bss};
    char refs[8][24];
    for (size_t i = 0; i < m2->count && op->nextra < 8; i++) {
        if (i == primary)
            continue;
        size_t k = op->nextra++;
        snprintf(op->extra[k].role, sizeof(op->extra[k].role), "%s", m2->bss[i].role);
        snprintf(op->extra[k].ssid, sizeof(op->extra[k].ssid), "%s", m2->bss[i].ssid);
        snprintf(op->extra[k].key, sizeof(op->extra[k].key), "%s", m2->bss[i].passphrase);
        snprintf(refs[k], sizeof(refs[k]), "extra-%zu", k);
        intent.additional[k].role = op->extra[k].role;
        intent.additional[k].ssid = op->extra[k].ssid;
        intent.additional[k].secret_ref = refs[k];
        intent.nadditional = k + 1;
    }
    if (active_op(a) && active_op(a) != op) {
        strcpy(op->state, "REJECTED");
        strcpy(op->reason, "BUSY");
        return "wsc_operation";
    }
    op->attempts = 1;
    em_ovs_session s = session_of(a);
    em_submit_result result;
    em_reason r = em_ap_submit(&a->profile, a->serial, a->multi_bss, &s, &intent, resolve, op, &result);
    if (r != EM_OK) {
        strcpy(op->state, "REJECTED");
        snprintf(op->reason, sizeof(op->reason), "%s", em_reason_name(r));
    } else if (!strcmp(result.status, "committed")) {
        strcpy(op->state, "CONFIG_COMMITTED");
        op->deadline = now() + AP_DEADLINE;
    } else if (!strcmp(result.status, "conflict")) {
        strcpy(op->state, "OWNERSHIP_CONFLICT");
        strcpy(op->reason, "OWNERSHIP_CONFLICT");
    } else if (!strcmp(result.status, "unknown")) {
        strcpy(op->state, "INDETERMINATE");
        strcpy(op->reason, "OUTCOME_UNKNOWN");
        op->deadline = now() + AP_DEADLINE;
    } else {
        strcpy(op->state, "FAILED");
        snprintf(op->reason, sizeof(op->reason), "%s", em_reason_name(result.reason));
    }
    LOG("operation %s (%s): %s %s", op->id, op->ssid, op->state, op->reason);
    return "wsc_operation";
}

/* -- client steering executor (spec §3.7) -------------------------------------------- */

static void steering_history(agent *a, const char *outcome)
{
    cJSON *e = cJSON_CreateObject();
    cJSON_AddStringToObject(e, "station", a->job.intent.station);
    cJSON_AddStringToObject(e, "target", a->job.intent.target_bssid);
    cJSON_AddStringToObject(e, "outcome", outcome);
    cJSON_AddStringToObject(e, "operation_id", a->job.op_id);
    cJSON_AddItemToArray(a->steering_history, e);
    while (cJSON_GetArraySize(a->steering_history) > 8)
        cJSON_DeleteItemFromArray(a->steering_history, 0);
    count(&a->steering_counts, outcome);
    LOG("client steering %s -> %s: %s", a->job.intent.station, a->job.intent.target_bssid, outcome);
    a->job.active = false;
}

static const char *start_steering(void *ctx, const em_steering_request *r, uint16_t mid)
{
    agent *a = ctx;
    (void)mid;
    if (!a->steering_on)
        return "steering_off";
    if (a->job.active)
        return "busy";
    steering_job *j = &a->job;
    memset(j, 0, sizeof(*j));
    char *text = em_mac_str(r->stations[0]);
    snprintf(j->intent.station, sizeof(j->intent.station), "%s", text);
    free(text);
    text = em_mac_str(r->source_bssid);
    snprintf(j->intent.source_bssid, sizeof(j->intent.source_bssid), "%s", text);
    free(text);
    text = em_mac_str(r->targets[0].bssid);
    snprintf(j->intent.target_bssid, sizeof(j->intent.target_bssid), "%s", text);
    free(text);
    j->intent.op_class = r->targets[0].op_class;
    j->intent.channel = r->targets[0].channel;
    j->intent.disassoc_imminent = r->disassoc_imminent;
    int window = r->window < 15 ? 15 : r->window;
    j->intent.window = window > 120 ? 120 : window;
    new_id(j->op_id);
    em_ovs_session s = session_of(a);
    em_submit_result result;
    em_reason e = em_steering_open(a->serial, &s, &j->intent, &result, &j->created);
    j->active = true;
    count(&a->steering_counts, "started");
    if (e != EM_OK || strcmp(result.status, "committed")) {
        char outcome[64];
        snprintf(outcome, sizeof(outcome), "refused_%s",
                 e != EM_OK ? em_reason_name(e) : em_reason_name(result.reason));
        for (char *p = outcome; *p; p++)
            if (*p >= 'A' && *p <= 'Z')
                *p = (char)(*p + 32);
        if (e == EM_OK) /* the outcome is unknown: remove a row it may have written */
            em_steering_close(&s, &j->intent, &j->created);
        steering_history(a, outcome);
        return NULL;
    }
    strcpy(j->phase, "opening");
    j->opened = now();
    return NULL;
}

static void steering_tick(agent *a)
{
    steering_job *j = &a->job;
    if (!j->active)
        return;
    const char *cs_state = NULL;
    const cJSON *row;
    cJSON_ArrayForEach(row, cJSON_GetObjectItemCaseSensitive(em_ovsdb_tables(a->ovs), "Band_Steering_Clients"))
    {
        const char *mac = ovs_str(row, "mac");
        if (mac && !strcmp(mac, j->intent.station))
            cs_state = ovs_str(row, "cs_state");
    }
    bool on_source = false;
    uint8_t station[6], source[6];
    em_parse_mac(j->intent.station, station);
    em_parse_mac(j->intent.source_bssid, source);
    for (size_t i = 0; i < a->represented.nbss; i++)
        if (!memcmp(a->represented.bss[i].bssid, source, 6))
            for (size_t k = 0; k < a->represented.bss[i].nstations; k++)
                on_source = on_source || !memcmp(a->represented.bss[i].stations[k], station, 6);
    em_ovs_session s = session_of(a);
    double t = now();
    if (!strcmp(j->phase, "opening")) {
        if (cs_state && !strcmp(cs_state, "steering")) {
            if (em_steering_kick(a->serial, &s, j->intent.station, j->created.client) != EM_OK) {
                em_steering_close(&s, &j->intent, &j->created);
                steering_history(a, "kick_failed");
                return;
            }
            strcpy(j->phase, "kicked");
            j->kicked = t;
            count(&a->steering_counts, "kicked");
        } else if (t >= j->opened + STEER_APPLY) {
            em_steering_close(&s, &j->intent, &j->created);
            steering_history(a, "not_applied");
        }
        return;
    }
    double limit = j->intent.disassoc_imminent ? j->intent.window + STEER_MARGIN : STEER_GENTLE;
    if (cs_state && !strcmp(cs_state, "steering") && on_source && t < j->kicked + limit)
        return;
    em_steering_close(&s, &j->intent, &j->created);
    steering_history(a, on_source ? "stayed" : "left_source");
}

/* -- the onboarding session (spec §2.5) ---------------------------------------------- */

static void close_session(agent *a, session_state end)
{
    a->state = end;
    if (a->has_m1)
        em_m1_free(&a->m1);
    a->has_m1 = false;
    a->control_ready = false;
}

static void renew(agent *a, const char *why)
{
    LOG("%s: fresh M1", why);
    close_session(a, S_NONE);
    a->failures = 0;
    a->next_start = 0;
}

static void start_session(agent *a)
{
    a->state = S_DISCOVERING;
    a->session_generation = a->generation;
    a->starts++;
    a->searches = 0;
    a->next_search = 0;
    a->discovery_deadline = now() + 5;
    a->op_of_session[0] = 0;
    a->nclients = 0;
    a->last_topology[0] = 0;
    a->topology_mark = -1;
    a->reannounced = false;
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
    if (em_search_frames(&b, 0, 1, profile2, a->r1 ? EM_SET_R1 : EM_SET_61, mid, &f) == EM_OK) {
        send_frames(a, &f);
        a->search_mids[a->searches++] = mid;
        a->next_search = now() + 1;
        count(&a->counts, "search_sent");
    }
}

static em_m1_device m1_device(agent *a)
{
    em_m1_device d = {0};
    char seed[128];
    uint8_t digest[32];
    snprintf(seed, sizeof(seed), "emosa-agent:%s", a->serial);
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

static void admitted(agent *a)
{
    em_binding b = {0};
    memcpy(b.local_al, a->al, 6);
    memcpy(b.controller_al, a->controller, 6);
    if (!a->r1) /* EasyMesh 6.1: the Early AP Capability Report before M1 */
        send_message(a, a->controller, 0x8043, next_mid(a), a->caps.tlvs, a->caps.count, false);
    em_m1_device d = m1_device(a);
    em_reason r = em_m1_create(&d, NULL, &a->m1);
    free_device(&d);
    if (r != EM_OK) {
        close_session(a, S_FAILED);
        count(&a->counts, "m1_failed");
        return;
    }
    a->has_m1 = true;
    em_radio_caps caps = radio_caps(a);
    em_frames f;
    if (em_m1_frames(&b, &caps, &a->m1, a->r1 ? EM_SET_R1 : EM_SET_61, next_mid(a), &f) == EM_OK)
        send_frames(a, &f);
    a->state = S_AWAITING_M2;
    memset(&a->control, 0, sizeof(a->control));
    a->control.binding = b;
    memcpy(a->control.binding.sources[0], a->controller, 6);
    a->control.binding.nsources = 1;
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
    if (m->message_type == 0x0008 && a->state == S_DISCOVERING) {
        em_advertisement adv;
        bool mid_ok = false;
        for (unsigned i = 0; i < a->searches; i++)
            mid_ok = mid_ok || a->search_mids[i] == m->mid;
        if (em_parse_response(m, a->r1 ? EM_SET_R1 : EM_SET_61, &adv) != EM_OK || !mid_ok || adv.band != 0 ||
            (!a->r1 && adv.profile >= 1 && adv.profile <= 3 && adv.profile != 1)) {
            count(&a->counts, "rejected_INVALID_INPUT");
            return;
        }
        admitted(a);
        return;
    }
    if (m->message_type == 0x0009) {
        if (a->state != S_AWAITING_M2 && a->state != S_PROVISIONING) {
            count(&a->counts, "wsc_before_admission");
            return;
        }
        em_binding b = a->control.binding;
        em_radio_caps caps = radio_caps(a);
        em_m2_result result;
        em_reason r = em_receive_m2(&b, &caps, &a->m1, m, a->multi_bss, a->shared_session, &result);
        if (r != EM_OK || result.teardown) {
            snprintf(label, sizeof(label), "wsc_rejected_%s", r != EM_OK ? em_reason_name(r) : "teardown");
            count(&a->counts, label);
            return;
        }
        count(&a->counts, operate(a, &result));
        if (a->state != S_PROVISIONING) {
            a->state = S_PROVISIONING;
            a->topology_mark = a->topology_responses;
        }
        return;
    }
    if (!a->control_ready) {
        snprintf(label, sizeof(label), "unsupported_message_%04x", m->message_type);
        count(&a->counts, label);
        return;
    }
    if (m->message_type == 0x0002) {
        em_tlv_list list;
        if (topology_tlvs(a, a->r1, &list) == EM_OK) {
            reply(a, 0x0003, m->mid, list.tlvs, list.count);
            em_tlv_list_free(&list);
            a->topology_responses++;
            count(&a->counts, "topology_response_sent");
        }
        return;
    }
    if (m->message_type == 0x8000) {
        count(&a->counts, "ack_received");
        return;
    }
    if (m->message_type == 0x8014 && a->state != S_PROVISIONING) {
        count(&a->counts, "unsupported_message_8014");
        return;
    }
    /* channel, policy and client steering */
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
            snprintf(label, sizeof(label), "rejected_%s", em_reason_name(error));
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
        reply(a, 0x8028, m->mid, NULL, 0); /* no EasyMesh backhaul station over GRE */
        count(&a->counts, "backhaul_sta_capability_report_sent");
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
        snprintf(label, sizeof(label), "unsupported_message_%04x", m->message_type);
        count(&a->counts, label);
    }
}

static void receive_frame(agent *a, const uint8_t *frame, size_t len)
{
    if (len < 22)
        return;
    uint16_t type = (uint16_t)(frame[16] << 8 | frame[17]);
    bool from_controller = !memcmp(frame + 6, a->controller, 6);
    if (from_controller && a->state != S_NONE)
        a->last_contact = now();
    if (from_controller && type == 0x000A) {
        count(&a->counts, "renew_received");
        renew(a, "AP-Autoconfiguration Renew from the controller");
        return;
    }
    if (a->state == S_NONE || a->state == S_FAILED || a->state == S_SOURCE_LOST)
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
        snprintf(label, sizeof(label), "rejected_%s", em_reason_name(r));
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
        off += (size_t)snprintf(topo + off, sizeof(topo) - off, "%s/%s;", mac, a->represented.bss[i].ssid);
        free(mac);
    }
    em_tlv al = {0x01, 6, a->al};
    if (strcmp(topo, a->last_topology)) {
        send_message(a, EM_MULTICAST, 0x0001, next_mid(a), &al, 1, true);
        snprintf(a->last_topology, sizeof(a->last_topology), "%s", topo);
        count(&a->counts, "observed_topology_notification");
    }
    if (!a->reannounced && a->topology_mark >= 0 && (long)a->topology_responses > a->topology_mark) {
        a->nclients = 0; /* announce every client again once the controller knows the BSS */
        a->reannounced = true;
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
    if (a->state == S_NONE) {
        if (t >= a->next_start && source_current(a))
            start_session(a);
        else
            return;
    }
    if (a->state == S_FAILED || a->state == S_SOURCE_LOST) {
        close_session(a, S_NONE);
        unsigned backoff = 1u << (a->failures < 5 ? a->failures : 5);
        a->next_start = t + (backoff < 30 ? backoff : 30);
        a->failures++;
        return;
    }
    if (!source_current(a) || a->generation != a->session_generation) {
        count(&a->counts, "source_lost");
        close_session(a, S_SOURCE_LOST);
        return;
    }
    if (a->state == S_DISCOVERING) {
        if (t >= a->discovery_deadline) {
            count(&a->counts, "discovery_timeout");
            close_session(a, S_FAILED);
        } else if (t >= a->next_search && a->searches < 3) {
            search(a);
        }
    }
    if (a->state == S_PROVISIONING) {
        a->failures = 0;
        notify(a);
    }
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
    cJSON_AddNullToObject(o, "backhaul");
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
    cJSON_AddStringToObject(session, "state", SESSION_NAMES[a->state]);
    cJSON_AddItemToObject(session, "counts", counters_json(&a->counts, ""));
    cJSON *steering = cJSON_AddObjectToObject(session, "steering");
    cJSON_AddItemToObject(steering, "counts", counters_json(&a->counts, "client_steering_"));
    cJSON *recovery = cJSON_AddObjectToObject(session, "recovery");
    cJSON_AddNumberToObject(recovery, "attempts_started", a->starts);
    cJSON_AddItemToObject(o, "session", session);
    for (size_t i = 0; i < a->nops; i++) {
        cJSON *x = cJSON_CreateObject();
        cJSON_AddStringToObject(x, "operation_id", a->ops[i].id);
        cJSON_AddStringToObject(x, "state", a->ops[i].state);
        if (a->ops[i].reason[0])
            cJSON_AddStringToObject(x, "reason", a->ops[i].reason);
        else
            cJSON_AddNullToObject(x, "reason");
        cJSON_AddStringToObject(x, "ssid", a->ops[i].ssid);
        cJSON_AddNumberToObject(x, "attempts", a->ops[i].attempts);
        cJSON_AddNullToObject(x, "application_evidence");
        cJSON_AddItemToArray(ops, x);
    }
    cJSON_AddItemToObject(o, "operations", ops);
    cJSON *uplink = cJSON_AddObjectToObject(o, "uplink");
    cJSON_AddStringToObject(uplink, "mode", "off");
    cJSON *telemetry = cJSON_AddObjectToObject(o, "telemetry");
    cJSON_AddStringToObject(telemetry, "mode", "off");
    cJSON *st = cJSON_AddObjectToObject(o, "steering");
    cJSON_AddStringToObject(st, "mode", a->steering_on ? "owm" : "off");
    cJSON_AddItemToObject(st, "counts", counters_json(&a->steering_counts, ""));
    if (a->job.active) {
        cJSON *x = cJSON_AddObjectToObject(st, "active");
        cJSON_AddStringToObject(x, "station", a->job.intent.station);
        cJSON_AddStringToObject(x, "target", a->job.intent.target_bssid);
        cJSON_AddStringToObject(x, "phase", a->job.phase);
        cJSON_AddStringToObject(x, "operation_id", a->job.op_id);
    } else {
        cJSON_AddNullToObject(st, "active");
    }
    cJSON_AddItemToObject(st, "history", cJSON_Duplicate(a->steering_history, 1));
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
    snprintf(summary, sizeof(summary), "%s %d %zu %s", SESSION_NAMES[a->state], source_current(a), a->nops,
             a->nops ? a->ops[a->nops - 1].state : "");
    bool changed = strcmp(summary, a->status_summary) != 0;
    if (changed)
        LOG("state: %s", summary);
    if (!changed && now() < a->status_written + 1)
        return;
    snprintf(a->status_summary, sizeof(a->status_summary), "%s", summary);
    cJSON *s = status(a);
    char *text = cJSON_Print(s), path[512], tmp[520];
    snprintf(path, sizeof(path), "%s/status.json", a->state_dir);
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    FILE *f = fopen(tmp, "w");
    if (f) {
        fputs(text, f);
        fputc('\n', f);
        fclose(f);
        rename(tmp, path);
    }
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
    FILE *f = fopen(path, "rb");
    if (!f)
        return false;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *text = malloc((size_t)n + 1);
    size_t got = fread(text, 1, (size_t)n, f);
    fclose(f);
    text[got] = 0;
    a->config = cJSON_Parse(text);
    free(text);
    const cJSON *c = a->config;
    a->pod_id = cfg_str(c, "pod_id");
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
    if (!a->pod_id || !a->serial || !a->interface || !a->state_dir ||
        !em_parse_mac(cfg_str(c, "al_mac") ? cfg_str(c, "al_mac") : "", a->al) ||
        !em_parse_mac(cfg_str(c, "controller_al") ? cfg_str(c, "controller_al") : "", a->controller))
        return false;
    char profile[1024];
    snprintf(profile, sizeof(profile), "%s/%s.json", profiles, a->profile_id);
    return em_profile_load(profile, &a->profile) == EM_OK;
}

int main(int argc, char **argv)
{
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
        fprintf(stderr, "usage: emosa-agent-c CONFIG.json [--profiles DIR] (configuration or profile unusable)\n");
        return 2;
    }
    mkdir(a.state_dir, 0700);
    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);
    signal(SIGPIPE, SIG_IGN);
    static const char *const tables[] = {"AWLAN_Node", "Wifi_Radio_Config", "Wifi_Radio_State",
        "Wifi_VIF_Config", "Wifi_VIF_State", "Wifi_Associated_Clients", "Wifi_Inet_Config",
        "Wifi_Credential_Config", "Connection_Manager_Uplink", "Band_Steering_Config",
        "Band_Steering_Clients", "Wifi_VIF_Neighbors"};
    a.ovs = em_ovsdb_open(cfg_str(a.config, "ovsdb"), tables, sizeof(tables) / sizeof(*tables));
    if (!a.ovs) {
        fprintf(stderr, "cannot listen on %s\n", cfg_str(a.config, "ovsdb"));
        return 1;
    }
    if (em_ethernet_open(&a.eth, a.interface, a.al) != EM_OK) {
        fprintf(stderr, "cannot open the 1905 interface %s with MAC %s\n", a.interface, cfg_str(a.config, "al_mac"));
        return 1;
    }
    uint16_t seed;
    RAND_bytes((uint8_t *)&seed, sizeof(seed));
    a.mid = seed;
    a.tokens = 32;
    a.token_time = a.last_contact = now();
    a.steering_history = cJSON_CreateArray();
    a.topology_mark = -1;
    LOG("EMOSA C agent for %s: AL %s, controller %s, OVSDB %s, %s", a.pod_id, cfg_str(a.config, "al_mac"),
        cfg_str(a.config, "controller_al"), cfg_str(a.config, "ovsdb"), a.r1 ? "r1" : "easymesh-6.1");
    double next_refresh = 0;
    uint8_t frame[1600];
    while (!stopping) {
        int fds[4];
        size_t n = em_ovsdb_fds(a.ovs, fds, 3);
        struct pollfd p[4];
        for (size_t i = 0; i < n; i++)
            p[i] = (struct pollfd){fds[i], POLLIN, 0};
        p[n] = (struct pollfd){a.eth.fd, POLLIN, 0};
        poll(p, n + 1, 200);
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
        if (a.state == S_PROVISIONING && t - a.last_contact > CONTROLLER_TIMEOUT) {
            renew(&a, "no message from the controller for 130s");
            a.last_contact = t;
        }
        bool unserved = a.state == S_PROVISIONING && source_current(&a) && !a.has_primary && !active_op(&a);
        if (!unserved)
            a.unserved_since = 0;
        else if (!a.unserved_since)
            a.unserved_since = t;
        else if (t - a.unserved_since > UNSERVED_RENEW) {
            renew(&a, "provisioned, but the pod serves no BSS");
            a.unserved_since = 0;
        }
        size_t len;
        while ((len = em_ethernet_receive(&a.eth, frame, sizeof(frame))) > 0)
            receive_frame(&a, frame, len);
        if (refreshed && source_current(&a)) {
            reconcile(&a);
            steering_tick(&a);
        }
        write_status(&a);
    }
    write_status(&a);
    em_ethernet_close(&a.eth);
    em_ovsdb_close(a.ovs);
    LOG("stopped");
    return 0;
}
