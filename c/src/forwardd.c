/* SPDX-License-Identifier: Apache-2.0 */
/* emosa-forward-c: the forwarder spec §3.1 names, for a fleet on a gateway: the fleet's front
 * port and its agent ports, which listen on loopback, reached on the fleet's advertise
 * address (the gateway's LAN address), each connection spliced to 127.0.0.1 on its port.
 *
 *   emosa-forward-c CONFIG     the fleet configuration; inert unless it has "forward": true
 *
 * One owner thread (QUALITY.md §2): a poll loop over the listeners and the connection pairs.
 * The listeners bind with IP_FREEBIND, so the forwarder may start before the LAN bridge has
 * its address. In the labs an LXD proxy does the same. */
#define _GNU_SOURCE /* NOLINT(cert-dcl37-c,cert-dcl51-cpp): QUALITY.md §5 (accept4) */
#include <arpa/inet.h>
#include <cjson/cJSON.h>
#include <errno.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "common.h"
#include "jschema.h"
#include "log.h"
#include "version.h"

#define CONFIG_MAX (1024 * 1024)
#define LISTENERS_MAX 129 /* the front port and at most 128 agent ports */
#define PAIRS_MAX 128     /* a pod holds one connection to its agent, and one to the fleet briefly */
#define BUFFER 8192
#define INFO(...) em_log(EM_LOG_INFO, "emosa.forward", __VA_ARGS__)
#define WARN(...) em_log(EM_LOG_WARNING, "emosa.forward", __VA_ARGS__)
#define FAIL(...) em_log(EM_LOG_ERROR, "emosa.forward", __VA_ARGS__)

typedef struct {
    int fd;
    int port;
} listener;

/* one direction of a pair: what was read from one side, not yet written to the other */
typedef struct {
    unsigned char data[BUFFER];
    size_t len, off;
    bool eof;  /* the reading side ended */
    bool shut; /* and the writing side was told */
} flow;

typedef struct {
    int client, upstream; /* -1 when closed */
    int port;
    bool connecting;
    flow up, down; /* client to upstream, upstream to client */
} pair;

static volatile sig_atomic_t stopping;

static void on_signal(int sig)
{
    (void)sig;
    stopping = 1;
}

static cJSON *load(const char *path)
{
    char *text = em_read_file(path, CONFIG_MAX, NULL);
    cJSON *config = text ? cJSON_Parse(text) : NULL;
    free(text);
    if (!config) {
        FAIL("%s: not a JSON fleet configuration", path);
        return NULL;
    }
    em_schema *schema = em_schema_load(em_schema_directory(), "fleet-config");
    char why[256] = "";
    bool valid = schema && em_schema_valid(schema, config, why, sizeof(why));
    em_schema_free(schema);
    if (!valid) {
        FAIL("%s: invalid fleet configuration (%s)", path, schema ? why : "no schema");
        cJSON_Delete(config);
        return NULL;
    }
    return config;
}

static int listen_on(struct in_addr address, int port)
{
    int fd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0), one = 1;
    if (fd < 0)
        return -1;
    struct sockaddr_in sa = {.sin_family = AF_INET, .sin_port = htons((uint16_t)port), .sin_addr = address};
    if (setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)) ||
        setsockopt(fd, IPPROTO_IP, IP_FREEBIND, &one, sizeof(one)) ||
        bind(fd, (struct sockaddr *)&sa, sizeof(sa)) || listen(fd, 16)) {
        close(fd);
        return -1;
    }
    return fd;
}

static void close_pair(pair *p, const char *why)
{
    if (p->client >= 0)
        close(p->client);
    if (p->upstream >= 0)
        close(p->upstream);
    if (why)
        INFO("port %d: connection closed (%s)", p->port, why);
    p->client = p->upstream = -1;
}

static void open_pair(pair *pairs, int fd, int port)
{
    pair *p = NULL;
    for (size_t i = 0; i < PAIRS_MAX && !p; i++)
        if (pairs[i].client < 0)
            p = &pairs[i];
    if (!p) {
        WARN("port %d: %d connections already, one refused", port, PAIRS_MAX);
        close(fd);
        return;
    }
    int up = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (up < 0) {
        close(fd);
        return;
    }
    struct sockaddr_in sa = {.sin_family = AF_INET, .sin_port = htons((uint16_t)port),
                             .sin_addr = {.s_addr = htonl(INADDR_LOOPBACK)}};
    int rc = connect(up, (struct sockaddr *)&sa, sizeof(sa));
    if (rc && errno != EINPROGRESS) {
        WARN("port %d: nothing listens on 127.0.0.1 (%s)", port, strerror(errno));
        close(up);
        close(fd);
        return;
    }
    memset(p, 0, sizeof(*p));
    p->client = fd;
    p->upstream = up;
    p->port = port;
    p->connecting = rc != 0;
}

/* read what one side sent into its flow; false: the pair failed */
static bool fill(int fd, flow *f)
{
    if (f->eof || f->len)
        return true;
    ssize_t n = recv(fd, f->data, sizeof(f->data), 0);
    if (n > 0) {
        f->len = (size_t)n;
        f->off = 0;
    } else if (n == 0) {
        f->eof = true;
    } else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
        return false;
    }
    return true;
}

/* write a flow's data to the other side, and pass its end on once drained; false: failed */
static bool drain(int fd, flow *f)
{
    if (f->off < f->len) {
        ssize_t n = send(fd, f->data + f->off, f->len - f->off, MSG_NOSIGNAL);
        if (n < 0)
            return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR;
        f->off += (size_t)n;
        if (f->off == f->len)
            f->len = f->off = 0;
    }
    if (f->eof && !f->len && !f->shut) {
        (void)shutdown(fd, SHUT_WR);
        f->shut = true;
    }
    return true;
}

static void step(pair *p, short client_events, short upstream_events)
{
    if (p->connecting) {
        if (!(upstream_events & (POLLOUT | POLLERR | POLLHUP)))
            return;
        int error = 0;
        socklen_t len = sizeof(error);
        if (getsockopt(p->upstream, SOL_SOCKET, SO_ERROR, &error, &len) || error) {
            WARN("port %d: nothing listens on 127.0.0.1 (%s)", p->port, strerror(error ? error : errno));
            close_pair(p, NULL);
            return;
        }
        p->connecting = false;
        INFO("port %d: connection forwarded", p->port);
    }
    bool ok = true;
    if (client_events & (POLLIN | POLLHUP | POLLERR))
        ok = ok && fill(p->client, &p->up);
    if (upstream_events & (POLLIN | POLLHUP | POLLERR))
        ok = ok && fill(p->upstream, &p->down);
    ok = ok && drain(p->upstream, &p->up) && drain(p->client, &p->down);
    if (!ok)
        close_pair(p, strerror(errno));
    else if (p->up.shut && p->down.shut)
        close_pair(p, "both ends done");
}

static int serve(listener *listeners, size_t nlisteners)
{
    pair *pairs = em_calloc(PAIRS_MAX, sizeof(pair));
    for (size_t i = 0; i < PAIRS_MAX; i++)
        pairs[i].client = pairs[i].upstream = -1;
    struct pollfd *fds = em_calloc(LISTENERS_MAX + 2 * PAIRS_MAX, sizeof(struct pollfd));
    while (!stopping) {
        size_t n = 0;
        for (size_t i = 0; i < nlisteners; i++)
            fds[n++] = (struct pollfd){.fd = listeners[i].fd, .events = POLLIN};
        for (size_t i = 0; i < PAIRS_MAX; i++) {
            pair *p = &pairs[i];
            short c = 0, u = 0;
            if (p->client >= 0 && p->connecting) {
                u = POLLOUT;
            } else if (p->client >= 0) {
                c = (short)((!p->up.eof && !p->up.len ? POLLIN : 0) | (p->down.len ? POLLOUT : 0));
                u = (short)((!p->down.eof && !p->down.len ? POLLIN : 0) | (p->up.len ? POLLOUT : 0));
            }
            fds[n++] = (struct pollfd){.fd = p->client >= 0 ? p->client : -1, .events = c};
            fds[n++] = (struct pollfd){.fd = p->client >= 0 ? p->upstream : -1, .events = u};
        }
        if (poll(fds, n, -1) < 0) {
            if (errno == EINTR)
                continue;
            FAIL("poll: %s", strerror(errno));
            break;
        }
        for (size_t i = 0; i < nlisteners; i++) {
            if (!(fds[i].revents & POLLIN))
                continue;
            int fd = accept4(listeners[i].fd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
            if (fd >= 0)
                open_pair(pairs, fd, listeners[i].port);
        }
        for (size_t i = 0; i < PAIRS_MAX; i++) {
            const struct pollfd *c = &fds[nlisteners + 2 * i], *u = &fds[nlisteners + 2 * i + 1];
            if (pairs[i].client >= 0 && (c->revents || u->revents))
                step(&pairs[i], c->revents, u->revents);
        }
    }
    for (size_t i = 0; i < PAIRS_MAX; i++)
        if (pairs[i].client >= 0)
            close_pair(&pairs[i], NULL);
    free(fds);
    free(pairs);
    return stopping ? 0 : 1;
}

/* the front port (when the fleet listens on loopback) and the agent ports, on the address */
static size_t plan(const cJSON *config, struct in_addr *address, listener *listeners)
{
    const cJSON *advertise = cJSON_GetObjectItemCaseSensitive(config, "advertise");
    if (!cJSON_IsString(advertise) || inet_pton(AF_INET, advertise->valuestring, address) != 1) {
        FAIL("forward: advertise must be the IPv4 address the pods reach");
        return 0;
    }
    if (address->s_addr == htonl(INADDR_LOOPBACK)) {
        FAIL("forward: advertise %s is where the ports listen, nothing to forward", advertise->valuestring);
        return 0;
    }
    size_t n = 0;
    const cJSON *front_listen = cJSON_GetObjectItemCaseSensitive(config, "listen");
    int front = 0;
    char host[32] = "";
    if (cJSON_IsString(front_listen) &&
        sscanf(front_listen->valuestring, "ptcp:%5d:%31s", &front, host) == 2 && /* NOLINT(cert-err34-c): QUALITY.md §5 */
        !strcmp(host, "127.0.0.1") && front > 0 && front < 65536)
        listeners[n++] = (listener){-1, front};
    const cJSON *ports = cJSON_GetObjectItemCaseSensitive(config, "ports");
    const cJSON *first = cJSON_GetArrayItem(ports, 0), *last = cJSON_GetArrayItem(ports, 1);
    if (!cJSON_IsNumber(first) || !cJSON_IsNumber(last) || first->valueint < 1 || last->valueint > 65535 ||
        first->valueint > last->valueint || last->valueint - first->valueint + 1 > LISTENERS_MAX - 1) {
        FAIL("forward: the agent ports must be a range of at most %d", LISTENERS_MAX - 1);
        return 0;
    }
    for (int port = first->valueint; port <= last->valueint; port++)
        listeners[n++] = (listener){-1, port};
    return n;
}

int main(int argc, char **argv)
{
    em_init();
    if (argc == 2 && !strcmp(argv[1], "--version")) {
        (void)printf("emosa-forward-c %s (%s)\n", EMOSA_VERSION, EMOSA_REVISION);
        return 0;
    }
    if (argc != 2) {
        (void)fprintf(stderr, "usage: emosa-forward-c CONFIG | --version\n");
        return 2;
    }
    em_log_open("forward", NULL);
    cJSON *config = load(argv[1]);
    if (!config)
        return 1;
    if (!cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(config, "forward"))) {
        INFO("%s: no \"forward\": nothing to forward", argv[1]);
        cJSON_Delete(config);
        return 0;
    }
    listener listeners[LISTENERS_MAX];
    struct in_addr address;
    size_t n = plan(config, &address, listeners);
    cJSON_Delete(config);
    if (!n)
        return 1;
    char text[INET_ADDRSTRLEN] = "";
    (void)inet_ntop(AF_INET, &address, text, sizeof(text));
    for (size_t i = 0; i < n; i++) {
        listeners[i].fd = listen_on(address, listeners[i].port);
        if (listeners[i].fd < 0) {
            FAIL("cannot listen on %s:%d: %s", text, listeners[i].port, strerror(errno));
            for (size_t j = 0; j < i; j++)
                close(listeners[j].fd);
            return 1;
        }
    }
    struct sigaction sa = {.sa_handler = on_signal};
    (void)sigemptyset(&sa.sa_mask);
    (void)sigaction(SIGTERM, &sa, NULL);
    (void)sigaction(SIGINT, &sa, NULL);
    INFO("forwarding %s:%d-%d to 127.0.0.1", text, listeners[0].port, listeners[n - 1].port);
    int rc = serve(listeners, n);
    for (size_t i = 0; i < n; i++)
        close(listeners[i].fd);
    return rc;
}
