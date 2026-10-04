/* SPDX-License-Identifier: Apache-2.0 */
/* emosa-fleet-c: the fleet (spec §4) in C, as emosa.agent.fleet's main.
 *
 *   emosa-fleet-c serve CONFIG          the front port: every pod handed over to its agent
 *   emosa-fleet-c list CONFIG           every pod and its agent (the registry)
 *   emosa-fleet-c forget CONFIG SERIAL  release a pod: stop its agent, archive its state
 *
 * One owner thread (QUALITY.md §2): a poll loop over the front port and the pods'
 * connections, each a small state machine (identify, start the agent, hand over), with
 * systemctl run as a child the loop reaps. A pod's whole exchange has 5 s. The files
 * are the reference fleet's (fleet.c), so either fleet takes over from the other. */
#include <arpa/inet.h>
#include <cjson/cJSON.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "canon.h"
#include "fleet.h"
#include "jschema.h"
#include "log.h"
#include "version.h"
#include "jsonrpc.h"
#include "proc.h"

#define TIMEOUT 5.0              /* seconds for one pod's whole exchange (spec §4) */
#define MESSAGE_MAX (1024 * 1024) /* a select of four columns is far smaller */
#define CONFIG_MAX (1024 * 1024)

static volatile sig_atomic_t stopping;
static void on_signal(int sig)
{
    (void)sig;
    stopping = 1;
}

#define LOG(...) em_log(EM_LOG_INFO, "emosa.fleet", __VA_ARGS__)
#define WARN(...) em_log(EM_LOG_WARNING, "emosa.fleet", __VA_ARGS__)
#define FAIL(...) em_log(EM_LOG_ERROR, "emosa.fleet", __VA_ARGS__)

static double monotonic(void)
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

/* -- the agents' units --------------------------------------------------------------- */

static bool unit_command(const char *action, const char *pod_id, pid_t *pid)
{
    char unit[96];
    EM_FORMAT_FIXED(unit, sizeof(unit), "emosa-agent@%s", pod_id); /* a valid serial: at most 64 */
    const char *enable[] = {"systemctl", "enable", "-q", unit, NULL};
    const char *other[] = {"systemctl", action, unit, NULL};
    return em_spawn(strcmp(action, "enable") ? other : enable, pid);
}

static void stop_agent(void *ctx, const char *pod_id)
{
    (void)ctx;
    char unit[96];
    EM_FORMAT_FIXED(unit, sizeof(unit), "emosa-agent@%s", pod_id);
    const char *argv[] = {"systemctl", "disable", "-q", "--now", unit, NULL};
    int status;
    if (!em_run(argv, NULL, NULL, &status)) /* as the reference: its outcome not checked */
        WARN("systemctl not started for %s", unit);
}

/* at the fleet's start, an agent of the registry: enabled, then started (restarted when
 * its configuration changed), each waited for; as the reference, a failure only logged */
static void start_agent(void *ctx, const char *pod_id, bool changed)
{
    (void)ctx;
    char unit[96];
    EM_FORMAT_FIXED(unit, sizeof(unit), "emosa-agent@%s", pod_id);
    const char *enable[] = {"systemctl", "enable", "-q", unit, NULL};
    const char *start[] = {"systemctl", changed ? "restart" : "start", unit, NULL};
    int status;
    if (!em_run(enable, NULL, NULL, &status) || status != 0 || !em_run(start, NULL, NULL, &status) || status != 0)
        WARN("%s not started", unit);
    else
        LOG("pod %s: its agent %s from the registry", pod_id,
            changed ? "restarted, its configuration changed," : "started");
}

/* -- the front port ----------------------------------------------------------------- */

typedef enum { SELECTING, ENABLING, STARTING, HANDING_OVER } stage;

typedef struct {
    int fd;
    char peer[64];
    double deadline;
    stage stage;
    pid_t child;
    bool child_running;
    em_fleet_handover handover;
    char *in, *out;
    size_t in_len, in_cap, out_len, out_off;
    em_json_scanner scan; /* where the scan of in stopped */
} conn;

typedef struct {
    em_fleet fleet;
    int listener;
    char unix_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
    conn *conns;
    size_t nconns, concurrency;
    pid_t *orphans; /* systemctl children whose pod gave up waiting: reaped later */
    size_t norphans;
} server;

static int listen_on(const char *name, char *unix_path, size_t unix_size)
{
    int fd = -1, one = 1, port = 0;
    unix_path[0] = 0;
    if (!strncmp(name, "ptcp:", 5)) {
        char host[64];
        const char *colon = strchr(name + 5, ':');
        char *end;
        long p = strtol(name + 5, &end, 10);
        if (!colon || end != colon || p < 1 || p > 65535 || !em_copy(host, sizeof(host), colon + 1))
            return -1;
        port = (int)p;
        struct sockaddr_in sa = {.sin_family = AF_INET, .sin_port = htons((uint16_t)port)};
        if (inet_pton(AF_INET, host, &sa.sin_addr) != 1)
            return -1;
        fd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
        if (fd < 0)
            return -1;
        (void)setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
        if (bind(fd, (struct sockaddr *)&sa, sizeof(sa)) || listen(fd, 16)) {
            close(fd);
            return -1;
        }
        return fd;
    }
    if (!strncmp(name, "punix:", 6)) {
        struct sockaddr_un sa = {.sun_family = AF_UNIX};
        struct stat st;
        if (!em_copy(sa.sun_path, sizeof(sa.sun_path), name + 6) || !em_copy(unix_path, unix_size, name + 6))
            return -1;
        if (!lstat(sa.sun_path, &st) && S_ISSOCK(st.st_mode))
            (void)unlink(sa.sun_path); /* a previous fleet's */
        fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
        if (fd < 0)
            return -1;
        if (bind(fd, (struct sockaddr *)&sa, sizeof(sa)) || listen(fd, 16)) {
            close(fd);
            unix_path[0] = 0;
            return -1;
        }
        return fd;
    }
    return -1;
}

static void queue(conn *c, const cJSON *message)
{
    char *text = cJSON_PrintUnformatted(message);
    size_t n = strlen(text);
    c->out = em_realloc(c->out, c->out_len + n);
    memcpy(c->out + c->out_len, text, n);
    c->out_len += n;
    free(text);
}

static void request(conn *c, const char *method, cJSON *params, long id)
{
    cJSON *m = em_rpc_request(method, params, id);
    queue(c, m);
    cJSON_Delete(m);
}

static void finish(server *s, size_t i, const char *problem)
{
    conn *c = &s->conns[i];
    if (problem)
        WARN("pod connection %s: %s", c->peer, problem);
    if (c->child_running) {
        s->orphans = em_realloc(s->orphans, (s->norphans + 1) * sizeof(*s->orphans));
        s->orphans[s->norphans++] = c->child;
    }
    em_fleet_handover_clear(&c->handover);
    free(c->in);
    free(c->out);
    close(c->fd);
    s->conns[i] = s->conns[--s->nconns];
}

static bool start_child(conn *c, const char *action)
{
    c->child_running = unit_command(action, c->handover.entry.pod_id, &c->child);
    return c->child_running;
}

/* the reply to our request: the next step, or the problem that ends the exchange */
static const char *on_reply(server *s, conn *c, const cJSON *result, char *problem, size_t size)
{
    if (c->stage == SELECTING) {
        em_fleet_outcome outcome = em_fleet_identify(&s->fleet, result, wall(), &c->handover);
        if (outcome == EM_FLEET_REFUSED) {
            WARN("%s (%s)", c->handover.why, c->peer);
            return "";
        }
        if (outcome != EM_FLEET_HANDOVER) {
            (void)em_format(problem, size, "%s", c->handover.why);
            return problem;
        }
        c->stage = ENABLING;
        return start_child(c, "enable") ? NULL : "systemctl could not be started";
    }
    /* HANDING_OVER */
    if (!em_fleet_update_ok(result)) {
        (void)em_format(problem, size, "%s: manager_addr update failed", c->handover.serial);
        return problem;
    }
    const em_fleet_entry *e = &c->handover.entry;
    const cJSON *row = cJSON_GetObjectItemCaseSensitive(cJSON_GetArrayItem(c->handover.update, 1), "row");
    LOG("pod %s (node %s, %s) -> agent AL %s on %s, manager_addr %s", c->handover.serial, c->handover.node,
        c->peer, e->al_mac, e->interface, cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(row, "manager_addr")));
    return "";
}

/* messages read so far: echoes answered, our reply acted on. NULL: keep going; "": done;
 * otherwise the problem */
static const char *on_input(server *s, conn *c, char *problem, size_t size)
{
    size_t start = 0;
    const char *verdict = NULL;
    while (!verdict && start < c->in_len) {
        size_t at, end;
        bool complete = em_json_scan(&c->scan, c->in + start, c->in_len - start, &at, &end);
        start += at;
        if (!complete)
            break;
        cJSON *m = cJSON_ParseWithLength(c->in + start, end - at);
        start += end - at;
        if (!m)
            return "not JSON-RPC";
        cJSON *echo = em_rpc_echo_reply(m);
        const cJSON *result = NULL;
        long id = c->stage == SELECTING ? 1 : 2;
        if (echo) {
            queue(c, echo);
            cJSON_Delete(echo);
        } else if ((c->stage == SELECTING || c->stage == HANDING_OVER) && em_rpc_is_reply(m, id, &result)) {
            verdict = result ? on_reply(s, c, result, problem, size) : "transact refused";
        }
        cJSON_Delete(m);
    }
    memmove(c->in, c->in + start, c->in_len - start);
    c->in_len -= start;
    return verdict;
}

static const char *read_conn(server *s, conn *c, char *problem, size_t size)
{
    for (;;) {
        if (c->in_cap - c->in_len < 4096) {
            if (c->in_cap >= MESSAGE_MAX)
                return "message too long";
            c->in_cap = c->in_cap ? c->in_cap * 2 : 8192;
            c->in = em_realloc(c->in, c->in_cap);
        }
        ssize_t r = recv(c->fd, c->in + c->in_len, c->in_cap - c->in_len, MSG_DONTWAIT);
        if (r > 0) {
            c->in_len += (size_t)r;
            continue;
        }
        if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
            return on_input(s, c, problem, size);
        if (r == 0) { /* what came before the end still counts */
            const char *verdict = on_input(s, c, problem, size);
            return verdict ? verdict : "connection lost";
        }
        return "connection lost";
    }
}

static bool write_conn(conn *c)
{
    while (c->out_off < c->out_len) {
        ssize_t w = send(c->fd, c->out + c->out_off, c->out_len - c->out_off, MSG_NOSIGNAL | MSG_DONTWAIT);
        if (w > 0)
            c->out_off += (size_t)w;
        else
            return w < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR);
    }
    c->out_len = c->out_off = 0;
    return true;
}

/* a child of this exchange ended: the next step, or the problem */
static const char *on_child(server *s, conn *c, int status)
{
    (void)s;
    c->child_running = false;
    if (status != 0)
        return c->stage == ENABLING ? "systemctl enable failed" : "the agent could not be started";
    if (c->stage == ENABLING) {
        c->stage = STARTING;
        return start_child(c, c->handover.changed ? "restart" : "start") ? NULL : "systemctl could not be started";
    }
    c->stage = HANDING_OVER;
    request(c, "transact", cJSON_Duplicate(c->handover.update, 1), 2);
    return NULL;
}

static void accept_pods(server *s)
{
    while (s->nconns < s->concurrency) {
        struct sockaddr_storage sa;
        socklen_t len = sizeof(sa);
        int fd = accept(s->listener, (struct sockaddr *)&sa, &len);
        if (fd < 0) {
            if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)
                WARN("accept: %s", strerror(errno));
            return;
        }
        int flags = fcntl(fd, F_GETFL);
        if (flags == -1 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) == -1 || fcntl(fd, F_SETFD, FD_CLOEXEC) == -1) {
            WARN("accept: %s", strerror(errno));
            close(fd);
            continue;
        }
        conn *c = &s->conns[s->nconns++];
        memset(c, 0, sizeof(*c));
        c->fd = fd;
        c->deadline = monotonic() + TIMEOUT;
        c->stage = SELECTING;
        if (sa.ss_family == AF_INET) {
            const struct sockaddr_in *in = (const struct sockaddr_in *)&sa;
            char host[INET_ADDRSTRLEN] = "";
            (void)inet_ntop(AF_INET, &in->sin_addr, host, sizeof(host));
            EM_FORMAT_FIXED(c->peer, sizeof(c->peer), "tcp:%s:%u", host, (unsigned)ntohs(in->sin_port));
        } else {
            EM_FORMAT_FIXED(c->peer, sizeof(c->peer), "unix");
        }
        request(c, "transact", em_fleet_select(&s->fleet), 1);
    }
}

static void reap_orphans(server *s)
{
    for (size_t i = 0; i < s->norphans;) {
        int status;
        if (em_reaped(s->orphans[i], &status))
            s->orphans[i] = s->orphans[--s->norphans];
        else
            i++;
    }
}

static int serve(server *s)
{
    const char *listen_name = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(s->fleet.config, "listen"));
    s->listener = listen_on(listen_name, s->unix_path, sizeof(s->unix_path));
    if (s->listener < 0) {
        FAIL("cannot listen on %s", listen_name);
        return 1;
    }
    const cJSON *concurrency = cJSON_GetObjectItemCaseSensitive(s->fleet.config, "concurrency");
    s->concurrency = cJSON_IsNumber(concurrency) ? (size_t)concurrency->valueint : 16;
    s->conns = em_calloc(s->concurrency, sizeof(*s->conns));
    char *ports = cJSON_PrintUnformatted(cJSON_GetObjectItemCaseSensitive(s->fleet.config, "ports"));
    LOG("EMOSA C fleet %s (%s): %s, agents on ports %s", EMOSA_VERSION, EMOSA_REVISION, listen_name, ports);
    free(ports);
    /* the agents the registry already has, before the first pod (an upgraded image) */
    size_t unstarted = em_fleet_start_registered(&s->fleet, start_agent, NULL);
    if (unstarted == SIZE_MAX)
        WARN("the fleet registry is unreadable: no agent started");
    else if (unstarted)
        WARN("%zu agents not started: their configurations not written", unstarted);
    struct pollfd *p = em_calloc(s->concurrency + 1, sizeof(*p));
    while (!stopping) {
        nfds_t n = 0;
        bool listening = s->nconns < s->concurrency; /* full: the others wait in the backlog */
        if (listening)
            p[n++] = (struct pollfd){s->listener, POLLIN, 0};
        double now = monotonic(), wake = now + 0.2;
        for (size_t i = 0; i < s->nconns; i++) {
            conn *c = &s->conns[i];
            p[n++] = (struct pollfd){c->fd, (short)(POLLIN | (c->out_len > c->out_off ? POLLOUT : 0)), 0};
            if (c->deadline < wake)
                wake = c->deadline;
            if (c->child_running && now + 0.05 < wake)
                wake = now + 0.05; /* systemctl: reaped promptly */
        }
        int timeout = wake > now ? (int)((wake - now) * 1000) + 1 : 0;
        if (poll(p, n, timeout) < 0 && errno != EINTR) {
            WARN("poll: %s", strerror(errno));
            break;
        }
        size_t first = listening ? 1 : 0, handled = s->nconns;
        /* the connections polled, newest first, as finish() moves the last into a gap */
        for (size_t k = handled; k-- > 0;) {
            conn *c = &s->conns[k];
            short revents = p[first + k].revents;
            char problem[256];
            const char *verdict = NULL;
            int status;
            if (revents & (POLLIN | POLLHUP | POLLERR))
                verdict = read_conn(s, c, problem, sizeof(problem));
            if (!verdict && c->child_running && em_reaped(c->child, &status))
                verdict = on_child(s, c, status);
            if (!verdict && !write_conn(c))
                verdict = "connection lost";
            if (!verdict && monotonic() > c->deadline)
                verdict = c->stage == HANDING_OVER || c->stage == SELECTING ? "no reply" : "the agent's start took too long";
            if (verdict)
                finish(s, k, *verdict ? verdict : NULL);
        }
        reap_orphans(s);
        if (listening && (p[0].revents & POLLIN))
            accept_pods(s);
    }
    while (s->nconns)
        finish(s, s->nconns - 1, NULL);
    free(p);
    free(s->conns);
    free(s->orphans);
    close(s->listener);
    if (s->unix_path[0])
        (void)unlink(s->unix_path);
    return 0;
}

/* -- main ----------------------------------------------------------------------------- */

static int usage(void)
{
    (void)fprintf(stderr, "usage: emosa-fleet-c serve|list|forget CONFIG [SERIAL] | --version\n");
    return 2;
}

static cJSON *load_config(const char *path)
{
    char *text = em_read_file(path, CONFIG_MAX, NULL);
    cJSON *config = text ? cJSON_Parse(text) : NULL;
    free(text);
    if (!config) {
        FAIL("%s: not a JSON fleet configuration", path);
        return NULL;
    }
    em_schema *schema = em_schema_load(em_schema_directory(), "fleet-config");
    char where[256] = "";
    bool valid = schema && em_schema_valid(schema, config, where, sizeof(where));
    em_schema_free(schema);
    if (!valid) {
        FAIL("%s: invalid fleet configuration (%s)", path, schema ? where : "no schema");
        cJSON_Delete(config);
        return NULL;
    }
    return config;
}

static void print_json(const cJSON *value)
{
    char *text = em_json_dumps(value, EM_JSON_INDENT2, true);
    if (text)
        (void)printf("%s\n", text);
    free(text);
}

int main(int argc, char **argv)
{
    em_init();
    if (argc == 2 && !strcmp(argv[1], "--version")) {
        (void)printf("emosa-fleet-c %s (%s)\n", EMOSA_VERSION, EMOSA_REVISION);
        return 0;
    }
    if (argc < 3 || argc > 4)
        return usage();
    const char *command = argv[1];
    bool serve_cmd = !strcmp(command, "serve"), list_cmd = !strcmp(command, "list"),
         forget_cmd = !strcmp(command, "forget");
    if (!serve_cmd && !list_cmd && !forget_cmd)
        return usage();
    if (serve_cmd) /* the service's log; list and forget answer on the terminal */
        em_log_open("fleet", NULL);
    if (forget_cmd && argc != 4) {
        (void)fprintf(stderr, "forget needs a SERIAL\n");
        return 2;
    }
    cJSON *config = load_config(argv[2]);
    if (!config)
        return 1;
    static server s;
    char why[600] = "";
    const char *state_root = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(config, "state_root"));
    const char *config_dir = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(config, "config_dir"));
    if (!em_mkdirs(state_root, 0700) || !em_mkdirs(config_dir, 0755)) {
        FAIL("%s or %s cannot be created", state_root, config_dir);
        cJSON_Delete(config);
        return 1;
    }
    if (!em_fleet_open(&s.fleet, config, why, sizeof(why))) {
        FAIL("%s", why);
        em_fleet_close(&s.fleet);
        return 1;
    }
    int rc = 0;
    if (list_cmd) {
        char *text = em_registry_dump(&s.fleet.registry);
        if (text)
            (void)fputs(text, stdout);
        free(text);
    } else if (forget_cmd) {
        char stamp[32];
        time_t t = time(NULL);
        struct tm local;
        if (!localtime_r(&t, &local) || !strftime(stamp, sizeof(stamp), "%Y%m%dT%H%M%S", &local))
            EM_FORMAT_FIXED(stamp, sizeof(stamp), "%lld", (long long)t);
        cJSON *entry = em_fleet_forget(&s.fleet, argv[3], stamp, stop_agent, NULL);
        if (entry)
            print_json(entry);
        else
            (void)printf("null\n");
        cJSON_Delete(entry);
    } else if (signal(SIGTERM, on_signal) == SIG_ERR || signal(SIGINT, on_signal) == SIG_ERR ||
               signal(SIGPIPE, SIG_IGN) == SIG_ERR) {
        FAIL("signal handlers not installed");
        rc = 1;
    } else {
        rc = serve(&s);
    }
    em_fleet_close(&s.fleet);
    return rc;
}
