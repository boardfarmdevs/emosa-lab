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
 * are the reference fleet's (fleet.c), so either fleet takes over from the other.
 *
 * Without systemd (a router's busybox init: "agents": "supervised" in the configuration, or
 * EMOSA_AGENTS=supervised in the environment or /etc/default/emosa) the agents are the
 * fleet's own children instead of emosa-agent@ units (supervise.h, spec §4).
 *
 * With "files" the loop also serves the pods' modules' files (spec 4, the pods' modules): plain
 * HTTP on the advertise address, a few connections at a time, each answered at once from
 * em_files_answer and closed. */
#include <arpa/inet.h>
#include <cjson/cJSON.h>
#include <dirent.h>
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
#include <sys/timex.h>
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
#include "supervise.h"

#ifndef EMOSA_AGENT_PROGRAM
#define EMOSA_AGENT_PROGRAM "/usr/bin/emosa-agent-c"
#endif
#ifndef EMOSA_LINK_PROGRAM
#define EMOSA_LINK_PROGRAM "/usr/libexec/emosa/emosa-agent-link"
#endif
#define DEFAULTS_DIR "/etc/default" /* emosa and emosa-POD: the agents' environment files */
#define DEFAULTS_SIZE 400
#define AGENT_LOG_MAX 262144L /* bytes of a supervised agent's log, twice that kept */

/* where emosa and emosa-POD are: EMOSA_DEFAULTS_DIR (the lab in a box's), else /etc/default */
static const char *defaults_dir(void)
{
    const char *dir = getenv("EMOSA_DEFAULTS_DIR");
    return dir && *dir && strlen(dir) < DEFAULTS_SIZE ? dir : DEFAULTS_DIR;
}

#define TIMEOUT 5.0              /* seconds for one pod's whole exchange (spec §4) */
#define MESSAGE_MAX (1024 * 1024) /* a select of four columns is far smaller */
#define CONFIG_MAX (1024 * 1024)
#define ADOPT_MAX 128   /* agent ports listened on for re-admission, at most (the forwarder's 128) */
#define ADOPT_CHECK 1.0 /* seconds between looks at the registry's free agent ports */
#define UNIDENTIFIED_LOG 60.0 /* seconds between log lines for connections that end before identifying */
#define UNIDENTIFIED_PLACES (ADOPT_MAX + 1) /* the front port and the agent ports listened on */
#define FLEET_STATUS "fleet-status.json" /* under the run root, else the state root: the fleet's ports */

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

/* -- the agents: systemd's units, or the fleet's own children ------------------------- */

typedef struct {
    bool supervised;    /* the agents are the fleet's children (supervise.h) */
    em_supervisor *sup; /* serve, supervised: its children */
    char log_root[512]; /* supervised: each agent's log and pid file, LOG_ROOT/POD */
} agents;

/* "agents" in the configuration; else EMOSA_AGENTS from the environment, else from
 * /etc/default/emosa; else systemd. False (*why) for a value that is neither. */
static bool agents_mode(const cJSON *config, bool *supervised, char *why, size_t size)
{
    const char *mode = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(config, "agents"));
    char value[32] = "", file[DEFAULTS_SIZE + 8];
    if (!mode)
        mode = getenv("EMOSA_AGENTS");
    EM_FORMAT_FIXED(file, sizeof(file), "%s/emosa", defaults_dir()); /* the directory's length is bounded */
    if (!mode && em_sup_file_value(file, "EMOSA_AGENTS", value, sizeof(value)))
        mode = value;
    if (!mode || !*mode)
        mode = "systemd";
    *supervised = !strcmp(mode, "supervised");
    if (!*supervised && strcmp(mode, "systemd")) {
        (void)em_format(why, size, "EMOSA_AGENTS=%s: neither systemd nor supervised", mode);
        return false;
    }
    return true;
}

/* the agents' logs: the run root (a RAM disk), else the state root */
static bool agents_open(agents *a, const cJSON *config, bool serving, char *why, size_t size)
{
    memset(a, 0, sizeof(*a));
    if (!agents_mode(config, &a->supervised, why, size))
        return false;
    if (!a->supervised)
        return true;
    const char *run = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(config, "run_root"));
    const char *state = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(config, "state_root"));
    if (!em_copy(a->log_root, sizeof(a->log_root), run ? run : state)) {
        (void)em_format(why, size, "the agents' log root is too long");
        return false;
    }
    if (!serving)
        return true;
    em_sup_config c = {
        .agent = EMOSA_AGENT_PROGRAM,
        .link = EMOSA_LINK_PROGRAM,
        .config_dir = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(config, "config_dir")),
        .log_root = a->log_root,
        .defaults = defaults_dir(),
        .restart_delay = 3.0,
        .stop_timeout = 5.0,
        .log_max = AGENT_LOG_MAX,
    };
    a->sup = em_sup_new(&c);
    if (!a->sup)
        (void)em_format(why, size, "the agents' paths are too long");
    return a->sup != NULL;
}

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
    const agents *mode = ctx;
    if (mode->supervised) { /* the serving fleet's child: the pid it recorded */
        if (!em_sup_stop_recorded(mode->log_root, pod_id, 5.0))
            WARN("pod %s: its agent did not stop", pod_id);
        return;
    }
    char unit[96];
    EM_FORMAT_FIXED(unit, sizeof(unit), "emosa-agent@%s", pod_id);
    const char *argv[] = {"systemctl", "disable", "-q", "--now", unit, NULL};
    int status;
    if (!em_run(argv, NULL, NULL, &status)) /* as the reference: its outcome not checked */
        WARN("systemctl not started for %s", unit);
}

/* a secret reference provisioned into an agent's store from the fleet's, or missing from both
 * (Fleet.provision_secrets): the reference, never the value */
static void secret_note(const char *pod_id, const char *ref, em_secret_event event)
{
    switch (event) {
    case EM_SECRET_PROVISIONED:
        LOG("pod %s: secret %s provisioned from the fleet's store", pod_id, ref);
        break;
    case EM_SECRET_MISSING: /* spec 4: loudly, recorded under the state root (em_fleet_provision_secrets) */
        FAIL("pod %s: secret %s in neither its store nor the fleet's: whatever needs it (its uplink, its GTP "
             "station) cannot come up until it has one (the fleet's secrets)",
             pod_id, ref);
        break;
    case EM_SECRET_REPLACED: /* the reason its agent restarts next */
        LOG("pod %s: secret %s replaced from the fleet's store (its source changed): its agent restarts", pod_id,
            ref);
        break;
    case EM_SECRET_ADOPTED:
        LOG("pod %s: secret %s equals the fleet's: marked its copy", pod_id, ref);
        break;
    case EM_SECRET_STALE:
        WARN("pod %s: secret %s differs from the fleet's store and is its own (placed or changed by hand): kept, "
             "never replaced",
             pod_id, ref);
        break;
    case EM_SECRET_UNSOURCED: /* recorded as missing, the copy never deleted */
        FAIL("pod %s: secret %s gone from the fleet's store: the fleet's copy in its store is kept and used, but "
             "no new value can reach it until the fleet's has one",
             pod_id, ref);
        break;
    }
}

/* at the fleet's start, an agent of the registry: enabled, then started (restarted when
 * its configuration changed), each waited for; as the reference, a failure only logged */
static void start_agent(void *ctx, const char *pod_id, bool changed)
{
    const agents *mode = ctx;
    if (mode->sup) { /* not waited for: the loop starts it */
        (void)em_sup_start(mode->sup, pod_id, changed, monotonic());
        LOG("pod %s: its agent %s from the registry", pod_id,
            changed ? "restarted, its configuration changed," : "started");
        return;
    }
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

/* at the fleet's start, an entry on a reserved port (spec 4, reserved ports): loudly, its agent
 * stopped, not started (it would listen where the platform's own service does) */
static void stop_reserved(void *ctx, const char *pod_id, int port)
{
    FAIL("pod %s: its entry is on agent port %d, reserved (reserved_ports): its agent is stopped, not started; the "
         "entry moves to a free port at the pod's next arrival",
         pod_id, port);
    stop_agent(ctx, pod_id);
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
    unsigned ticket; /* supervised, STARTING: the agent's start asked for (em_sup_start) */
    int port;        /* the agent port it came on (re-admission); 0: the front port */
    bool replied;    /* its reply to the select came: what ends it now is not "unidentified" */
    em_fleet_handover handover;
    char *in, *out;
    size_t in_len, in_cap, out_len, out_off;
    em_json_scanner scan; /* where the scan of in stopped */
} conn;

/* -- the file server (spec 4, the pods' modules) ------------------------------------------ */

#define HTTP_CONNS 8
#define HTTP_REQUEST_MAX 2048 /* bytes of a request's head, more is not read */
#define HTTP_TIMEOUT 10.0     /* seconds a connection has for its request and the answer */
#define FILES_RETRY 10        /* seconds between tries to listen while the address is not there */

typedef struct {
    int fd;
    char peer[64];
    char in[HTTP_REQUEST_MAX + 1];
    size_t in_len;
    char *out; /* the answer, once the request's head is in */
    size_t out_len, out_off;
    double deadline;
} http_conn;

typedef struct {
    em_fleet fleet;
    agents agents;
    int files_fd; /* the file server's listener, -1 without */
    /* when to try listening again (monotonic; 0: never, no files) and the failed tries so far */
    double files_retry_at;
    unsigned files_failures;
    const char *files_dir;
    bool clock_known, clock_trusted; /* the host's clock, checked at clock_checked (monotonic) */
    double clock_checked;
    http_conn http[HTTP_CONNS];
    size_t nhttp;
    int listener;
    char unix_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
    conn *conns;
    size_t nconns, concurrency;
    pid_t *orphans; /* systemctl children whose pod gave up waiting: reaped later */
    size_t norphans;
    /* spec 4, re-admission: a listener on each agent port no entry holds (loopback, where the
     * agents listen), in line with the registry (adopt_sync); one is closed before an agent
     * starts on its port (adopt_release) */
    int adopt_fd[ADOPT_MAX], adopt_port[ADOPT_MAX];
    size_t nadopt;
    double adopt_check;            /* monotonic: the next look at the registry */
    int adopt_failed[ADOPT_MAX];   /* the ports it could not listen on, each logged once */
    size_t nadopt_failed;
    /* connections that ended before the pod identified itself (a platform's client on an agent
     * port, say): each counted, one logged per UNIDENTIFIED_LOG s with the rest summed by port */
    unsigned long unidentified;      /* since the fleet started */
    double unidentified_quiet_until; /* monotonic: until then they are summed, not logged */
    int summed_port[UNIDENTIFIED_PLACES]; /* 0: the front port */
    unsigned long summed[UNIDENTIFIED_PLACES];
    size_t nsummed;
    unsigned long summed_total; /* since the last line, those of ports with no place too */
    char *status_text;      /* the fleet status as last written (FLEET_STATUS) */
    char status_error[160]; /* why the last write failed, logged once */
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

static int adopt_index(const server *s, int port)
{
    for (size_t i = 0; i < s->nadopt; i++)
        if (s->adopt_port[i] == port)
            return (int)i;
    return -1;
}

static void adopt_close(server *s, size_t i)
{
    close(s->adopt_fd[i]);
    s->adopt_fd[i] = s->adopt_fd[--s->nadopt];
    s->adopt_port[i] = s->adopt_port[s->nadopt];
}

/* the listener on agent port `port` closed, before an agent starts there */
static void adopt_release(server *s, int port)
{
    int i = adopt_index(s, port);
    if (i >= 0)
        adopt_close(s, (size_t)i);
}

/* the listeners on the agent ports in line with the registry (spec 4, re-admission): closed where an
 * entry now holds the port, opened on the other free ports; one it cannot listen on (an agent of the
 * other fleet's there) tried again at the next look, its failure logged once */
static void adopt_sync(server *s)
{
    int free_ports[ADOPT_MAX];
    size_t nfree = em_fleet_free_ports(&s->fleet, free_ports, ADOPT_MAX);
    if (nfree == SIZE_MAX)
        return; /* the registry unreadable: the listeners as they were */
    for (size_t i = s->nadopt; i-- > 0;) {
        bool still = false;
        for (size_t j = 0; j < nfree && !still; j++)
            still = free_ports[j] == s->adopt_port[i];
        if (!still)
            adopt_close(s, i);
    }
    for (size_t j = 0; j < nfree; j++) {
        if (adopt_index(s, free_ports[j]) >= 0)
            continue;
        char name[32], unused[sizeof(((struct sockaddr_un *)0)->sun_path)];
        EM_FORMAT_FIXED(name, sizeof(name), "ptcp:%d:127.0.0.1", free_ports[j]);
        int fd = listen_on(name, unused, sizeof(unused));
        size_t logged = s->nadopt_failed;
        for (size_t k = 0; k < s->nadopt_failed && logged == s->nadopt_failed; k++)
            if (s->adopt_failed[k] == free_ports[j])
                logged = k;
        if (fd < 0) {
            if (logged == s->nadopt_failed && s->nadopt_failed < ADOPT_MAX) {
                WARN("agent port %d: cannot listen for re-admission: %s", free_ports[j], strerror(errno));
                s->adopt_failed[s->nadopt_failed++] = free_ports[j];
            }
            continue;
        }
        if (logged < s->nadopt_failed) /* listening again: a later failure is logged again */
            s->adopt_failed[logged] = s->adopt_failed[--s->nadopt_failed];
        s->adopt_fd[s->nadopt] = fd;
        s->adopt_port[s->nadopt++] = free_ports[j];
    }
}

static int by_number(const void *a, const void *b)
{
    int x = *(const int *)a, y = *(const int *)b;
    return (x > y) - (x < y);
}

static cJSON *sorted_ports(const int *ports, size_t n)
{
    int *order = em_calloc(n + 1, sizeof(*order));
    memcpy(order, ports, n * sizeof(*order));
    em_sort(order, n, sizeof(*order), by_number);
    cJSON *list = cJSON_CreateArray();
    for (size_t i = 0; i < n; i++)
        cJSON_AddItemToArray(list, cJSON_CreateNumber(order[i]));
    free(order);
    return list;
}

/* Fleet.status (fleet-status schema, spec 4): its agent ports, the reserved ones and where they
 * come from, the ports entries hold (and those of them reserved), the ports it listens on for
 * re-admission or could not, and the connections that ended before identifying since it started.
 * Written as FLEET_STATUS under the run root (else the state root) when it changed; the registry
 * as adopt_sync read it last. */
static void write_status(server *s)
{
    const em_fleet *f = &s->fleet;
    cJSON *st = cJSON_CreateObject();
    int range[2] = {f->registry.port_low, f->registry.port_high};
    cJSON_AddItemToObject(st, "agent_ports", cJSON_CreateIntArray(range, 2));
    cJSON *reserved = cJSON_AddArrayToObject(st, "reserved_ports");
    for (int p = 1; p < EM_PORT_BITS; p++)
        if (em_port_reserved(f->reserved_ports, p))
            cJSON_AddItemToArray(reserved, cJSON_CreateNumber(p));
    cJSON_AddStringToObject(st, "reserved_from", f->reserved_from);
    int *held = em_calloc(f->registry.count + 1, sizeof(*held));
    int *on_reserved = em_calloc(f->registry.count + 1, sizeof(*on_reserved));
    size_t nheld = 0, non_reserved = 0;
    for (size_t i = 0; i < f->registry.count; i++) {
        held[nheld++] = f->registry.entries[i].port;
        if (em_registry_reserved(&f->registry, f->registry.entries[i].port))
            on_reserved[non_reserved++] = f->registry.entries[i].port;
    }
    cJSON_AddItemToObject(st, "assigned", sorted_ports(held, nheld));
    cJSON_AddItemToObject(st, "reserved_assigned", sorted_ports(on_reserved, non_reserved));
    free(held);
    free(on_reserved);
    cJSON_AddItemToObject(st, "listening", sorted_ports(s->adopt_port, s->nadopt));
    cJSON_AddItemToObject(st, "cannot_listen", sorted_ports(s->adopt_failed, s->nadopt_failed));
    cJSON_AddNumberToObject(st, "unidentified_connections", (double)s->unidentified);
    char *body = em_json_dumps(st, EM_JSON_INDENT2, true);
    cJSON_Delete(st);
    size_t n = strlen(body) + 2;
    char *text = em_malloc(n);
    EM_FORMAT_FIXED(text, n, "%s\n", body);
    free(body);
    if (s->status_text && !strcmp(text, s->status_text)) {
        free(text);
        return;
    }
    const char *run_root = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(f->config, "run_root"));
    const char *root = run_root && *run_root ? run_root : f->state_root;
    char path[600];
    if (!em_format(path, sizeof(path), "%s/%s", root, FLEET_STATUS) || !em_mkdirs(root, 0755) ||
        !em_write_file(path, text, false)) {
        char why[sizeof(s->status_error)];
        (void)em_copy(why, sizeof(why), strerror(errno));
        if (strcmp(why, s->status_error)) { /* logged once while it fails the same way */
            WARN("fleet status not written under %s: %s", root, why);
            (void)em_copy(s->status_error, sizeof(s->status_error), why);
        }
        free(text);
        return;
    }
    s->status_error[0] = 0;
    free(s->status_text);
    s->status_text = text;
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

/* a connection that ended before the pod identified itself: counted; logged when none was in the
 * last UNIDENTIFIED_LOG s, else summed into the next summary (unidentified_tick), so a platform's
 * client redialling an agent port cannot flood the log */
static void note_unidentified(server *s, const conn *c, const char *problem)
{
    s->unidentified++;
    double now = monotonic();
    if (now >= s->unidentified_quiet_until) {
        s->unidentified_quiet_until = now + UNIDENTIFIED_LOG;
        WARN("pod connection %s: %s, before it identified itself", c->peer, problem);
        return;
    }
    s->summed_total++;
    size_t k = 0;
    while (k < s->nsummed && s->summed_port[k] != c->port)
        k++;
    if (k == s->nsummed) {
        if (k == UNIDENTIFIED_PLACES)
            return; /* in the total only */
        s->summed_port[k] = c->port;
        s->summed[k] = 0;
        s->nsummed++;
    }
    s->summed[k]++;
}

static int by_port(const void *a, const void *b)
{
    int x = *(const int *)a, y = *(const int *)b;
    if ((x == 0) != (y == 0))
        return x == 0 ? 1 : -1; /* the agent ports, then the front port */
    return (x > y) - (x < y);
}

/* the summary of the connections summed since the last line, once their quiet time is over: one
 * line per UNIDENTIFIED_LOG s while they go on */
static void unidentified_tick(server *s)
{
    double now = monotonic();
    if (!s->summed_total || now < s->unidentified_quiet_until)
        return;
    int order[UNIDENTIFIED_PLACES];
    for (size_t k = 0; k < s->nsummed; k++)
        order[k] = s->summed_port[k];
    em_sort(order, s->nsummed, sizeof(*order), by_port);
    char where[1024] = "";
    size_t n = 0;
    for (size_t j = 0; j < s->nsummed && n < sizeof(where); j++) {
        size_t k = 0;
        while (s->summed_port[k] != order[j])
            k++;
        int w = order[j] ? snprintf(where + n, sizeof(where) - n, "%sagent port %d: %lu", n ? ", " : "", order[j],
                                    s->summed[k])
                         : snprintf(where + n, sizeof(where) - n, "%sfront port: %lu", n ? ", " : "", s->summed[k]);
        if (w < 0 || (size_t)w >= sizeof(where) - n)
            break; /* cut: the total stands */
        n += (size_t)w;
    }
    WARN("%lu more pod connections ended before identifying in the last %d s (%s)", s->summed_total,
         (int)UNIDENTIFIED_LOG, where);
    s->nsummed = 0;
    s->summed_total = 0;
    s->unidentified_quiet_until = now + UNIDENTIFIED_LOG;
}

static void finish(server *s, size_t i, const char *problem)
{
    conn *c = &s->conns[i];
    if (problem && c->stage == SELECTING && !c->replied)
        note_unidentified(s, c, problem);
    else if (problem)
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

/* the agent runs: the pod's manager_addr set to it */
static void hand_over(conn *c)
{
    c->stage = HANDING_OVER;
    request(c, "transact", cJSON_Duplicate(c->handover.update, 1), 2);
}

/* the reply to our request: the next step, or the problem that ends the exchange */
static const char *on_reply(server *s, conn *c, const cJSON *result, char *problem, size_t size)
{
    if (c->stage == SELECTING) {
        em_fleet_outcome outcome = em_fleet_identify_at(&s->fleet, result, c->port, wall(), &c->handover);
        if (outcome == EM_FLEET_REFUSED) {
            if (c->handover.loud)
                FAIL("%s (%s)", c->handover.why, c->peer);
            else
                WARN("%s (%s)", c->handover.why, c->peer);
            return "";
        }
        if (outcome != EM_FLEET_HANDOVER) {
            (void)em_format(problem, size, "%s", c->handover.why);
            return problem;
        }
        if (c->handover.moved_from) /* spec 4, reserved ports: loudly */
            FAIL("pod %s: its entry was on agent port %d, reserved (reserved_ports): moved to %d (%s), its agent "
                 "restarted there",
                 c->handover.serial, c->handover.moved_from, c->handover.entry.port, c->handover.entry.interface);
        adopt_release(s, c->handover.entry.port); /* its agent listens there now */
        if (s->agents.sup) { /* the fleet's child: nothing to enable */
            c->stage = STARTING;
            c->ticket = em_sup_start(s->agents.sup, c->handover.entry.pod_id, c->handover.changed, monotonic());
            if (!c->ticket) /* it runs already */
                hand_over(c);
            return NULL;
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
    char placed[96] = "";
    if (c->handover.placed == EM_FLEET_ADOPTED)
        EM_FORMAT_FIXED(placed, sizeof(placed), "; re-admitted on the agent port it dialled (%d), no entry held it",
                        c->handover.dialled);
    else if (c->handover.placed == EM_FLEET_REGISTERED)
        EM_FORMAT_FIXED(placed, sizeof(placed), "; it dialled agent port %d, its entry's is %d", c->handover.dialled,
                        e->port);
    LOG("pod %s (node %s, %s) -> agent AL %s on %s, manager_addr %s; admitted by %s%s", c->handover.serial,
        c->handover.node, c->peer, e->al_mac, e->interface,
        cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(row, "manager_addr")), c->handover.admission, placed);
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
            c->replied = c->replied || (c->stage == SELECTING && result);
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
    hand_over(c);
    return NULL;
}

/* supervised: the agent this exchange asked for, once it runs or failed */
static const char *on_supervised(server *s, conn *c)
{
    int started = em_sup_started(s->agents.sup, c->handover.entry.pod_id, c->ticket);
    if (started < 0)
        return "the agent could not be started";
    if (started > 0) {
        c->ticket = 0;
        hand_over(c);
    }
    return NULL;
}

/* the pods waiting on `listener`: the front port, or (port) an agent port no entry holds */
static void accept_pods(server *s, int listener, int port)
{
    while (s->nconns < s->concurrency) {
        struct sockaddr_storage sa;
        socklen_t len = sizeof(sa);
        int fd = accept(listener, (struct sockaddr *)&sa, &len);
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
        c->port = port;
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

/* The file server's listener on the advertise address, its files listed in the log; -1 without
 * files, or when it cannot listen yet: the fleet serves its pods meanwhile and tries again every
 * FILES_RETRY s (s->files_retry_at), as a gateway's LAN bridge may get the address minutes after
 * the fleet starts (14 October); the first failure and the listener's start logged. */
static int files_open(server *s)
{
    const cJSON *files = cJSON_GetObjectItemCaseSensitive(s->fleet.config, "files");
    s->files_dir = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(files, "directory"));
    s->files_retry_at = 0;
    if (!s->files_dir)
        return -1;
    const cJSON *port = cJSON_GetObjectItemCaseSensitive(files, "port");
    const char *advertise = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(s->fleet.config, "advertise"));
    char name[320], unused[8];
    int number = cJSON_IsNumber(port) ? port->valueint : EM_FILES_PORT;
    int fd = advertise && em_format(name, sizeof(name), "ptcp:%d:%s", number, advertise) ? listen_on(name, unused, sizeof(unused)) : -1;
    if (fd < 0) {
        if (!s->files_failures++)
            WARN("files: cannot serve on %s:%d: %s; trying again every %d s", advertise ? advertise : "?", number,
                 strerror(errno), FILES_RETRY);
        s->files_retry_at = monotonic() + FILES_RETRY;
        return -1;
    }
    if (s->files_failures)
        LOG("files: serving on %s:%d after %u tries", advertise, number, s->files_failures + 1);
    size_t served = 0;
    DIR *dir = opendir(s->files_dir);
    for (struct dirent *e = dir ? readdir(dir) : NULL; e; e = readdir(dir)) {
        if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, ".."))
            continue;
        size_t len;
        const char *why = NULL;
        char *content = em_files_servable(s->files_dir, e->d_name, &len, &why);
        if (content)
            served++;
        else
            WARN("files: %s not served: %s", e->d_name, why);
        free(content);
    }
    if (dir)
        closedir(dir);
    LOG("files: %zu served from %s on %s:%d", served, s->files_dir, advertise, number);
    return fd;
}

/* fleet.clock_synchronized: the box's test hook EMOSA_FILES_CLOCK, else files.time_synced_command
 * (exit 0: synchronized) when set, else the kernel's clock status (adjtimex: TIME_ERROR while
 * unsynchronized, as ntpq's kerninfo shows) */
static bool clock_synchronized(server *s)
{
    const char *forced = getenv("EMOSA_FILES_CLOCK");
    if (forced && (!strcmp(forced, "synchronized") || !strcmp(forced, "unsynchronized")))
        return !strcmp(forced, "synchronized");
    const cJSON *files = cJSON_GetObjectItemCaseSensitive(s->fleet.config, "files");
    const char *command = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(files, "time_synced_command"));
    if (command) {
        const char *argv[] = {"/bin/sh", "-c", command, NULL};
        int status = -1;
        return em_run(argv, NULL, NULL, &status) && status == 0;
    }
    struct timex tx;
    memset(&tx, 0, sizeof(tx)); /* modes 0: read only */
    int state = adjtimex(&tx);
    return state >= 0 && state != TIME_ERROR;
}

/* FileServer.clock: checked at most once a minute, each change logged */
static bool clock_trusted(server *s)
{
    double now = monotonic();
    if (!s->clock_known || now - s->clock_checked >= 60) {
        bool trusted = clock_synchronized(s);
        if (!s->clock_known || trusted != s->clock_trusted)
            WARN("files: the clock is %s", trusted ? "synchronized: the time served" : "not synchronized: no time served");
        s->clock_known = true;
        s->clock_trusted = trusted;
        s->clock_checked = now;
    }
    return s->clock_trusted;
}

static void http_close(server *s, size_t i)
{
    close(s->http[i].fd);
    free(s->http[i].out);
    s->http[i] = s->http[--s->nhttp];
}

static void http_accept(server *s)
{
    while (s->nhttp < HTTP_CONNS) {
        struct sockaddr_in sa;
        socklen_t len = sizeof(sa);
        int fd = accept(s->files_fd, (struct sockaddr *)&sa, &len);
        if (fd < 0) {
            if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)
                WARN("files: accept: %s", strerror(errno));
            return;
        }
        int flags = fcntl(fd, F_GETFL);
        if (flags == -1 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) == -1 || fcntl(fd, F_SETFD, FD_CLOEXEC) == -1) {
            close(fd);
            continue;
        }
        http_conn *h = &s->http[s->nhttp++];
        memset(h, 0, sizeof(*h));
        h->fd = fd;
        h->deadline = monotonic() + HTTP_TIMEOUT;
        char host[INET_ADDRSTRLEN] = "";
        (void)inet_ntop(AF_INET, &sa.sin_addr, host, sizeof(host));
        EM_FORMAT_FIXED(h->peer, sizeof(h->peer), "%s", host); /* an IPv4 address */
    }
}

/* one step of a file server connection: false when it is done (answered, or gone) */
static bool http_step(server *s, http_conn *h, short revents)
{
    if (!h->out && (revents & (POLLIN | POLLHUP | POLLERR))) {
        ssize_t r = recv(h->fd, h->in + h->in_len, HTTP_REQUEST_MAX - h->in_len, 0);
        if (r == 0 || (r < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR))
            return false;
        if (r > 0)
            h->in_len += (size_t)r;
        h->in[h->in_len] = 0;
        if (strstr(h->in, "\r\n\r\n") || strstr(h->in, "\n\n") || h->in_len == HTTP_REQUEST_MAX) {
            int status = 0;
            char note[300];
            h->out = em_files_answer(s->files_dir, h->in, time(NULL), clock_trusted(s), &h->out_len, &status, note,
                                     sizeof(note));
            if (!h->out)
                return false;
            if (note[0] && status == 200)
                LOG("files: %s %s", h->peer, note);
            else if (note[0])
                WARN("files: %s %s", h->peer, note);
        }
    }
    while (h->out && h->out_off < h->out_len) {
        ssize_t w = send(h->fd, h->out + h->out_off, h->out_len - h->out_off, MSG_NOSIGNAL);
        if (w < 0)
            return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR;
        h->out_off += (size_t)w;
    }
    return !h->out;
}

static int serve(server *s)
{
    const char *listen_name = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(s->fleet.config, "listen"));
    s->listener = listen_on(listen_name, s->unix_path, sizeof(s->unix_path));
    if (s->listener < 0) {
        FAIL("cannot listen on %s", listen_name);
        em_sup_free(s->agents.sup); /* none started yet */
        return 1;
    }
    const cJSON *concurrency = cJSON_GetObjectItemCaseSensitive(s->fleet.config, "concurrency");
    s->concurrency = cJSON_IsNumber(concurrency) ? (size_t)concurrency->valueint : 16;
    s->conns = em_calloc(s->concurrency, sizeof(*s->conns));
    char *ports = cJSON_PrintUnformatted(cJSON_GetObjectItemCaseSensitive(s->fleet.config, "ports"));
    char reserved[512];
    if (!em_port_list(s->fleet.reserved_ports, reserved, sizeof(reserved)))
        (void)em_copy(reserved, sizeof(reserved), "(too many to name)");
    LOG("EMOSA C fleet %s (%s): %s, agents on ports %s, reserved %s (%s), %s", EMOSA_VERSION, EMOSA_REVISION,
        listen_name, ports, reserved, s->fleet.reserved_from,
        s->agents.sup ? "supervised by the fleet" : "systemd units");
    free(ports);
    /* the agents the registry already has, before the first pod (an upgraded image) */
    size_t unstarted = em_fleet_start_registered(&s->fleet, start_agent, stop_reserved, &s->agents);
    if (unstarted == SIZE_MAX)
        WARN("the fleet registry is unreadable: no agent started");
    else if (unstarted)
        WARN("%zu agents not started: their configurations not written", unstarted);
    s->files_fd = files_open(s);
    struct pollfd *p = em_calloc(s->concurrency + 2 + HTTP_CONNS + ADOPT_MAX, sizeof(*p));
    int polled_adopt_fd[ADOPT_MAX], polled_adopt_port[ADOPT_MAX];
    while (!stopping) {
        nfds_t n = 0;
        bool listening = s->nconns < s->concurrency; /* full: the others wait in the backlog */
        if (listening)
            p[n++] = (struct pollfd){s->listener, POLLIN, 0};
        double now = monotonic(), wake = now + 0.2;
        if (now >= s->adopt_check) { /* a pod handed over, or one forgotten, since the last look */
            adopt_sync(s);
            unidentified_tick(s);
            write_status(s);
            s->adopt_check = now + ADOPT_CHECK;
        }
        for (size_t i = 0; i < s->nconns; i++) {
            conn *c = &s->conns[i];
            p[n++] = (struct pollfd){c->fd, (short)(POLLIN | (c->out_len > c->out_off ? POLLOUT : 0)), 0};
            if (c->deadline < wake)
                wake = c->deadline;
            if ((c->child_running || c->ticket) && now + 0.05 < wake)
                wake = now + 0.05; /* systemctl reaped, a supervised start seen, promptly */
        }
        /* the file server after the pods: its listener while a connection is free, then each one;
         * listening once more when the address was not there */
        if (s->files_fd < 0 && s->files_retry_at > 0 && now >= s->files_retry_at)
            s->files_fd = files_open(s);
        nfds_t files_at = n;
        bool files_listening = s->files_fd >= 0 && s->nhttp < HTTP_CONNS;
        if (files_listening)
            p[n++] = (struct pollfd){s->files_fd, POLLIN, 0};
        for (size_t i = 0; i < s->nhttp; i++) {
            http_conn *h = &s->http[i];
            p[n++] = (struct pollfd){h->fd, (short)(h->out ? POLLOUT : POLLIN), 0};
            if (h->deadline < wake)
                wake = h->deadline;
        }
        /* the agent ports' listeners last (re-admission), as they are now: a reply below may close
         * one (adopt_release), so each is accepted on only while it is still the same */
        nfds_t adopt_at = n;
        size_t nadopt_polled = listening ? s->nadopt : 0;
        for (size_t i = 0; i < nadopt_polled; i++) {
            polled_adopt_fd[i] = s->adopt_fd[i];
            polled_adopt_port[i] = s->adopt_port[i];
            p[n++] = (struct pollfd){s->adopt_fd[i], POLLIN, 0};
        }
        int timeout = wake > now ? (int)((wake - now) * 1000) + 1 : 0;
        if (poll(p, n, timeout) < 0 && errno != EINTR) {
            WARN("poll: %s", strerror(errno));
            break;
        }
        if (s->agents.sup)
            em_sup_step(s->agents.sup, monotonic());
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
            if (!verdict && c->ticket && c->stage == STARTING)
                verdict = on_supervised(s, c);
            if (!verdict && !write_conn(c))
                verdict = "connection lost";
            if (!verdict && monotonic() > c->deadline)
                verdict = c->stage == HANDING_OVER || c->stage == SELECTING ? "no reply" : "the agent's start took too long";
            if (verdict)
                finish(s, k, *verdict ? verdict : NULL);
        }
        reap_orphans(s);
        if (listening && (p[0].revents & POLLIN))
            accept_pods(s, s->listener, 0);
        for (size_t i = 0; i < nadopt_polled; i++) {
            int now_at = adopt_index(s, polled_adopt_port[i]);
            if ((p[adopt_at + i].revents & POLLIN) && now_at >= 0 && s->adopt_fd[now_at] == polled_adopt_fd[i])
                accept_pods(s, polled_adopt_fd[i], polled_adopt_port[i]);
        }
        size_t first_http = files_at + (files_listening ? 1 : 0), polled = s->nhttp;
        for (size_t k = polled; k-- > 0;) /* newest first, as http_close moves the last into a gap */
            if (!http_step(s, &s->http[k], p[first_http + k].revents) || monotonic() > s->http[k].deadline)
                http_close(s, k);
        if (files_listening && (p[files_at].revents & POLLIN))
            http_accept(s);
    }
    while (s->nhttp)
        http_close(s, s->nhttp - 1);
    while (s->nadopt)
        adopt_close(s, s->nadopt - 1);
    free(s->status_text);
    s->status_text = NULL;
    if (s->files_fd >= 0)
        close(s->files_fd);
    while (s->nconns)
        finish(s, s->nconns - 1, NULL);
    if (s->agents.sup) { /* the fleet's agents end with it; its next start starts them again */
        LOG("stopping the agents");
        em_sup_free(s->agents.sup);
        s->agents.sup = NULL;
    }
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

/* spec 4 (15 October: one pod's unknown key kept every pod off EMOSA while the fleet restarted again
 * and again): the configuration serve runs on (fleet.served_config). The file when it is valid, kept
 * then as the last valid one (state_root/fleet-config.valid.json, the rejection record gone); else
 * the one kept under the state root it names, or the platform's when it names none (not JSON, a
 * partial write), the rejection logged once and recorded (fleet-config.rejected.json: the file, its
 * error, when); NULL when there is none. */
#define VALID_CONFIG "fleet-config.valid.json"
#define REJECTED_CONFIG "fleet-config.rejected.json"

static bool config_valid(const cJSON *config, char *where, size_t size)
{
    em_schema *schema = em_schema_load(em_schema_directory(), "fleet-config");
    bool valid = schema && em_schema_valid(schema, config, where, size);
    if (!schema)
        (void)em_copy(where, size, "no schema");
    em_schema_free(schema);
    return valid;
}

#ifndef EMOSA_STATE_ROOT_DIR
#define EMOSA_STATE_ROOT_DIR "/var/lib/emosa"
#endif

/* the platform's state root (EMOSA_STATE_ROOT, as /etc/default/emosa gives it, else the build's):
 * where a file that names none (not JSON, a partial write) finds the last valid configuration */
static const char *platform_state_root(void)
{
    const char *env = getenv("EMOSA_STATE_ROOT");
    return env && *env ? env : EMOSA_STATE_ROOT_DIR;
}

static cJSON *served_config(const char *path)
{
    char *text = em_read_file(path, CONFIG_MAX, NULL);
    cJSON *config = text ? cJSON_Parse(text) : NULL;
    free(text);
    char where[256] = "", kept[1100], record[1100];
    if (config && config_valid(config, where, sizeof(where))) {
        const char *root = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(config, "state_root"));
        char *copy = em_json_dumps(config, EM_JSON_INDENT2, true);
        if (!root || !em_format(kept, sizeof(kept), "%s/%s", root, VALID_CONFIG) ||
            !em_format(record, sizeof(record), "%s/%s", root, REJECTED_CONFIG) || !em_mkdirs(root, 0700) ||
            !copy || !em_write_file(kept, copy, true))
            WARN("%s: the last valid configuration not kept", path);
        else
            (void)unlink(record);
        free(copy);
        return config;
    }
    if (!config)
        (void)em_copy(where, sizeof(where), "not JSON");
    /* the state root it names, else the platform's */
    const char *named = config ? cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(config, "state_root")) : NULL;
    const char *root = named && *named ? named : platform_state_root();
    cJSON *last = NULL;
    if (em_format(kept, sizeof(kept), "%s/%s", root, VALID_CONFIG) &&
        em_format(record, sizeof(record), "%s/%s", root, REJECTED_CONFIG)) {
        char *saved = em_read_file(kept, CONFIG_MAX, NULL), other[256] = "";
        last = saved ? cJSON_Parse(saved) : NULL;
        free(saved);
        if (last && !config_valid(last, other, sizeof(other))) {
            cJSON_Delete(last);
            last = NULL;
        }
    }
    cJSON_Delete(config);
    if (!last) {
        FAIL("%s: invalid fleet configuration (%s), and no last valid one to serve", path, where);
        return NULL;
    }
    FAIL("%s: invalid fleet configuration (%s): rejected, serving the last valid one (%s)", path, where, kept);
    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "config", path);
    cJSON_AddStringToObject(r, "error", where);
    cJSON_AddNumberToObject(r, "at", (double)time(NULL));
    char *out = em_json_dumps(r, EM_JSON_INDENT2, true);
    if (!out || !em_write_file(record, out, true))
        WARN("%s: the rejection not recorded", record);
    free(out);
    cJSON_Delete(r);
    return last;
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
    /* serve: an invalid configuration rejected, the last valid one served (spec 4) */
    cJSON *config = serve_cmd ? served_config(argv[2]) : load_config(argv[2]);
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
    if (!em_fleet_open(&s.fleet, config, why, sizeof(why)) ||
        ((serve_cmd || forget_cmd) && !agents_open(&s.agents, s.fleet.config, serve_cmd, why, sizeof(why)))) {
        FAIL("%s", why);
        em_fleet_close(&s.fleet);
        return 1;
    }
    s.fleet.secret_note = secret_note; /* Fleet.provision_secrets' log lines */
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
        cJSON *entry = em_fleet_forget(&s.fleet, argv[3], stamp, stop_agent, &s.agents);
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
