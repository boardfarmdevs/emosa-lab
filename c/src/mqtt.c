/* A minimal MQTT 3.1.1 subscriber (the reference uses paho-mqtt). */
#define _GNU_SOURCE /* NOLINT(cert-dcl37-c,cert-dcl51-cpp): QUALITY.md §5 */
#include "mqtt.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define KEEPALIVE 30
#define MAX_PACKET (4 * 1024 * 1024)

typedef enum { M_IDLE, M_CONNECTING, M_AWAIT_CONNACK, M_AWAIT_SUBACK, M_READY } phase;

struct em_mqtt {
    char host[256], topic[256], client_id[128];
    int port, fd;
    phase phase;
    double next_attempt, backoff, last_sent;
    em_mqtt_deliver deliver;
    void *ctx;
    uint8_t *in;
    size_t len, cap;
};

em_mqtt *em_mqtt_open(const char *host, int port, const char *topic, const char *client_id,
                      em_mqtt_deliver deliver, void *ctx)
{
    em_mqtt *m = em_calloc(1, sizeof(*m));
    if (!em_copy(m->host, sizeof(m->host), host) || !em_copy(m->topic, sizeof(m->topic), topic) ||
        !em_copy(m->client_id, sizeof(m->client_id), client_id)) {
        free(m);
        return NULL;
    }
    m->port = port;
    m->fd = -1;
    m->backoff = 1;
    m->deliver = deliver;
    m->ctx = ctx;
    return m;
}

static void drop(em_mqtt *m, double now)
{
    if (m->fd >= 0)
        close(m->fd);
    m->fd = -1;
    m->phase = M_IDLE;
    m->len = 0;
    m->next_attempt = now + m->backoff;
    m->backoff = m->backoff * 2 > 30 ? 30 : m->backoff * 2;
}

void em_mqtt_close(em_mqtt *m)
{
    if (!m)
        return;
    if (m->fd >= 0) {
        static const uint8_t disconnect[] = {0xE0, 0x00};
        (void)!write(m->fd, disconnect, sizeof(disconnect));
        close(m->fd);
    }
    free(m->in);
    free(m);
}

int em_mqtt_fd(const em_mqtt *m, short *events)
{
    *events = m->phase == M_CONNECTING ? POLLOUT : POLLIN;
    return m->fd;
}

bool em_mqtt_connected(const em_mqtt *m) { return m->phase == M_READY; }

/* one whole packet (small: CONNECT, SUBSCRIBE, PINGREQ, PUBACK) */
static bool send_packet(em_mqtt *m, uint8_t type, const uint8_t *body, size_t n, double now)
{
    uint8_t head[5];
    size_t h = 0;
    head[h++] = type;
    size_t rest = n;
    do {
        uint8_t byte = rest % 128;
        rest /= 128;
        head[h++] = byte | (rest ? 0x80 : 0);
    } while (rest);
    uint8_t *packet = em_malloc(h + n);
    memcpy(packet, head, h);
    if (n)
        memcpy(packet + h, body, n);
    ssize_t sent = send(m->fd, packet, h + n, MSG_NOSIGNAL);
    free(packet);
    m->last_sent = now;
    return sent == (ssize_t)(h + n);
}

static size_t put_string(uint8_t *out, const char *text)
{
    size_t n = strlen(text);
    out[0] = (uint8_t)(n >> 8);
    out[1] = (uint8_t)n;
    memcpy(out + 2, text, n);
    return n + 2;
}

static bool send_connect(em_mqtt *m, double now)
{
    uint8_t body[256];
    size_t n = put_string(body, "MQTT");
    body[n++] = 4;    /* 3.1.1 */
    body[n++] = 0x02; /* clean session */
    body[n++] = 0;
    body[n++] = KEEPALIVE;
    n += put_string(body + n, m->client_id);
    return send_packet(m, 0x10, body, n, now);
}

static bool send_subscribe(em_mqtt *m, double now)
{
    uint8_t body[300];
    body[0] = 0;
    body[1] = 1; /* packet identifier */
    size_t n = 2 + put_string(body + 2, m->topic);
    body[n++] = 0; /* QoS 0 */
    return send_packet(m, 0x82, body, n, now);
}

static void start(em_mqtt *m, double now)
{
    char port[8];
    EM_FORMAT_FIXED(port, sizeof(port), "%d", m->port);
    struct addrinfo hints = {.ai_family = AF_UNSPEC, .ai_socktype = SOCK_STREAM}, *res = NULL;
    if (getaddrinfo(m->host, port, &hints, &res) || !res) {
        drop(m, now);
        return;
    }
    m->fd = socket(res->ai_family, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (m->fd < 0 || (connect(m->fd, res->ai_addr, res->ai_addrlen) && errno != EINPROGRESS)) {
        freeaddrinfo(res);
        drop(m, now);
        return;
    }
    freeaddrinfo(res);
    m->phase = M_CONNECTING;
    m->last_sent = now;
}

/* the packets complete in the input buffer */
static bool handle(em_mqtt *m, double now)
{
    for (;;) {
        if (m->len < 2)
            return true;
        size_t rest = 0, h = 1, mult = 1;
        for (;;) {
            if (h >= m->len)
                return true; /* the length is incomplete */
            uint8_t byte = m->in[h++];
            rest += (byte & 0x7F) * mult;
            mult *= 128;
            if (!(byte & 0x80))
                break;
            if (h > 4)
                return false;
        }
        if (rest > MAX_PACKET)
            return false;
        if (m->len < h + rest)
            return true;
        uint8_t type = m->in[0];
        const uint8_t *body = m->in + h;
        switch (type >> 4) {
        case 2: /* CONNACK */
            if (m->phase != M_AWAIT_CONNACK || rest != 2 || body[1] != 0 || !send_subscribe(m, now))
                return false;
            m->phase = M_AWAIT_SUBACK;
            break;
        case 9: /* SUBACK */
            if (m->phase != M_AWAIT_SUBACK || rest < 3 || body[2] == 0x80)
                return false;
            m->phase = M_READY;
            m->backoff = 1;
            break;
        case 3: { /* PUBLISH */
            if (rest < 2)
                return false;
            size_t tlen = (size_t)body[0] << 8 | body[1], at = 2 + tlen;
            int qos = (type >> 1) & 3;
            if (at + (qos ? 2 : 0) > rest)
                return false;
            char *topic = strndup((const char *)body + 2, tlen);
            uint16_t pid = qos ? (uint16_t)(body[at] << 8 | body[at + 1]) : 0;
            at += qos ? 2 : 0;
            if (m->phase == M_READY)
                m->deliver(m->ctx, topic, body + at, rest - at, type & 1);
            free(topic);
            if (qos == 1) {
                uint8_t ack[2] = {(uint8_t)(pid >> 8), (uint8_t)pid};
                send_packet(m, 0x40, ack, 2, now);
            }
            break;
        }
        case 13: /* PINGRESP */
            break;
        default:
            break;
        }
        memmove(m->in, m->in + h + rest, m->len - h - rest);
        m->len -= h + rest;
    }
}

void em_mqtt_pump(em_mqtt *m, double now)
{
    if (m->phase == M_IDLE) {
        if (now >= m->next_attempt)
            start(m, now);
        return;
    }
    if (m->phase == M_CONNECTING) {
        struct pollfd p = {m->fd, POLLOUT, 0};
        if (poll(&p, 1, 0) <= 0) {
            if (now - m->last_sent > 10)
                drop(m, now);
            return;
        }
        int err = 0;
        socklen_t len = sizeof(err);
        getsockopt(m->fd, SOL_SOCKET, SO_ERROR, &err, &len);
        if (err || !send_connect(m, now)) {
            drop(m, now);
            return;
        }
        m->phase = M_AWAIT_CONNACK;
    }
    for (;;) {
        if (m->cap - m->len < 65536) {
            size_t cap = m->cap ? m->cap * 2 : 131072;
            if (cap > MAX_PACKET + 65536) {
                drop(m, now);
                return;
            }
            m->in = em_realloc(m->in, cap);
            m->cap = cap;
        }
        ssize_t n = recv(m->fd, m->in + m->len, m->cap - m->len, MSG_DONTWAIT);
        if (n > 0) {
            m->len += (size_t)n;
            continue;
        }
        if (n == 0 || (errno != EAGAIN && errno != EWOULDBLOCK)) {
            drop(m, now);
            return;
        }
        break;
    }
    if (!handle(m, now)) {
        drop(m, now);
        return;
    }
    if (m->phase != M_READY && now - m->last_sent > 10) {
        drop(m, now); /* no CONNACK or SUBACK */
        return;
    }
    if (now - m->last_sent >= KEEPALIVE / 2.0 && !send_packet(m, 0xC0, NULL, 0, now))
        drop(m, now);
}
