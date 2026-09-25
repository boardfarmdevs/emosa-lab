/* The provisioned agent's control procedures. Mirrors emosa.wire.channel,
 * emosa.wire.reporting_policy and emosa.wire.steering. */
#include "control.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static bool seen(em_control *c, uint16_t type, uint16_t mid)
{
    uint32_t key = (uint32_t)type << 16 | mid;
    for (size_t i = 0; i < c->nrecent; i++)
        if (c->recent[i] == key)
            return true;
    return false;
}

static void remember(em_control *c, uint16_t type, uint16_t mid)
{
    uint32_t key = (uint32_t)type << 16 | mid;
    if (c->nrecent == 64) {
        memmove(c->recent, c->recent + 1, 63 * sizeof(uint32_t));
        c->nrecent--;
    }
    c->recent[c->nrecent++] = key;
}

static uint16_t next_mid(em_control *c) { return ++c->previous_mid; }

/* Append the frames of one message to *out. */
static em_reason send(em_control *c, uint16_t type, uint16_t mid, const em_tlv *tlvs, size_t n,
                      em_frames *out)
{
    em_frames f;
    em_reason r = em_fragment(c->binding.controller_al, c->binding.local_al, type, mid, tlvs, n,
                              false, 1500, &f);
    if (r != EM_OK)
        return r;
    em_buf *grown = realloc(out->frames, (out->count + f.count) * sizeof(em_buf));
    if (!grown) {
        em_frames_free(&f);
        return EM_NO_MEMORY;
    }
    out->frames = grown;
    memcpy(out->frames + out->count, f.frames, f.count * sizeof(em_buf));
    out->count += f.count;
    free(f.frames);
    return EM_OK;
}

static bool unicast_mac(const uint8_t *m)
{
    static const uint8_t zero[6] = {0};
    return memcmp(m, zero, 6) && !(m[0] & 1);
}

/* -- channel (spec §3.4) ----------------------------------------------------------- */

/* The selected policy: EM_OK with *decline set when the pod cannot do it. */
static em_reason channel_policy(const em_control *c, const em_message *m, bool *decline)
{
    bool preferences = false, power = false;
    *decline = false;
    for (size_t i = 0; i < m->ntlvs; i++) {
        const em_tlv *t = &m->tlvs[i];
        if (t->kind == 0x8B) {
            if (preferences || t->len < 7 || memcmp(t->value, c->radio->ruid, 6))
                return EM_INVALID_INPUT;
            preferences = true;
            uint8_t count = t->value[6];
            if (count > 8)
                return EM_INVALID_INPUT;
            const uint8_t *p = t->value + 7, *end = t->value + t->len;
            bool specified[14] = {0};
            for (int g = 0; g < count; g++) {
                if (end - p < 3)
                    return EM_INVALID_INPUT;
                uint8_t opclass = p[0], n = p[1];
                if (n > 13 || end - p < 3 + n)
                    return EM_INVALID_INPUT;
                const uint8_t *channels = p + 2;
                uint8_t value = p[2 + n], score = value >> 4, reason = value & 15;
                p += 3 + n;
                if (score == 15 || reason > 13 || (reason >= 6 && reason <= 10))
                    return EM_INVALID_INPUT;
                if (opclass != 81)
                    continue; /* a class the radio does not advertise */
                bool applies[14] = {0};
                for (int k = 0; k < n; k++) {
                    if (channels[k] < 1 || channels[k] > 13 || applies[channels[k]])
                        return EM_INVALID_INPUT;
                    applies[channels[k]] = true;
                }
                if (!n)
                    for (int k = 1; k <= 13; k++)
                        applies[k] = true;
                for (int k = 1; k <= 13; k++) {
                    if (applies[k] && specified[k])
                        return EM_INVALID_INPUT; /* overlapping preferences */
                    specified[k] = specified[k] || applies[k];
                }
                if (applies[6] && score == 0)
                    *decline = true; /* forbids the sole operable channel */
            }
            if (p != end)
                return EM_INVALID_INPUT;
        } else if (t->kind == 0x8D) {
            if (power || t->len != 7 || memcmp(t->value, c->radio->ruid, 6))
                return EM_INVALID_INPUT;
            power = true;
            if ((int8_t)t->value[6] < c->tx_power_dbm)
                *decline = true; /* power actuation requires a qualified mapping */
        } else {
            return EM_UNSUPPORTED_OPERATION; /* an unimplemented companion */
        }
    }
    return EM_OK;
}

static const char *channel(em_control *c, const em_message *m, em_frames *out, em_reason *error)
{
    if (seen(c, m->message_type, m->mid))
        return "duplicate_channel_request";
    if (c->tx_power_dbm > c->max_eirp_dbm) {
        *error = EM_NOT_READY;
        return NULL;
    }
    if (m->message_type == 0x8004) {
        if (m->ntlvs) {
            *error = EM_INVALID_INPUT;
            return NULL;
        }
        *error = send(c, 0x8005, m->mid, NULL, 0, out);
        remember(c, m->message_type, m->mid);
        return *error == EM_OK ? "channel_preference_report_sent" : NULL;
    }
    bool decline;
    em_reason r = channel_policy(c, m, &decline);
    if (r != EM_OK) {
        *error = r;
        return NULL;
    }
    uint8_t response[7], report[10];
    memcpy(response, c->radio->ruid, 6);
    response[6] = decline ? 0x02 : 0x00;
    memcpy(report, c->radio->ruid, 6);
    report[6] = 1;
    report[7] = 81;
    report[8] = 6;
    report[9] = (uint8_t)c->tx_power_dbm;
    em_tlv rt = {0x8E, 7, response}, ot = {0x8F, 10, report};
    if ((r = send(c, 0x8007, m->mid, &rt, 1, out)) != EM_OK ||
        (r = send(c, 0x8008, next_mid(c), &ot, 1, out)) != EM_OK) {
        *error = r;
        return NULL;
    }
    remember(c, m->message_type, m->mid);
    c->channel_policy_declined = decline;
    return decline ? "channel_selection_declined" : "channel_selection_accepted";
}

/* -- Multi-AP policy (receipt only) ----------------------------------------------- */

static bool addresses(const uint8_t **p, const uint8_t *end)
{
    if (end - *p < 1 || end - *p < 1 + 6 * (*p)[0])
        return false;
    uint8_t n = (*p)[0];
    const uint8_t *list = *p + 1;
    for (int i = 0; i < n; i++) {
        if (!unicast_mac(list + 6 * i))
            return false;
        for (int j = 0; j < i; j++)
            if (!memcmp(list + 6 * i, list + 6 * j, 6))
                return false;
    }
    *p += 1 + 6 * n;
    return true;
}

static em_reason decode_policy(const em_control *c, const em_message *m)
{
    bool metrics = false, steering = false;
    for (size_t i = 0; i < m->ntlvs; i++) {
        const em_tlv *t = &m->tlvs[i];
        const uint8_t *d = t->value, *end = t->value + t->len;
        if (t->kind == 0x8A) {
            if (metrics || t->len < 2 || t->len != 2 + d[1] * 10)
                return EM_INVALID_INPUT;
            metrics = true;
            if (d[1] > 1)
                return EM_UNSUPPORTED_OPERATION; /* sole-radio policy required */
            if (d[1] && (memcmp(d + 2, c->radio->ruid, 6) || d[8] > 220))
                return EM_INVALID_INPUT;
        } else if (t->kind == 0x89) {
            if (steering)
                return EM_INVALID_INPUT;
            steering = true;
            const uint8_t *p = d;
            if (!addresses(&p, end) || !addresses(&p, end))
                return EM_INVALID_INPUT;
            if (end - p < 1 || end - p != 1 + 9 * p[0])
                return EM_INVALID_INPUT;
            if (p[0] > 1)
                return EM_UNSUPPORTED_OPERATION;
            if (p[0] && (memcmp(p + 1, c->radio->ruid, 6) || p[7] > 2 || p[9] > 220))
                return EM_INVALID_INPUT;
        } else if (t->kind == 0xDB) {
            const uint8_t *p = d;
            if (!addresses(&p, end) || !addresses(&p, end) || end - p != 20)
                return EM_INVALID_INPUT;
        }
        /* any other policy TLV is recorded as received and not applied */
    }
    return EM_OK;
}

static const char *policy(em_control *c, const em_message *m, em_frames *out, em_reason *error)
{
    em_reason r = decode_policy(c, m);
    if (r == EM_OK)
        r = send(c, 0x8000, m->mid, NULL, 0, out);
    if (r != EM_OK) {
        *error = r;
        return NULL;
    }
    remember(c, m->message_type, m->mid);
    return "policy_receipt_ack_sent";
}

/* -- client steering (spec §3.7) -------------------------------------------------- */

em_reason em_decode_steering_request(const em_message *m, em_steering_request *out)
{
    const em_tlv *t = NULL;
    for (size_t i = 0; i < m->ntlvs; i++)
        if (m->tlvs[i].kind == 0x9B) {
            if (t)
                return EM_INVALID_INPUT;
            t = &m->tlvs[i];
        }
    if (!t || t->len < 12)
        return EM_INVALID_INPUT;
    memset(out, 0, sizeof(*out));
    const uint8_t *d = t->value;
    memcpy(out->source_bssid, d, 6);
    out->mandate = d[6] & 0x80;
    out->disassoc_imminent = d[6] & 0x40;
    out->abridged = d[6] & 0x20;
    out->window = (uint16_t)(d[7] << 8 | d[8]);
    out->disassoc_timer = (uint16_t)(d[9] << 8 | d[10]);
    size_t k = d[11], offset = 12;
    if (k > 32 || t->len < offset + 6 * k + 1)
        return EM_INVALID_INPUT;
    for (size_t i = 0; i < k; i++) {
        if (!unicast_mac(d + offset + 6 * i))
            return EM_INVALID_INPUT;
        memcpy(out->stations[i], d + offset + 6 * i, 6);
    }
    out->nstations = k;
    offset += 6 * k;
    size_t n = d[offset++];
    if (n > 32 || t->len != offset + 8 * n)
        return EM_INVALID_INPUT;
    for (size_t i = 0; i < n; i++) {
        memcpy(out->targets[i].bssid, d + offset + 8 * i, 6);
        out->targets[i].op_class = d[offset + 8 * i + 6];
        out->targets[i].channel = d[offset + 8 * i + 7];
    }
    out->ntargets = n;
    for (size_t i = 0; i < k; i++)
        for (size_t j = 0; j < i; j++)
            if (!memcmp(out->stations[i], out->stations[j], 6))
                return EM_INVALID_INPUT;
    if (!unicast_mac(out->source_bssid))
        return EM_INVALID_INPUT;
    return EM_OK;
}

static const em_bss_view *bss(const em_control *c, const uint8_t bssid[6])
{
    for (size_t i = 0; i < c->radio->nbss; i++)
        if (!memcmp(c->radio->bss[i].bssid, bssid, 6))
            return &c->radio->bss[i];
    return NULL;
}

static bool associated(const em_bss_view *b, const uint8_t mac[6])
{
    for (size_t i = 0; b && i < b->nstations; i++)
        if (!memcmp(b->stations[i], mac, 6))
            return true;
    return false;
}

/* Why EMOSA cannot carry out a request it acknowledges, or NULL (spec §3.7). */
static const char *refusal(const em_steering_request *r, const em_bss_view *source, em_reason *error)
{
    static const uint8_t wildcard[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
    if (!r->mandate)
        return "opportunity";
    if (r->nstations != 1)
        return "not_one_station";
    if (!associated(source, r->stations[0]))
        return "station_not_associated";
    if (r->ntargets != 1)
        return "not_one_target";
    if (!memcmp(r->targets[0].bssid, wildcard, 6))
        return "agent_selected_target";
    if (!unicast_mac(r->targets[0].bssid)) {
        *error = EM_INVALID_INPUT;
        return NULL;
    }
    if (!memcmp(r->targets[0].bssid, r->source_bssid, 6) || !r->targets[0].channel)
        return "invalid_target";
    return NULL;
}

static const char *steer(em_control *c, const em_message *m, em_frames *out, em_reason *error)
{
    static char label[96];
    em_steering_request r;
    em_reason e = em_decode_steering_request(m, &r);
    if (e != EM_OK) {
        *error = e;
        return NULL;
    }
    const em_bss_view *source = bss(c, r.source_bssid);
    em_tlv errors[32];
    uint8_t values[32][7];
    size_t nerrors = 0;
    for (size_t i = 0; i < r.nstations; i++)
        if (!associated(source, r.stations[i])) {
            values[nerrors][0] = 0x02; /* STA not associated with the source BSS */
            memcpy(values[nerrors] + 1, r.stations[i], 6);
            errors[nerrors] = (em_tlv){0xA3, 7, values[nerrors]};
            nerrors++;
        }
    bool repeated = seen(c, m->message_type, m->mid);
    if ((e = send(c, 0x8000, m->mid, errors, nerrors, out)) != EM_OK) {
        *error = e;
        return NULL;
    }
    if (repeated)
        return "client_steering_repeated_ack_sent";
    remember(c, m->message_type, m->mid);
    const char *reason = refusal(&r, source, error);
    if (!reason && *error != EM_OK)
        return NULL;
    if (!reason)
        reason = c->executor ? c->executor(c->executor_ctx, &r, m->mid) : "no_executor";
    if (reason && !strcmp(reason, "opportunity")) {
        /* EMOSA steers nothing on its own account: the window ends at once */
        if ((e = send(c, 0x8017, next_mid(c), NULL, 0, out)) != EM_OK) {
            *error = e;
            return NULL;
        }
        return "client_steering_opportunity_completed";
    }
    if (reason) {
        snprintf(label, sizeof(label), "client_steering_refused_%s", reason);
        return label;
    }
    return "client_steering_started";
}

const char *em_control_handle(em_control *c, const em_message *m, em_frames *out, em_reason *error)
{
    *error = EM_OK;
    if (m->message_type != 0x8003 && m->message_type != 0x8004 && m->message_type != 0x8006 &&
        m->message_type != 0x8014)
        return NULL;
    if (memcmp(m->source, c->binding.controller_al, 6) ||
        memcmp(m->destination, c->binding.local_al, 6) || m->relay) {
        *error = EM_INVALID_INPUT; /* an unbound controller or envelope */
        return NULL;
    }
    switch (m->message_type) {
    case 0x8003: return policy(c, m, out, error);
    case 0x8014: return steer(c, m, out, error);
    default: return channel(c, m, out, error);
    }
}
