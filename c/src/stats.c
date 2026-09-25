/* The pod's own statistics (spec §3.6). Mirrors emosa.opensync.stats, with a proto2
 * wire decoder for the sts.Report fields EMOSA uses (the pinned opensync_stats.proto). */
#include "stats.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *const COUNTER_NAMES[EM_COUNTERS] = {
    "tx_bytes", "rx_bytes", "tx_frames", "rx_frames",
    "tx_retries", "rx_retries", "tx_errors", "rx_errors"};
/* Client.Stats field numbers of the counters above */
static const int COUNTER_FIELDS[EM_COUNTERS] = {2, 1, 4, 3, 6, 5, 8, 7};
static const char *const BANDS[7] = {"2.4G", "5G", "5GL", "5GU", "6G", "6GL", "6GU"};
#define TX_RETRIES 4 /* never implied: dppline.c sends it only together with rx_retries */
#define MAX_PAYLOAD 65536
#define MAX_STATIONS 64
#define QM_BATCH 60

/* -- proto2 wire reading ----------------------------------------------------------- */

typedef struct {
    const uint8_t *p, *end;
} reader;

typedef struct {
    int field, wire;
    uint64_t varint;
    const uint8_t *data;
    size_t len;
} pb_field;

static bool varint(reader *r, uint64_t *out)
{
    uint64_t v = 0;
    for (int shift = 0; shift < 64; shift += 7) {
        if (r->p >= r->end)
            return false;
        uint8_t b = *r->p++;
        v |= (uint64_t)(b & 0x7f) << shift;
        if (!(b & 0x80)) {
            *out = v;
            return true;
        }
    }
    return false;
}

/* The next field, or false at the end; *error set on malformed input. */
static bool next(reader *r, pb_field *f, bool *error)
{
    if (r->p >= r->end)
        return false;
    uint64_t key;
    if (!varint(r, &key) || (key >> 3) == 0 || (key >> 3) > 536870911) {
        *error = true;
        return false;
    }
    f->field = (int)(key >> 3);
    f->wire = (int)(key & 7);
    f->data = NULL;
    f->len = 0;
    switch (f->wire) {
    case 0:
        if (!varint(r, &f->varint))
            break;
        return true;
    case 1:
        if (r->end - r->p < 8)
            break;
        f->data = r->p;
        f->len = 8;
        r->p += 8;
        return true;
    case 2: {
        uint64_t n;
        if (!varint(r, &n) || n > (uint64_t)(r->end - r->p))
            break;
        f->data = r->p;
        f->len = (size_t)n;
        r->p += n;
        return true;
    }
    case 5:
        if (r->end - r->p < 4)
            break;
        f->data = r->p;
        f->len = 4;
        r->p += 4;
        return true;
    }
    *error = true;
    return false;
}

static double f64(const uint8_t *d)
{
    uint64_t bits = 0;
    for (int i = 7; i >= 0; i--)
        bits = bits << 8 | d[i];
    double v;
    memcpy(&v, &bits, sizeof(v));
    return v;
}

/* -- the messages ------------------------------------------------------------------ */

typedef struct {
    bool has[14];
    uint64_t v[14];
    double rate[14];
} pb_stats;

typedef struct {
    const uint8_t *mac, *ssid;
    size_t mac_len, ssid_len;
    bool has_mac, connected, has_duration, has_stats;
    uint64_t connect_count, disconnect_count, duration_ms;
    pb_stats stats;
} pb_client;

typedef enum { OK, MALFORMED, INCOMPLETE } decoded;

/* RxStats/TxStats need mcs, nss and bw (fields 1-3) to be initialized */
static decoded rate_stats(const uint8_t *d, size_t n)
{
    reader r = {d, d + n};
    pb_field f = {0};
    bool error = false, seen[4] = {0};
    while (next(&r, &f, &error))
        if (f.field <= 3 && f.wire == 0)
            seen[f.field] = true;
    return error ? MALFORMED : (seen[1] && seen[2] && seen[3]) ? OK : INCOMPLETE;
}

static decoded client(const uint8_t *d, size_t n, pb_client *c)
{
    memset(c, 0, sizeof(*c));
    reader r = {d, d + n};
    pb_field f = {0};
    bool error = false;
    decoded result = OK;
    while (next(&r, &f, &error)) {
        switch (f.field) {
        case 1: if (f.wire == 2) { c->mac = f.data; c->mac_len = f.len; c->has_mac = true; } break;
        case 2: if (f.wire == 2) { c->ssid = f.data; c->ssid_len = f.len; } break;
        case 3: if (f.wire == 0) c->connected = f.varint != 0; break;
        case 4: if (f.wire == 0) c->connect_count = f.varint; break;
        case 5: if (f.wire == 0) c->disconnect_count = f.varint; break;
        case 8: if (f.wire == 0) { c->duration_ms = f.varint; c->has_duration = true; } break;
        case 9:
            if (f.wire == 2) {
                c->has_stats = true;
                reader s = {f.data, f.data + f.len};
                pb_field g = {0};
                while (next(&s, &g, &error)) {
                    if (g.field < 1 || g.field > 13)
                        continue;
                    if (g.wire == 0) {
                        c->stats.has[g.field] = true;
                        c->stats.v[g.field] = g.varint;
                    } else if (g.wire == 1) {
                        c->stats.has[g.field] = true;
                        c->stats.rate[g.field] = f64(g.data);
                    }
                }
            }
            break;
        case 10: case 11:
            if (f.wire == 2) {
                decoded x = rate_stats(f.data, f.len);
                if (x == MALFORMED)
                    return MALFORMED;
                if (x == INCOMPLETE)
                    result = INCOMPLETE;
            }
            break;
        }
    }
    if (error)
        return MALFORMED;
    return c->has_mac ? result : INCOMPLETE;
}

typedef struct {
    const uint8_t *data;
    size_t len;
    uint64_t timestamp_ms, band, channel;
    bool has_timestamp, has_band, has_channel;
} pb_client_report;

static int by_timestamp(const void *a, const void *b)
{
    uint64_t x = ((const pb_client_report *)a)->timestamp_ms,
             y = ((const pb_client_report *)b)->timestamp_ms;
    return x < y ? -1 : x > y;
}

/* -- PodStats ---------------------------------------------------------------------- */

void em_pod_stats_init(em_pod_stats *s, const char *topic, unsigned interval,
                       double (*clock)(void *), void *clock_ctx)
{
    memset(s, 0, sizeof(*s));
    snprintf(s->topic, sizeof(s->topic), "%s", topic);
    s->interval = interval;
    s->lifetime = 3.0 * interval + QM_BATCH;
    s->clock = clock;
    s->clock_ctx = clock_ctx;
}

static em_station_stats *station(em_pod_stats *s, const char *mac)
{
    for (size_t i = 0; i < s->nstations; i++)
        if (!strcmp(s->stations[i].mac, mac))
            return &s->stations[i];
    return NULL;
}

/* one ClientReport; NULL, or why the report is refused */
static const char *client_report(em_pod_stats *s, const pb_client_report *cr)
{
    if (cr->band > 6 || !cr->has_timestamp)
        return "client report without band or time";
    int band = (int)cr->band;
    uint64_t stamp = cr->timestamp_ms;
    if (s->has_last[band] && stamp <= s->last_timestamp[band])
        return "duplicate or out-of-order client report";
    bool broken = !s->has_last[band] || stamp - s->last_timestamp[band] > 1500ull * s->interval;
    if (s->has_last[band] && broken)
        s->gaps++;
    s->has_last[band] = true;
    s->last_timestamp[band] = stamp;
    reader r = {cr->data, cr->data + cr->len};
    pb_field f = {0};
    bool error = false;
    size_t count = 0;
    while (next(&r, &f, &error))
        count += f.field == 3;
    if (count > MAX_STATIONS)
        return "station inventory exceeds the local bound";
    double end = (double)stamp / 1000;
    r = (reader){cr->data, cr->data + cr->len};
    while (next(&r, &f, &error)) {
        if (f.field != 3 || f.wire != 2)
            continue;
        pb_client c;
        client(f.data, f.len, &c);
        char mac[18];
        if (c.mac_len != 17 || !c.has_stats || !c.connected)
            continue; /* a leave or an entry without measurements: nothing to add */
        for (int i = 0; i < 17; i++)
            mac[i] = (char)(c.mac[i] >= 'A' && c.mac[i] <= 'Z' ? c.mac[i] + 32 : c.mac[i]);
        mac[17] = 0;
        bool known[EM_COUNTERS];
        uint64_t delta[EM_COUNTERS];
        for (int n = 0; n < EM_COUNTERS; n++) {
            bool present = c.stats.has[COUNTER_FIELDS[n]];
            if (present && n != TX_RETRIES)
                s->measured[n] = true;
            known[n] = present || s->measured[n];
            delta[n] = present ? c.stats.v[COUNTER_FIELDS[n]] : 0;
        }
        em_station_stats *old = station(s, mac);
        bool events = c.connect_count || c.disconnect_count, now_known = false;
        for (int n = 0; old && n < EM_COUNTERS; n++)
            if (n != TX_RETRIES && !old->known[n] && known[n])
                now_known = true;
        bool same = old && !broken && !events && !now_known && !strcmp(old->band, BANDS[band]) &&
                    end - old->measured_at <= 1.5 * s->interval;
        em_station_stats next_state = {0};
        snprintf(next_state.mac, sizeof(next_state.mac), "%s", mac);
        size_t ssid_len = c.ssid_len < 64 ? c.ssid_len : 64;
        memcpy(next_state.ssid, c.ssid, ssid_len);
        snprintf(next_state.band, sizeof(next_state.band), "%s", BANDS[band]);
        next_state.channel = (unsigned)cr->channel;
        next_state.measured_at = end;
        if (same) {
            for (int n = 0; n < EM_COUNTERS; n++) {
                next_state.known[n] = known[n] && old->known[n];
                next_state.counters[n] = next_state.known[n] ? old->counters[n] + delta[n] : 0;
            }
            next_state.periods = old->periods + 1;
            next_state.epoch_start = old->epoch_start;
        } else {
            for (int n = 0; n < EM_COUNTERS; n++) {
                next_state.known[n] = known[n];
                next_state.counters[n] = delta[n];
            }
            next_state.periods = 1;
            double period = c.has_duration ? (double)c.duration_ms / 1000 : 0;
            next_state.epoch_start = end - (period ? period : s->interval);
        }
        next_state.has_tx_rate = c.stats.has[10];
        next_state.tx_rate = c.stats.rate[10];
        next_state.has_rx_rate = c.stats.has[9];
        next_state.rx_rate = c.stats.rate[9];
        next_state.has_snr = c.stats.has[11];
        next_state.snr = (unsigned)c.stats.v[11];
        if (old) {
            *old = next_state;
        } else if (s->nstations < 256) {
            s->stations[s->nstations++] = next_state;
        }
    }
    return NULL;
}

bool em_pod_stats_receive(em_pod_stats *s, const char *topic, const uint8_t *payload, size_t len,
                          bool retained)
{
    const char *reason = NULL;
    pb_client_report *reports = NULL;
    size_t nreports = 0;
    if (retained || strcmp(topic, s->topic) || len < 1 || len > MAX_PAYLOAD) {
        reason = "not a live report on this pod's topic";
        goto done;
    }
    reader r = {payload, payload + len};
    pb_field f = {0};
    bool error = false, node = false;
    reports = calloc(len, sizeof(*reports));
    while (reports && next(&r, &f, &error)) {
        if (f.field == 1 && f.wire == 2)
            node = true;
        else if (f.field == 5 && f.wire == 2)
            reports[nreports++] = (pb_client_report){.data = f.data, .len = f.len};
    }
    if (!reports || error) {
        reason = "Error parsing message";
        goto done;
    }
    bool incomplete = !node;
    for (size_t i = 0; i < nreports; i++) {
        pb_client_report *cr = &reports[i];
        reader x = {cr->data, cr->data + cr->len};
        while (next(&x, &f, &error)) {
            if (f.field == 1 && f.wire == 0) {
                cr->band = f.varint;
                cr->has_band = true;
            } else if (f.field == 2 && f.wire == 0) {
                cr->timestamp_ms = f.varint;
                cr->has_timestamp = true;
            } else if (f.field == 4 && f.wire == 0) {
                cr->channel = f.varint;
                cr->has_channel = true;
            } else if (f.field == 3 && f.wire == 2) {
                pb_client c;
                decoded d = client(f.data, f.len, &c);
                if (d == MALFORMED)
                    error = true;
                else if (d == INCOMPLETE)
                    incomplete = true;
            }
        }
        incomplete = incomplete || !cr->has_band || !cr->has_channel;
    }
    if (error) {
        reason = "Error parsing message";
        goto done;
    }
    if (incomplete) {
        reason = "incomplete report";
        goto done;
    }
    qsort(reports, nreports, sizeof(*reports), by_timestamp);
    for (size_t i = 0; i < nreports && !reason; i++)
        reason = client_report(s, &reports[i]);
done:
    free(reports);
    if (reason) {
        s->rejected++;
        snprintf(s->last_error, sizeof(s->last_error), "%s", reason);
        s->has_error = true;
        return false;
    }
    s->accepted++;
    s->last_report_at = s->clock(s->clock_ctx);
    s->has_report = true;
    return true;
}

static int by_mac(const void *a, const void *b)
{
    return strcmp(((const em_station_stats *)a)->mac, ((const em_station_stats *)b)->mac);
}

cJSON *em_pod_stats_status(em_pod_stats *s)
{
    double now = s->clock(s->clock_ctx);
    size_t kept = 0;
    for (size_t i = 0; i < s->nstations; i++)
        if (now - s->stations[i].measured_at <= s->lifetime)
            s->stations[kept++] = s->stations[i];
    s->nstations = kept;
    cJSON *o = cJSON_CreateObject(), *measured = cJSON_CreateArray(), *stations = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "topic", s->topic);
    cJSON_AddNumberToObject(o, "reports_accepted", s->accepted);
    cJSON_AddNumberToObject(o, "reports_rejected", s->rejected);
    cJSON_AddNumberToObject(o, "gaps", s->gaps);
    const char *names[EM_COUNTERS];
    size_t n = 0;
    for (int i = 0; i < EM_COUNTERS; i++)
        if (s->measured[i])
            names[n++] = COUNTER_NAMES[i];
    for (size_t i = 0; i < n; i++) /* sorted */
        for (size_t j = i + 1; j < n; j++)
            if (strcmp(names[j], names[i]) < 0) {
                const char *t = names[i];
                names[i] = names[j];
                names[j] = t;
            }
    for (size_t i = 0; i < n; i++)
        cJSON_AddItemToArray(measured, cJSON_CreateString(names[i]));
    cJSON_AddItemToObject(o, "measured_counters", measured);
    if (s->has_error)
        cJSON_AddStringToObject(o, "last_error", s->last_error);
    else
        cJSON_AddNullToObject(o, "last_error");
    if (s->has_report)
        cJSON_AddNumberToObject(o, "last_report_at", s->last_report_at);
    else
        cJSON_AddNullToObject(o, "last_report_at");
    qsort(s->stations, s->nstations, sizeof(em_station_stats), by_mac);
    for (size_t i = 0; i < s->nstations; i++) {
        const em_station_stats *st = &s->stations[i];
        cJSON *x = cJSON_CreateObject();
        cJSON_AddStringToObject(x, "ssid", st->ssid);
        cJSON_AddStringToObject(x, "band", st->band);
        cJSON_AddNumberToObject(x, "channel", st->channel);
        cJSON_AddNumberToObject(x, "measured_at", st->measured_at);
        cJSON_AddNumberToObject(x, "periods", st->periods);
        cJSON_AddNumberToObject(x, "epoch_start", st->epoch_start);
        if (st->has_tx_rate) cJSON_AddNumberToObject(x, "tx_rate_mbps", st->tx_rate);
        else cJSON_AddNullToObject(x, "tx_rate_mbps");
        if (st->has_rx_rate) cJSON_AddNumberToObject(x, "rx_rate_mbps", st->rx_rate);
        else cJSON_AddNullToObject(x, "rx_rate_mbps");
        if (st->has_snr) cJSON_AddNumberToObject(x, "snr_db", st->snr);
        else cJSON_AddNullToObject(x, "snr_db");
        for (int k = 0; k < EM_COUNTERS; k++) {
            if (st->known[k])
                cJSON_AddNumberToObject(x, COUNTER_NAMES[k], (double)st->counters[k]);
            else
                cJSON_AddNullToObject(x, COUNTER_NAMES[k]);
        }
        cJSON_AddItemToObject(stations, st->mac, x);
    }
    cJSON_AddItemToObject(o, "stations", stations);
    return o;
}
