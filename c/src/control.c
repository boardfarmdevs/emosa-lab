/* SPDX-License-Identifier: Apache-2.0 */
/* The provisioned agent's control procedures. Mirrors emosa.wire.channel,
 * emosa.wire.reporting_policy and emosa.wire.steering. */
#include "control.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>

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
    em_buf *grown = em_realloc(out->frames, (out->count + f.count) * sizeof(em_buf));
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

static void add_hex(cJSON *o, const char *name, const uint8_t mac[6])
{
    char *hex = em_hex(mac, 6);
    cJSON_AddStringToObject(o, name, hex);
    free(hex);
}

static cJSON *channel_list(const uint8_t *channels, uint8_t n)
{
    cJSON *list = cJSON_CreateArray();
    for (uint8_t k = 0; k < n; k++)
        cJSON_AddItemToArray(list, cJSON_CreateNumber(channels[k]));
    return list;
}

/* the agent's radios for the procedures: the configured list, or the represented radio alone */
static size_t control_radios(const em_control *c, em_control_radio out[4])
{
    if (c->nradios) {
        memcpy(out, c->radios, c->nradios * sizeof(*out));
        return c->nradios;
    }
    memset(out, 0, sizeof(*out));
    memcpy(out[0].ruid, c->radio->ruid, 6);
    out[0].op_class = 81;
    out[0].channel = c->radio->channel;
    out[0].tx_power_dbm = c->tx_power_dbm;
    out[0].max_eirp_dbm = c->max_eirp_dbm;
    out[0].measured = true;
    return 1;
}

/* CLASS_CHANNELS: the operating classes a reported radio may be on and their channels (2.4 GHz,
 * and the non-DFS 5 GHz UNII-1 and UNII-3 classes of a pod's second radio, spec 9) */
static bool class_channels(uint8_t op_class, const uint8_t **list, size_t *n)
{
    static const uint8_t ch81[13] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13}, ch115[4] = {36, 40, 44, 48},
                         ch124[4] = {149, 153, 157, 161}, ch125[8] = {149, 153, 157, 161, 165, 169, 173, 177};
    *list = op_class == 81 ? ch81 : op_class == 115 ? ch115 : op_class == 124 ? ch124 : op_class == 125 ? ch125 : NULL;
    *n = op_class == 81 ? 13 : op_class == 115 || op_class == 124 ? 4 : op_class == 125 ? 8 : 0;
    return *list != NULL;
}

/* BAND_CLASSES (advertised): the classes a radio operating in this class advertises, every class
 * of its band, their count */
static size_t advertised(uint8_t op_class, const uint8_t **classes)
{
    static const uint8_t band24[1] = {81}, band50[3] = {115, 124, 125};
    *classes = op_class == 81 ? band24 : band50;
    return op_class == 81 ? 1 : op_class == 115 || op_class == 124 || op_class == 125 ? 3 : 0;
}

/* one radio a channel request names: its policy record entry and whether it is declined */
typedef struct {
    size_t radio; /* its index among the agent's radios */
    bool preferences, power, decline;
    cJSON *entry; /* {"ruid", "preferences", "power_limit_dbm", "ignored"} */
} named_radio;

static named_radio *name_radio(const em_control_radio *radios, size_t k, named_radio *named, size_t *nnamed)
{
    for (size_t i = 0; i < *nnamed; i++)
        if (named[i].radio == k)
            return &named[i];
    named_radio *x = &named[(*nnamed)++];
    memset(x, 0, sizeof(*x));
    x->radio = k;
    x->entry = cJSON_CreateObject();
    add_hex(x->entry, "ruid", radios[k].ruid);
    cJSON_AddArrayToObject(x->entry, "preferences");
    cJSON_AddNullToObject(x->entry, "power_limit_dbm");
    cJSON_AddArrayToObject(x->entry, "ignored");
    return x;
}

/* The selected policy (emosa.wire.channel.selected_policy): each radio the request names, in the
 * order first named (every radio for a request naming none), its preferences in its own operating
 * class, power limit and the classes ignored, and whether the pod cannot do it (*named owns the
 * entries). */
static em_reason channel_policy(const em_message *m, const em_control_radio *radios, size_t nradios,
                                named_radio named[4], size_t *nnamed)
{
    *nnamed = 0;
    for (size_t i = 0; i < m->ntlvs; i++) {
        const em_tlv *t = &m->tlvs[i];
        if (t->kind != 0x8B && t->kind != 0x8D)
            return EM_UNSUPPORTED_OPERATION; /* an unimplemented companion */
        size_t k = 0;
        while (t->len >= 6 && k < nradios && memcmp(t->value, radios[k].ruid, 6))
            k++;
        bool preferences = t->kind == 0x8B;
        if (t->len < (preferences ? 7 : 6) || k == nradios)
            return EM_INVALID_INPUT; /* truncated or foreign */
        named_radio *x = name_radio(radios, k, named, nnamed);
        if (!preferences) {
            if (x->power || t->len != 7)
                return EM_INVALID_INPUT;
            x->power = true;
            cJSON_ReplaceItemInObject(x->entry, "power_limit_dbm", cJSON_CreateNumber((int8_t)t->value[6]));
            if ((int8_t)t->value[6] < radios[k].tx_power_dbm)
                x->decline = true; /* power actuation requires a qualified mapping */
            continue;
        }
        if (x->preferences)
            return EM_INVALID_INPUT; /* duplicate */
        x->preferences = true;
        const uint8_t *own, *classes;
        size_t nown, nclasses = advertised(radios[k].op_class, &classes);
        cJSON *prefs = cJSON_GetObjectItemCaseSensitive(x->entry, "preferences"),
              *ignored = cJSON_GetObjectItemCaseSensitive(x->entry, "ignored");
        uint8_t count = t->value[6];
        if (count > 8)
            return EM_INVALID_INPUT;
        const uint8_t *p = t->value + 7, *end = t->value + t->len;
        bool specified[3][16] = {{0}}; /* by advertised class, by index in its channels */
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
            size_t which = 0;
            while (which < nclasses && classes[which] != opclass)
                which++;
            if (which == nclasses) { /* a class the radio does not advertise (RDK's 40 MHz 83/84) */
                cJSON *other = cJSON_CreateObject();
                cJSON_AddNumberToObject(other, "class", opclass);
                cJSON_AddItemToObject(other, "channels", channel_list(channels, n));
                cJSON_AddItemToArray(ignored, other);
                continue;
            }
            class_channels(opclass, &own, &nown);
            bool applies[16] = {0};
            for (int j = 0; j < n; j++) {
                size_t at = 0;
                while (at < nown && own[at] != channels[j])
                    at++;
                if (at == nown || applies[at])
                    return EM_INVALID_INPUT; /* outside the class, or repeated */
                applies[at] = true;
            }
            for (size_t at = 0; !n && at < nown; at++)
                applies[at] = true;
            for (size_t at = 0; at < nown; at++) {
                if (applies[at] && specified[which][at])
                    return EM_INVALID_INPUT; /* overlapping preferences */
                specified[which][at] = specified[which][at] || applies[at];
                if (applies[at] && own[at] == radios[k].channel && score == 0)
                    x->decline = true; /* forbids the sole operable channel, in any class (124, 125: 149) */
            }
            cJSON *pref = cJSON_CreateObject();
            cJSON_AddNumberToObject(pref, "class", opclass);
            cJSON_AddItemToObject(pref, "channels", channel_list(channels, n));
            cJSON_AddNumberToObject(pref, "preference", score);
            cJSON_AddNumberToObject(pref, "reason", reason);
            cJSON_AddItemToArray(prefs, pref);
        }
        if (p != end)
            return EM_INVALID_INPUT;
    }
    if (!*nnamed) /* a request naming no radio: accepted for every radio, as for one radio */
        for (size_t k = 0; k < nradios; k++)
            name_radio(radios, k, named, nnamed);
    return EM_OK;
}

static void free_named(named_radio *named, size_t n)
{
    for (size_t i = 0; i < n; i++)
        cJSON_Delete(named[i].entry);
}

static const char *channel(em_control *c, const em_message *m, em_frames *out, em_reason *error)
{
    if (seen(c, m->message_type, m->mid))
        return "duplicate_channel_request";
    em_control_radio radios[4];
    size_t nradios = control_radios(c, radios);
    for (size_t k = 0; k < nradios; k++)
        if (!radios[k].measured || radios[k].tx_power_dbm > radios[k].max_eirp_dbm) {
            *error = EM_NOT_READY; /* measured operating parameters unavailable */
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
    named_radio named[4];
    size_t nnamed = 0;
    em_reason r = channel_policy(m, radios, nradios, named, &nnamed);
    bool declined = false, accepted = false;
    for (size_t i = 0; i < nnamed; i++) {
        declined = declined || named[i].decline;
        accepted = accepted || !named[i].decline;
    }
    if (r == EM_OK && accepted && c->keep_channel_policy) {
        /* the accepted radios' preferences (spec 9: each radio its own) */
        cJSON *policy = cJSON_CreateObject(), *list;
        cJSON_AddStringToObject(policy, "status", "accepted_no_adjustment");
        cJSON_AddNumberToObject(policy, "mid", m->mid);
        add_hex(policy, "controller", c->binding.controller_al);
        add_hex(policy, "local_al", c->binding.local_al);
        list = cJSON_AddArrayToObject(policy, "radios");
        for (size_t i = 0; i < nnamed; i++)
            if (!named[i].decline)
                cJSON_AddItemToArray(list, cJSON_Duplicate(named[i].entry, 1));
        if (!c->keep_channel_policy(c->keep_ctx, policy))
            r = EM_NOT_READY; /* "channel preference persistence failed" */
        cJSON_Delete(policy);
    }
    if (r != EM_OK) {
        free_named(named, nnamed);
        *error = r;
        return NULL;
    }
    /* one Channel Selection Response per radio the request names; one Operating Channel Report
     * TLV per radio of the agent */
    uint8_t response[4][7], report[4][10];
    em_tlv rt[4], ot[4];
    for (size_t i = 0; i < nnamed; i++) {
        memcpy(response[i], radios[named[i].radio].ruid, 6);
        response[i][6] = named[i].decline ? 0x02 : 0x00;
        rt[i] = (em_tlv){0x8E, 7, response[i]};
    }
    for (size_t k = 0; k < nradios; k++) {
        memcpy(report[k], radios[k].ruid, 6);
        report[k][6] = 1;
        report[k][7] = radios[k].op_class;
        report[k][8] = (uint8_t)radios[k].channel;
        report[k][9] = (uint8_t)radios[k].tx_power_dbm;
        ot[k] = (em_tlv){0x8F, 10, report[k]};
    }
    free_named(named, nnamed);
    if ((r = send(c, 0x8007, m->mid, rt, nnamed, out)) != EM_OK ||
        (r = send(c, 0x8008, next_mid(c), ot, nradios, out)) != EM_OK) {
        *error = r;
        return NULL;
    }
    remember(c, m->message_type, m->mid);
    c->channel_policy_declined = declined;
    return declined ? "channel_selection_declined" : "channel_selection_accepted";
}

static void system_utc(char out[40])
{
    struct timespec now;
    struct tm tm;
    clock_gettime(CLOCK_REALTIME, &now);
    /* the system clock: a year of four digits (a failure here is the platform's) */
    size_t n = gmtime_r(&now.tv_sec, &tm) ? strftime(out, 40, "%Y-%m-%dT%H:%M:%S", &tm) : 0;
    if (!n)
        abort();
    EM_FORMAT_FIXED(out + n, 40 - n, ".%03ldZ", now.tv_nsec / 1000000);
}

/* one "scan not supported" result (RUID, class, channel, status 0x01), appended */
static void add_result(uint8_t (**results)[9], size_t *n, size_t *cap, const uint8_t *ruid, uint8_t op_class,
                       uint8_t channel)
{
    if (*n == *cap) {
        *cap = *cap ? *cap * 2 : 16;
        *results = em_realloc(*results, *cap * sizeof(**results));
    }
    memcpy((*results)[*n], ruid, 6);
    (*results)[*n][6] = op_class;
    (*results)[*n][7] = channel;
    (*results)[(*n)++][8] = 0x01; /* scan not supported */
}

/* Channel Scan Request: Ack, then a report of "scan not supported" results, as many as
 * the request names for the pod's radio (the reference's list; the request's TLV bounds
 * them to about 65 000). */
static const char *scan(em_control *c, const em_message *m, em_frames *out, em_reason *error)
{
    if (seen(c, m->message_type, m->mid))
        return "duplicate_channel_request";
    em_control_radio radios[4];
    size_t nradios = control_radios(c, radios);
    for (size_t k = 0; k < nradios; k++)
        if (!radios[k].measured || radios[k].tx_power_dbm > radios[k].max_eirp_dbm) {
            *error = EM_NOT_READY; /* measured operating parameters unavailable */
            return NULL;
        }
    const em_tlv *request = NULL;
    for (size_t i = 0; i < m->ntlvs; i++)
        if (m->tlvs[i].kind == 0xA6) {
            if (request) {
                *error = EM_INVALID_INPUT;
                return NULL;
            }
            request = &m->tlvs[i];
        }
    if (!request || request->len < 2) {
        *error = EM_INVALID_INPUT; /* one Channel Scan Request TLV required */
        return NULL;
    }
    const uint8_t *d = request->value;
    size_t length = request->len, offset = 2, nresults = 0, cap = 0;
    uint8_t(*results)[9] = NULL;
    for (unsigned radio = 0; radio < d[1]; radio++) {
        if (length < offset + 7) {
            free(results);
            *error = EM_INVALID_INPUT;
            return NULL;
        }
        const uint8_t *ruid = d + offset;
        unsigned nclasses = d[offset + 6];
        /* one of the agent's radios (spec 9: a second radio too), on its own class and channel */
        const em_control_radio *mine = NULL;
        for (size_t k = 0; k < nradios && !mine; k++)
            if (!memcmp(ruid, radios[k].ruid, 6))
                mine = &radios[k];
        bool ours = mine != NULL;
        offset += 7;
        if (ours && !nclasses)
            add_result(&results, &nresults, &cap, ruid, mine->op_class, (uint8_t)mine->channel);
        for (unsigned k = 0; k < nclasses; k++) {
            if (length < offset + 2 || length < offset + 2 + d[offset + 1]) {
                free(results);
                *error = EM_INVALID_INPUT;
                return NULL;
            }
            uint8_t op_class = d[offset], n = d[offset + 1];
            const uint8_t *channels = d + offset + 2;
            uint8_t current = ours ? (uint8_t)mine->channel : 0;
            if (!n && ours && op_class == mine->op_class) {
                channels = &current;
                n = 1;
            }
            for (unsigned j = 0; ours && j < n; j++)
                add_result(&results, &nresults, &cap, ruid, op_class, channels[j]);
            offset += 2 + d[offset + 1];
        }
    }
    if (offset != length) {
        free(results);
        *error = EM_INVALID_INPUT; /* trailing octets */
        return NULL;
    }
    char stamp[40];
    (c->utc ? c->utc : system_utc)(stamp);
    uint8_t timestamp[41];
    timestamp[0] = (uint8_t)strlen(stamp);
    memcpy(timestamp + 1, stamp, timestamp[0]);
    em_tlv *report = em_calloc(nresults + 1, sizeof(*report));
    report[0] = (em_tlv){0xA8, (uint16_t)(timestamp[0] + 1), timestamp};
    for (size_t i = 0; i < nresults; i++)
        report[i + 1] = (em_tlv){0xA7, 9, results[i]};
    em_reason r;
    if ((r = send(c, 0x8000, m->mid, NULL, 0, out)) != EM_OK ||
        (r = send(c, 0x801C, next_mid(c), report, nresults + 1, out)) != EM_OK) {
        free(report);
        free(results);
        *error = r;
        return NULL;
    }
    free(report);
    free(results);
    remember(c, m->message_type, m->mid);
    return "channel_scan_not_supported_reported";
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

/* a radio list's entries (each `stride` octets from `list`, its RUID first) name only the agent's
 * radios, each once (reporting_policy.decode_policy, spec 9) */
static bool own_radios(const em_control *c, const uint8_t *list, size_t n, size_t stride)
{
    em_control_radio radios[4];
    size_t nradios = control_radios(c, radios);
    for (size_t i = 0; i < n; i++) {
        const uint8_t *ruid = list + i * stride;
        bool ours = false;
        for (size_t k = 0; k < nradios && !ours; k++)
            ours = !memcmp(ruid, radios[k].ruid, 6);
        for (size_t j = 0; ours && j < i; j++)
            ours = memcmp(ruid, list + j * stride, 6) != 0;
        if (!ours)
            return false;
    }
    return true;
}

static em_reason decode_policy(const em_control *c, const em_message *m)
{
    bool metrics = false, steering = false;
    size_t radios = c->nradios ? c->nradios : 1;
    for (size_t i = 0; i < m->ntlvs; i++) {
        const em_tlv *t = &m->tlvs[i];
        const uint8_t *d = t->value, *end = t->value + t->len;
        if (t->kind == 0x8A) {
            if (metrics || t->len < 2 || t->len != 2 + d[1] * 10)
                return EM_INVALID_INPUT;
            metrics = true;
            if (d[1] > radios)
                return EM_UNSUPPORTED_OPERATION; /* more radios than the agent has */
            if (!own_radios(c, d + 2, d[1], 10))
                return EM_INVALID_INPUT;
            for (size_t k = 0; k < d[1]; k++)
                if (d[2 + k * 10 + 6] > 220)
                    return EM_INVALID_INPUT; /* a reserved RCPI threshold */
        } else if (t->kind == 0x89) {
            if (steering)
                return EM_INVALID_INPUT;
            steering = true;
            const uint8_t *p = d;
            if (!addresses(&p, end) || !addresses(&p, end))
                return EM_INVALID_INPUT;
            if (end - p < 1 || end - p != 1 + 9 * p[0])
                return EM_INVALID_INPUT;
            if (p[0] > radios)
                return EM_UNSUPPORTED_OPERATION;
            if (!own_radios(c, p + 1, p[0], 9))
                return EM_INVALID_INPUT;
            for (size_t k = 0; k < p[0]; k++)
                if (p[1 + k * 9 + 6] > 2 || p[1 + k * 9 + 8] > 220)
                    return EM_INVALID_INPUT; /* a reserved steering field */
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
    if (t->len < offset + 6 * k + 1)
        return EM_INVALID_INPUT;
    for (size_t i = 0; i < k; i++) {
        if (!unicast_mac(d + offset + 6 * i))
            return EM_INVALID_INPUT;
        memcpy(out->stations[i], d + offset + 6 * i, 6);
    }
    out->nstations = k;
    offset += 6 * k;
    size_t n = d[offset++];
    if (t->len != offset + 8 * n)
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
    em_tlv errors[255];
    uint8_t values[255][7];
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
        EM_FORMAT_FIXED(label, sizeof(label), "client_steering_refused_%s", reason);
        return label;
    }
    return "client_steering_started";
}

#define PROBE_LIFETIME 120 /* seconds: an older probe is not a measurement */
#define NOISE_FLOOR_DBM (-96) /* OpenSync reports SNR = signal - its fixed noise floor */

/* The band of an audited operating class (emosa.operating_classes), or NULL. */
static const char *class_band(uint8_t op_class)
{
    if (op_class >= 81 && op_class <= 84)
        return "2.4G";
    if ((op_class >= 115 && op_class <= 117) || op_class == 124 || op_class == 125 || op_class == 128)
        return "5G";
    return NULL;
}

/* a station on a BSS of any radio the agent shows (spec 9) */
static bool on_the_pod(const em_control *c, const uint8_t station[6])
{
    for (size_t r = 0; r <= c->nfurther; r++) {
        const em_radio_view *radio = r ? c->further[r - 1] : c->radio;
        for (size_t b = 0; b < radio->nbss; b++)
            if (associated(&radio->bss[b], station))
                return true;
    }
    return false;
}

/* a radio of the agent operates on this channel in a class of its band (spec 9: the second radio's
 * 149 in class 124 or 125): the pod hears nothing off channel */
static bool heard_on(const em_control *c, uint8_t op_class, uint8_t channel)
{
    em_control_radio radios[4];
    size_t nradios = control_radios(c, radios);
    for (size_t k = 0; k < nradios; k++) {
        const uint8_t *classes;
        size_t nclasses = advertised(radios[k].op_class, &classes);
        bool in_band = false;
        for (size_t i = 0; i < nclasses; i++)
            in_band = in_band || classes[i] == op_class;
        if (radios[k].measured && in_band && radios[k].channel == channel)
            return true;
    }
    return false;
}

static double wall_now(const em_control *c)
{
    if (c->wall)
        return c->wall();
    struct timespec t;
    clock_gettime(CLOCK_REALTIME, &t);
    return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}

/* Unassociated STA Link Metrics Query (spec §3.9), as emosa.wire.unassociated: the Ack
 * refuses a station associated with a BSS of the pod (reason 0x01) and one the pod has
 * not heard on the requested class and channel within PROBE_LIFETIME (0x02); the others
 * follow in a Response with the query's MID, measured from their last probe request. */
static const char *unassociated(em_control *c, const em_message *m, em_frames *out, em_reason *error)
{
    const em_tlv *t = NULL;
    for (size_t i = 0; i < m->ntlvs; i++)
        if (m->tlvs[i].kind == 0x97) {
            if (t) {
                *error = EM_INVALID_INPUT;
                return NULL;
            }
            t = &m->tlvs[i];
        }
    if (!t || t->len < 2) {
        *error = EM_INVALID_INPUT;
        return NULL;
    }
    const uint8_t *d = t->value;
    uint8_t op_class = d[0];
    size_t at = 2, n = 0;
    static uint8_t stations[64][6], channels[64];
    for (size_t k = 0; k < d[1]; k++) {
        if (at + 2 > t->len || at + 2 + 6 * (size_t)d[at + 1] > t->len) {
            *error = EM_INVALID_INPUT;
            return NULL;
        }
        uint8_t channel = d[at];
        size_t count = d[at + 1];
        at += 2;
        for (size_t i = 0; i < count; i++, at += 6) {
            if (!unicast_mac(d + at)) {
                *error = EM_INVALID_INPUT;
                return NULL;
            }
            bool seen = false;
            for (size_t j = 0; j < n && !seen; j++)
                seen = !memcmp(stations[j], d + at, 6);
            if (seen)
                continue;
            if (n == 64) {
                *error = EM_INVALID_INPUT; /* the controller's own bound */
                return NULL;
            }
            channels[n] = channel;
            memcpy(stations[n++], d + at, 6);
        }
    }
    if (at != t->len || n == 0) {
        *error = EM_INVALID_INPUT;
        return NULL;
    }
    double now = wall_now(c);
    static em_tlv errors[64];
    static uint8_t values[64][7], body[2 + 64 * 12];
    size_t nerrors = 0, measured = 0;
    body[0] = op_class;
    for (size_t i = 0; i < n; i++) {
        bool on_pod = on_the_pod(c, stations[i]);
        const em_probe_stats *p = NULL;
        if (!on_pod && heard_on(c, op_class, channels[i]) && c->stats) {
            char mac[18];
            EM_FORMAT_FIXED(mac, sizeof(mac), "%02x:%02x:%02x:%02x:%02x:%02x", stations[i][0],
                     stations[i][1], stations[i][2], stations[i][3], stations[i][4], stations[i][5]);
            p = em_pod_stats_probe(c->stats, mac);
            const char *band = class_band(op_class);
            if (p && (!band || strcmp(p->band, band) || now - p->measured_at < 0 ||
                      now - p->measured_at > PROBE_LIFETIME))
                p = NULL;
        }
        if (!p) {
            values[nerrors][0] = on_pod ? 0x01 : 0x02;
            memcpy(values[nerrors] + 1, stations[i], 6);
            errors[nerrors] = (em_tlv){0xA3, 7, values[nerrors]};
            nerrors++;
            continue;
        }
        /* round half to even, as the reference */
        uint32_t age_ms = (uint32_t)nearbyint((now - p->measured_at) * 1000);
        int rcpi = 2 * ((int)p->snr_db + NOISE_FLOOR_DBM + 110);
        uint8_t *e = body + 2 + 12 * measured++;
        memcpy(e, stations[i], 6);
        e[6] = channels[i];
        e[7] = (uint8_t)(age_ms >> 24);
        e[8] = (uint8_t)(age_ms >> 16);
        e[9] = (uint8_t)(age_ms >> 8);
        e[10] = (uint8_t)age_ms;
        e[11] = (uint8_t)(rcpi < 0 ? 0 : rcpi > 220 ? 220 : rcpi);
    }
    em_reason e = send(c, 0x8000, m->mid, errors, nerrors, out);
    if (e != EM_OK) {
        *error = e;
        return NULL;
    }
    if (c->watch) { /* the stations asked about on the pod's own channel: watch their probes (§3.9) */
        static uint8_t heard[64][6];
        size_t nheard = 0;
        for (size_t i = 0; i < n; i++)
            if (!on_the_pod(c, stations[i]) && heard_on(c, op_class, channels[i]))
                memcpy(heard[nheard++], stations[i], 6);
        if (nheard)
            c->watch(c->watch_ctx, (const uint8_t(*)[6])heard, nheard);
    }
    if (!measured)
        return "unassociated_query_refused";
    body[1] = (uint8_t)measured;
    em_tlv response = {0x98, (uint16_t)(2 + 12 * measured), body};
    /* a response carries its request's MID (IEEE 1905.1) */
    if ((e = send(c, 0x8010, m->mid, &response, 1, out)) != EM_OK) {
        *error = e;
        return NULL;
    }
    return "unassociated_metrics_response_sent";
}

const char *em_control_handle(em_control *c, const em_message *m, em_frames *out, em_reason *error)
{
    *error = EM_OK;
    if (m->message_type != 0x8003 && m->message_type != 0x8004 && m->message_type != 0x8006 &&
        m->message_type != 0x8014 && m->message_type != 0x801B && m->message_type != 0x800F)
        return NULL;
    if (memcmp(m->source, c->binding.controller_al, 6) ||
        memcmp(m->destination, c->binding.local_al, 6) || m->relay) {
        *error = EM_INVALID_INPUT; /* an unbound controller or envelope */
        return NULL;
    }
    switch (m->message_type) {
    case 0x8003: return policy(c, m, out, error);
    case 0x8014: return steer(c, m, out, error);
    case 0x800F: return unassociated(c, m, out, error);
    case 0x801B: return scan(c, m, out, error);
    default: return channel(c, m, out, error);
    }
}
