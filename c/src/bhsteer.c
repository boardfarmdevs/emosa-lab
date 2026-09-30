/* Backhaul Steering (emosa.wire.backhaul_steering). */
#include "bhsteer.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define REQUEST 0x8019
#define RESPONSE 0x801A
#define ACK 0x8000
#define REQUEST_TLV 0x9E
#define RESPONSE_TLV 0x9F
#define ERROR_CODE_TLV 0xA3
#define ASSOCIATION_FAILED 0x06
#define DEADLINE 120 /* the uplink switch's own deadline (90 s), a refresh and the pod's State */

static const char *record(em_bh_coordinator *c, const char *event)
{
    for (size_t i = 0; i < c->counts.n; i++)
        if (!strcmp(c->counts.items[i].name, event)) {
            c->counts.items[i].count++;
            return event;
        }
    if (c->counts.n < 16) {
        EM_FORMAT_FIXED(c->counts.items[c->counts.n].name, sizeof(c->counts.items[0].name), "%.95s", event);
        c->counts.items[c->counts.n++].count = 1;
    }
    return event;
}

static bool unicast(const uint8_t *m)
{
    static const uint8_t zero[6] = {0};
    return memcmp(m, zero, 6) && !(m[0] & 1);
}

static void append(em_frames *out, em_frames *f)
{
    out->frames = em_realloc(out->frames, (out->count + f->count) * sizeof(*out->frames));
    memcpy(out->frames + out->count, f->frames, f->count * sizeof(*f->frames));
    out->count += f->count;
    free(f->frames);
}

static void send(em_bh_coordinator *c, uint16_t type, uint16_t mid, const em_tlv *tlvs, size_t n, em_frames *out)
{
    em_frames f;
    if (em_fragment(c->controller, c->local_al, type, mid, tlvs, n, false, EM_MAX_CMDU, &f) == EM_OK)
        append(out, &f);
}

static void respond(em_bh_coordinator *c, uint16_t mid, const uint8_t request[14], uint8_t error, double now,
                    em_frames *out)
{
    uint8_t response[13], code[7];
    memcpy(response, request, 12);
    response[12] = error ? 0x01 : 0x00;
    em_tlv tlvs[2] = {{RESPONSE_TLV, 13, response}, {ERROR_CODE_TLV, 7, code}};
    code[0] = error;
    memcpy(code + 1, request, 6);
    send(c, RESPONSE, mid, tlvs, error ? 2 : 1, out);
    em_bh_shared *s = c->shared;
    size_t slot = s->nanswered;
    for (size_t i = 0; i < s->nanswered; i++)
        if (s->answered[i].mid == mid)
            slot = i;
    if (slot == s->nanswered) {
        if (s->nanswered == 16) { /* the oldest answer goes */
            memmove(&s->answered[0], &s->answered[1], 15 * sizeof(s->answered[0]));
            slot = 15;
        } else {
            s->nanswered++;
        }
    }
    s->answered[slot].mid = mid;
    memcpy(s->answered[slot].request, request, 14);
    s->answered[slot].error = error;
    s->answered[slot].expiry = now + 30;
}

static cJSON *request_json(const uint8_t r[14])
{
    char station[18], target[18];
    EM_FORMAT_FIXED(station, sizeof(station), "%02x:%02x:%02x:%02x:%02x:%02x", r[0], r[1], r[2], r[3], r[4], r[5]);
    EM_FORMAT_FIXED(target, sizeof(target), "%02x:%02x:%02x:%02x:%02x:%02x", r[6], r[7], r[8], r[9], r[10], r[11]);
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "station", station);
    cJSON_AddStringToObject(o, "target", target);
    cJSON_AddNumberToObject(o, "operating_class", r[12]);
    cJSON_AddNumberToObject(o, "channel", r[13]);
    return o;
}

void em_bh_start(em_bh_coordinator *c, em_bh_shared *shared, const uint8_t controller[6], const uint8_t local_al[6],
                 em_bh_executor executor, em_bh_outcome outcome, void *ctx)
{
    cJSON_Delete(c->last);
    memset(c, 0, sizeof(*c));
    c->shared = shared;
    memcpy(c->controller, controller, 6);
    memcpy(c->local_al, local_al, 6);
    c->executor = executor;
    c->outcome = outcome;
    c->ctx = ctx;
}

void em_bh_close(em_bh_coordinator *c) { c->closed = true; }

const char *em_bh_handle(em_bh_coordinator *c, const em_message *m, double now, const uint8_t (*stations)[6],
                         size_t nstations, em_frames *out, em_reason *error)
{
    static char label[128];
    *error = EM_OK;
    if (m->message_type != REQUEST)
        return NULL;
    if (memcmp(m->source, c->controller, 6) || memcmp(m->destination, c->local_al, 6) || m->relay) {
        *error = EM_INVALID_INPUT; /* "backhaul steering request from an unbound controller" */
        return NULL;
    }
    em_bh_shared *s = c->shared;
    size_t kept = 0;
    for (size_t i = 0; i < s->nanswered; i++)
        if (s->answered[i].expiry > now)
            s->answered[kept++] = s->answered[i];
    s->nanswered = kept;
    const em_tlv *t = NULL;
    size_t n = 0;
    for (size_t i = 0; i < m->ntlvs; i++)
        if (m->tlvs[i].kind == REQUEST_TLV) {
            t = &m->tlvs[i];
            n++;
        }
    if (n != 1 || t->len != 14 || !unicast(t->value)) {
        *error = EM_INVALID_INPUT; /* "one Backhaul Steering Request TLV required" */
        return NULL;
    }
    uint8_t request[14];
    memcpy(request, t->value, 14);
    send(c, ACK, m->mid, NULL, 0, out);
    for (size_t i = 0; i < s->nanswered; i++)
        if (s->answered[i].mid == m->mid) {
            uint8_t again[14];
            memcpy(again, s->answered[i].request, 14);
            respond(c, m->mid, again, s->answered[i].error, now, out);
            return record(c, "backhaul_steering_repeated_response_sent");
        }
    if (s->pending && s->pending_mid == m->mid)
        return record(c, "backhaul_steering_repeated_ack_sent");
    const char *reason = NULL;
    bool known = false;
    for (size_t i = 0; i < nstations && !known; i++)
        known = !memcmp(stations[i], request, 6);
    if (!known)
        reason = "not_backhaul_station";
    else if (!unicast(request + 6))
        reason = "invalid_target";
    else if (s->pending)
        reason = "move_in_progress";
    if (!reason) {
        char bssid[18];
        EM_FORMAT_FIXED(bssid, sizeof(bssid), "%02x:%02x:%02x:%02x:%02x:%02x", request[6], request[7], request[8], request[9],
                 request[10], request[11]);
        reason = c->closed || !c->executor ? "session_closed" : c->executor(c->ctx, bssid);
    }
    cJSON_Delete(c->last);
    c->last = cJSON_CreateObject();
    cJSON_AddNumberToObject(c->last, "mid", m->mid);
    cJSON_AddItemToObject(c->last, "request", request_json(request));
    cJSON_AddItemToObject(c->last, "refused", reason ? cJSON_CreateString(reason) : cJSON_CreateNull());
    if (reason) {
        respond(c, m->mid, request, ASSOCIATION_FAILED, now, out);
        EM_FORMAT_FIXED(label, sizeof(label), "backhaul_steering_refused_%s", reason);
        return record(c, label);
    }
    s->pending = true;
    s->pending_mid = m->mid;
    memcpy(s->request, request, 14);
    s->deadline = now + DEADLINE;
    return record(c, "backhaul_steering_started");
}

void em_bh_tick(em_bh_coordinator *c, double now, bool source_available, em_frames *out)
{
    em_bh_shared *s = c->shared;
    if (!s || !s->pending)
        return;
    char bssid[18];
    const uint8_t *r = s->request;
    EM_FORMAT_FIXED(bssid, sizeof(bssid), "%02x:%02x:%02x:%02x:%02x:%02x", r[6], r[7], r[8], r[9], r[10], r[11]);
    const char *why = NULL;
    int result = c->outcome ? c->outcome(c->ctx, bssid, &why) : -1;
    bool expired = now >= s->deadline;
    if (result == 0 && !expired)
        return;
    if (!source_available) {
        /* the pod's State is being read again after the move: answer on a later tick,
         * unless it stays away well past the deadline */
        if (now < s->deadline + 30)
            return;
        /* {**last, "mid": mid, "result": "unanswered"} */
        if (!c->last)
            c->last = cJSON_CreateObject();
        cJSON_DeleteItemFromObjectCaseSensitive(c->last, "mid");
        cJSON_DeleteItemFromObjectCaseSensitive(c->last, "result");
        cJSON_AddNumberToObject(c->last, "mid", s->pending_mid);
        cJSON_AddStringToObject(c->last, "result", "unanswered");
        s->pending = false;
        record(c, "backhaul_steering_unanswered");
        return;
    }
    uint8_t request[14];
    memcpy(request, s->request, 14);
    uint16_t mid = s->pending_mid;
    s->pending = false;
    bool applied = result == 1;
    respond(c, mid, request, applied ? 0 : ASSOCIATION_FAILED, now, out);
    cJSON_Delete(c->last);
    c->last = cJSON_CreateObject();
    cJSON_AddNumberToObject(c->last, "mid", mid);
    cJSON_AddItemToObject(c->last, "request", request_json(request));
    cJSON_AddStringToObject(c->last, "result", applied ? "succeeded" : "failed");
    cJSON_AddItemToObject(c->last, "reason", applied ? cJSON_CreateNull() : cJSON_CreateString(why ? why : "deadline"));
    record(c, applied ? "backhaul_steering_succeeded" : "backhaul_steering_failed");
}

cJSON *em_bh_status(const em_bh_coordinator *c)
{
    cJSON *o = cJSON_CreateObject(), *counts = cJSON_AddObjectToObject(o, "counts");
    for (size_t i = 0; i < c->counts.n; i++)
        cJSON_AddNumberToObject(counts, c->counts.items[i].name, c->counts.items[i].count);
    if (c->shared && c->shared->pending) {
        cJSON *p = cJSON_AddObjectToObject(o, "pending");
        cJSON_AddNumberToObject(p, "mid", c->shared->pending_mid);
        cJSON_AddItemToObject(p, "request", request_json(c->shared->request));
    } else {
        cJSON_AddNullToObject(o, "pending");
    }
    cJSON_AddItemToObject(o, "last", c->last ? cJSON_Duplicate(c->last, true) : cJSON_CreateNull());
    return o;
}
