/* SPDX-License-Identifier: Apache-2.0 */
/* The fleet's agents as its own children (supervise.h). One owner thread, the fleet's: the
 * children are started with fork and execve (no shell, no PATH search: CERT ENV33-C) and
 * reaped without blocking from its poll loop. */
#include "supervise.h"

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "log.h"
#include "proc.h"

extern char **environ;

#define LOG(...) em_log(EM_LOG_INFO, "emosa.fleet", __VA_ARGS__)
#define WARN(...) em_log(EM_LOG_WARNING, "emosa.fleet", __VA_ARGS__)

#define PATH_SIZE 512
#define ENV_FILE_MAX (64 * 1024)
#define ENV_KEY_MAX 128
#define ENV_VALUE_MAX 4096

typedef enum { IDLE, LINKING, RUNNING, STOPPING, WAITING } phase;

typedef struct {
    char pod[65];
    phase phase;
    pid_t pid;      /* LINKING: the link helper; RUNNING and STOPPING: the agent */
    bool again;     /* a restart: started again as soon as the current child has ended */
    bool gone;      /* its configuration removed: not started again */
    bool killed;    /* STOPPING: SIGKILL sent */
    double at;      /* WAITING: the next start; STOPPING: SIGKILL */
    double checked; /* the configuration's and the log's last look */
    unsigned asked, served, failed; /* tickets: the last asked for, started, failed */
    int log;        /* agent.log, -1 when it could not be opened (the fleet's stderr then) */
} agent;

struct em_supervisor {
    char agent_program[PATH_SIZE], link_program[PATH_SIZE], config_dir[PATH_SIZE], log_root[PATH_SIZE],
        defaults[PATH_SIZE];
    double restart_delay, stop_timeout;
    long log_max;
    unsigned tickets;
    agent *agents;
    size_t count;
};

static double monotonic(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static void pause_briefly(void)
{
    struct timespec ts = {0, 20 * 1000 * 1000};
    (void)nanosleep(&ts, NULL);
}

/* a pod's name as the fleet makes them (em_serial_valid): also a file name */
static bool pod_valid(const char *pod)
{
    size_t n = pod ? strlen(pod) : 0;
    if (!n || n > 64 || !isalnum((unsigned char)pod[0]))
        return false;
    for (size_t i = 0; i < n; i++)
        if (!isalnum((unsigned char)pod[i]) && pod[i] != '.' && pod[i] != '_' && pod[i] != '-')
            return false;
    return true;
}

/* -- environment files ----------------------------------------------------------------- */

bool em_sup_env_line(const char *line, char *key, size_t key_size, char *value, size_t value_size)
{
    const char *p = line;
    while (*p == ' ' || *p == '\t')
        p++;
    if (!strncmp(p, "export", 6) && (p[6] == ' ' || p[6] == '\t')) {
        p += 6;
        while (*p == ' ' || *p == '\t')
            p++;
    }
    const char *k = p;
    if (!isalpha((unsigned char)*p) && *p != '_')
        return false;
    while (isalnum((unsigned char)*p) || *p == '_')
        p++;
    size_t klen = (size_t)(p - k);
    if (*p != '=' || klen >= key_size)
        return false;
    memcpy(key, k, klen);
    key[klen] = 0;
    p++;
    size_t n = strlen(p);
    while (n && (p[n - 1] == '\n' || p[n - 1] == '\r' || p[n - 1] == ' ' || p[n - 1] == '\t'))
        n--;
    if (n >= 2 && (p[0] == '"' || p[0] == '\'') && p[n - 1] == p[0]) {
        p++;
        n -= 2;
    }
    if (n >= value_size)
        return false;
    memcpy(value, p, n);
    value[n] = 0;
    return true;
}

typedef struct {
    char **v;
    size_t n, cap;
} env_vec;

/* entry ("KEY=VALUE", taken) replaces the entry of the same KEY, or is added */
static void env_put(env_vec *e, char *entry)
{
    size_t klen = strcspn(entry, "=") + 1;
    for (size_t i = 0; i < e->n; i++)
        if (!strncmp(e->v[i], entry, klen)) {
            free(e->v[i]);
            e->v[i] = entry;
            return;
        }
    if (e->n + 1 >= e->cap) {
        e->cap = e->cap ? e->cap * 2 : 64;
        e->v = em_realloc(e->v, e->cap * sizeof(*e->v));
    }
    e->v[e->n++] = entry;
}

/* each KEY=VALUE line of text, in order, to f(ctx, key, value) */
static void env_lines(char *text, void (*f)(void *ctx, const char *key, const char *value), void *ctx)
{
    char *save = NULL;
    for (char *line = strtok_r(text, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
        char key[ENV_KEY_MAX], value[ENV_VALUE_MAX];
        if (em_sup_env_line(line, key, sizeof(key), value, sizeof(value)))
            f(ctx, key, value);
    }
}

static void env_add(void *ctx, const char *key, const char *value)
{
    size_t size = strlen(key) + strlen(value) + 2;
    char *entry = em_calloc(1, size);
    (void)em_format(entry, size, "%s=%s", key, value);
    env_put(ctx, entry);
}

char **em_sup_environment(char *const *base, const char *const *files, size_t nfiles,
                          const char *const *extra)
{
    env_vec e = {0};
    for (size_t i = 0; base && base[i]; i++)
        if (strchr(base[i], '='))
            env_put(&e, em_strdup(base[i]));
    for (size_t f = 0; f < nfiles; f++) {
        char *text = em_read_file(files[f], ENV_FILE_MAX, NULL);
        if (text)
            env_lines(text, env_add, &e);
        free(text);
    }
    for (size_t i = 0; extra && extra[i]; i++)
        if (strchr(extra[i], '='))
            env_put(&e, em_strdup(extra[i]));
    if (e.n + 1 > e.cap)
        e.v = em_realloc(e.v, (e.n + 1) * sizeof(*e.v));
    e.v[e.n] = NULL;
    return e.v;
}

void em_sup_env_free(char **env)
{
    for (size_t i = 0; env && env[i]; i++)
        free(env[i]);
    free(env);
}

typedef struct {
    const char *key;
    char *value;
    size_t size;
    bool found;
} lookup;

static void env_find(void *ctx, const char *key, const char *value)
{
    lookup *l = ctx;
    if (!strcmp(key, l->key) && em_copy(l->value, l->size, value))
        l->found = true; /* the last one wins, as in the environment */
}

bool em_sup_file_value(const char *path, const char *key, char *value, size_t size)
{
    char *text = em_read_file(path, ENV_FILE_MAX, NULL);
    lookup l = {key, value, size, false};
    if (text)
        env_lines(text, env_find, &l);
    free(text);
    return l.found;
}

static const char *env_value(char *const *env, const char *key)
{
    size_t n = strlen(key);
    for (size_t i = 0; env && env[i]; i++)
        if (!strncmp(env[i], key, n) && env[i][n] == '=')
            return env[i] + n + 1;
    return NULL;
}

/* -- one pod's agent ------------------------------------------------------------------- */

static bool pod_file(const em_supervisor *s, const agent *a, const char *name, char *out, size_t size)
{
    return em_format(out, size, "%s/%s/%s", s->log_root, a->pod, name);
}

static bool config_path(const em_supervisor *s, const agent *a, char *out, size_t size)
{
    return em_format(out, size, "%s/%s.json", s->config_dir, a->pod);
}

static void open_log(const em_supervisor *s, agent *a)
{
    char dir[PATH_SIZE], path[PATH_SIZE];
    if (a->log >= 0)
        return;
    if (!em_format(dir, sizeof(dir), "%s/%s", s->log_root, a->pod) || !em_mkdirs(dir, 0755) ||
        !pod_file(s, a, "agent.log", path, sizeof(path)) ||
        (a->log = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0640)) < 0)
        WARN("pod %s: its agent's log not opened under %s: its output on the fleet's", a->pod, s->log_root);
}

static bool write_all(int fd, const char *data, size_t n)
{
    while (n) {
        ssize_t w = write(fd, data, n);
        if (w < 0 && errno == EINTR)
            continue;
        if (w <= 0)
            return false;
        data += w;
        n -= (size_t)w;
    }
    return true;
}

/* agent.log past log_max: copied to agent.log.1 and truncated. Its writers append
 * (O_APPEND), so they go on from its new start; what they write during the copy is lost. */
static void bound_log(const em_supervisor *s, const agent *a)
{
    struct stat st;
    char path[PATH_SIZE], old[PATH_SIZE], tmp[PATH_SIZE];
    if (a->log < 0 || fstat(a->log, &st) || st.st_size <= (off_t)s->log_max)
        return;
    if (!pod_file(s, a, "agent.log", path, sizeof(path)) || !pod_file(s, a, "agent.log.1", old, sizeof(old)) ||
        !pod_file(s, a, "agent.log.1.tmp", tmp, sizeof(tmp)))
        return;
    int in = open(path, O_RDONLY | O_CLOEXEC);
    int out = in >= 0 ? open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0640) : -1;
    bool ok = in >= 0 && out >= 0;
    char chunk[8192];
    while (ok) {
        ssize_t r = read(in, chunk, sizeof(chunk));
        if (r < 0 && errno == EINTR)
            continue;
        if (r <= 0) {
            ok = r == 0;
            break;
        }
        ok = write_all(out, chunk, (size_t)r);
    }
    if (in >= 0)
        close(in);
    if (out >= 0 && close(out))
        ok = false;
    if (!ok || rename(tmp, old)) {
        (void)unlink(tmp);
        WARN("pod %s: %s not kept as agent.log.1", a->pod, path);
    }
    if (ftruncate(a->log, 0))
        WARN("pod %s: %s not truncated: %s", a->pod, path, strerror(errno));
}

/* program ARG in a child with env and the agent's log as its output; -1 when not started.
 * The child dies with the fleet (PR_SET_PDEATHSIG), in its own session, with the signal
 * dispositions a service manager would give it. */
static pid_t launch(const agent *a, const char *program, const char *arg, char *const *env)
{
    if (program[0] != '/') {
        WARN("pod %s: %s is not an absolute path", a->pod, program);
        return -1;
    }
    const char *args[] = {program, arg, NULL};
    union {
        const char *const *in;
        char *const *out;
    } cast = {.in = args}; /* execve's argv is char *const *: the strings are not written */
    pid_t parent = getpid();
    pid_t pid = fork();
    if (pid != 0)
        return pid;
    /* the child, until execve: only what is safe after a fork */
    (void)prctl(PR_SET_PDEATHSIG, SIGTERM);
    if (getppid() != parent)
        _exit(127); /* the fleet ended before the line above */
    sigset_t none;
    (void)sigemptyset(&none);
    (void)sigprocmask(SIG_SETMASK, &none, NULL);
    (void)signal(SIGPIPE, SIG_DFL); /* ignored by the fleet, and kept ignored across execve */
    (void)setsid();
    int null = open("/dev/null", O_RDONLY);
    if (null < 0 || dup2(null, 0) < 0)
        _exit(127);
    if (a->log >= 0 && (dup2(a->log, 1) < 0 || dup2(a->log, 2) < 0))
        _exit(127);
    execve(program, cast.out, env);
    _exit(127);
}

static char **pod_environment(const em_supervisor *s, const agent *a)
{
    char common[PATH_SIZE], own[PATH_SIZE], dir[PATH_SIZE + 32];
    const char *files[2];
    size_t nfiles = 0;
    if (em_format(common, sizeof(common), "%s/emosa", s->defaults))
        files[nfiles++] = common;
    if (em_format(own, sizeof(own), "%s/emosa-%s", s->defaults, a->pod))
        files[nfiles++] = own;
    /* the helper reads the configuration the agent is given */
    const char *extra[] = {dir, NULL};
    if (!em_format(dir, sizeof(dir), "EMOSA_AGENT_CONFIG_DIR=%s", s->config_dir))
        extra[0] = NULL;
    return em_sup_environment(environ, files, nfiles, extra);
}

static void retry_later(const em_supervisor *s, agent *a, double now)
{
    a->failed = a->asked;
    a->pid = 0;
    a->phase = WAITING;
    a->at = now + s->restart_delay;
}

/* the first step of a start: the link helper (emosa-agent-link POD) */
static void start_link(em_supervisor *s, agent *a, double now)
{
    char config[PATH_SIZE];
    a->again = false;
    if (!config_path(s, a, config, sizeof(config)) || access(config, F_OK)) {
        WARN("pod %s: its agent not started: no %s", a->pod, config);
        a->gone = true;
        a->failed = a->asked;
        a->phase = IDLE;
        return;
    }
    open_log(s, a);
    char **env = pod_environment(s, a);
    const char *link = env_value(env, "EMOSA_AGENT_LINK");
    a->pid = launch(a, link && *link ? link : s->link_program, a->pod, env);
    em_sup_env_free(env);
    if (a->pid <= 0) {
        WARN("pod %s: its link helper not started, again in %g s", a->pod, s->restart_delay);
        retry_later(s, a, now);
        return;
    }
    a->phase = LINKING;
}

/* the second: the agent (emosa-agent-c CONFIG_DIR/POD.json), recorded in agent.pid */
static void start_agent(em_supervisor *s, agent *a, double now)
{
    char config[PATH_SIZE], pidfile[PATH_SIZE], text[32];
    if (!config_path(s, a, config, sizeof(config))) {
        retry_later(s, a, now);
        return;
    }
    char **env = pod_environment(s, a);
    const char *program = env_value(env, "EMOSA_AGENT");
    a->pid = launch(a, program && *program ? program : s->agent_program, config, env);
    em_sup_env_free(env);
    if (a->pid <= 0) {
        WARN("pod %s: its agent not started, again in %g s", a->pod, s->restart_delay);
        retry_later(s, a, now);
        return;
    }
    a->phase = RUNNING;
    a->served = a->asked;
    EM_FORMAT_FIXED(text, sizeof(text), "%ld\n", (long)a->pid);
    if (!pod_file(s, a, "agent.pid", pidfile, sizeof(pidfile)) || !em_write_file(pidfile, text, false))
        WARN("pod %s: its agent's pid not recorded under %s", a->pod, s->log_root);
    LOG("pod %s: its agent started (pid %ld)", a->pod, (long)a->pid);
}

static void terminate(const em_supervisor *s, agent *a, double now)
{
    (void)kill(a->pid, SIGTERM);
    a->phase = STOPPING;
    a->killed = false;
    a->at = now + s->stop_timeout;
}

static void forget_pid(const em_supervisor *s, const agent *a)
{
    char pidfile[PATH_SIZE];
    if (pod_file(s, a, "agent.pid", pidfile, sizeof(pidfile)))
        (void)unlink(pidfile);
}

/* the agent ended: started again (at once for a restart, after restart_delay otherwise),
 * unless its configuration is gone */
static void ended(em_supervisor *s, agent *a, int status, double now)
{
    bool asked = a->phase == STOPPING;
    forget_pid(s, a);
    a->pid = 0;
    a->killed = false;
    if (asked)
        LOG("pod %s: its agent stopped (status %d)", a->pod, status);
    else
        WARN("pod %s: its agent ended (status %d), again in %g s", a->pod, status, s->restart_delay);
    if (a->gone)
        a->phase = IDLE;
    else if (a->again)
        start_link(s, a, now);
    else {
        a->phase = WAITING;
        a->at = now + s->restart_delay;
    }
}

/* -- the supervisor -------------------------------------------------------------------- */

em_supervisor *em_sup_new(const em_sup_config *c)
{
    em_supervisor *s = em_calloc(1, sizeof(*s));
    if (!em_copy(s->agent_program, sizeof(s->agent_program), c->agent) ||
        !em_copy(s->link_program, sizeof(s->link_program), c->link) ||
        !em_copy(s->config_dir, sizeof(s->config_dir), c->config_dir) ||
        !em_copy(s->log_root, sizeof(s->log_root), c->log_root) ||
        !em_copy(s->defaults, sizeof(s->defaults), c->defaults)) {
        free(s);
        return NULL;
    }
    s->restart_delay = c->restart_delay;
    s->stop_timeout = c->stop_timeout;
    s->log_max = c->log_max;
    return s;
}

static agent *find(const em_supervisor *s, const char *pod)
{
    for (size_t i = 0; i < s->count; i++)
        if (!strcmp(s->agents[i].pod, pod))
            return &s->agents[i];
    return NULL;
}

unsigned em_sup_start(em_supervisor *s, const char *pod, bool restart, double now)
{
    unsigned ticket = ++s->tickets;
    if (!pod_valid(pod)) {
        WARN("not a pod's name: %s", pod ? pod : "(none)");
        return ticket; /* unknown to em_sup_started: failed */
    }
    agent *a = find(s, pod);
    if (!a) {
        s->agents = em_realloc(s->agents, (s->count + 1) * sizeof(*s->agents));
        a = &s->agents[s->count++];
        memset(a, 0, sizeof(*a));
        (void)em_copy(a->pod, sizeof(a->pod), pod);
        a->log = -1;
        a->phase = IDLE;
    }
    if (!restart && a->phase == RUNNING) {
        s->tickets--; /* nothing asked */
        return 0;
    }
    a->asked = ticket;
    a->gone = false;
    switch (a->phase) {
    case IDLE:
    case WAITING:
        start_link(s, a, now);
        break;
    case LINKING: /* the start under way serves a start; a restart links again after it */
        a->again = a->again || restart;
        break;
    case RUNNING:
        terminate(s, a, now);
        a->again = true;
        break;
    case STOPPING:
        a->again = true;
        break;
    }
    return ticket;
}

int em_sup_started(const em_supervisor *s, const char *pod, unsigned ticket)
{
    const agent *a = find(s, pod);
    if (!a)
        return -1;
    if (a->served >= ticket)
        return 1;
    return a->failed >= ticket ? -1 : 0;
}

pid_t em_sup_pid(const em_supervisor *s, const char *pod)
{
    const agent *a = find(s, pod);
    return a && (a->phase == RUNNING || a->phase == STOPPING) ? a->pid : 0;
}

void em_sup_step(em_supervisor *s, double now)
{
    for (size_t i = 0; i < s->count; i++) {
        agent *a = &s->agents[i];
        int status;
        if (a->phase != IDLE && now - a->checked >= 1.0) {
            char config[PATH_SIZE];
            a->checked = now;
            bound_log(s, a);
            if (!a->gone && (!config_path(s, a, config, sizeof(config)) || access(config, F_OK))) {
                LOG("pod %s: its configuration removed: its agent stopped", a->pod);
                a->gone = true;
                a->again = false;
                a->failed = a->asked;
                if (a->phase == RUNNING)
                    terminate(s, a, now);
                else if (a->phase == WAITING)
                    a->phase = IDLE;
            }
        }
        switch (a->phase) {
        case LINKING:
            if (!em_reaped(a->pid, &status))
                break;
            a->pid = 0;
            if (status != 0) {
                WARN("pod %s: its link helper failed (status %d), again in %g s", a->pod, status,
                     s->restart_delay);
                retry_later(s, a, now);
            } else if (a->gone) {
                a->phase = IDLE;
            } else if (a->again) {
                start_link(s, a, now);
            } else {
                start_agent(s, a, now);
            }
            break;
        case RUNNING:
        case STOPPING:
            if (em_reaped(a->pid, &status)) {
                ended(s, a, status, now);
            } else if (a->phase == STOPPING && !a->killed && now >= a->at) {
                WARN("pod %s: its agent killed, %.0f s after SIGTERM", a->pod, s->stop_timeout);
                (void)kill(a->pid, SIGKILL);
                a->killed = true;
            }
            break;
        case WAITING:
            if (now >= a->at) {
                if (a->gone)
                    a->phase = IDLE;
                else
                    start_link(s, a, now);
            }
            break;
        case IDLE:
            break;
        }
    }
}

void em_sup_free(em_supervisor *s)
{
    if (!s)
        return;
    size_t running = 0;
    for (size_t i = 0; i < s->count; i++)
        if (s->agents[i].pid > 0) {
            (void)kill(s->agents[i].pid, SIGTERM);
            running++;
        }
    double deadline = monotonic() + s->stop_timeout;
    bool killed = false;
    while (running) {
        running = 0;
        for (size_t i = 0; i < s->count; i++) {
            agent *a = &s->agents[i];
            int status;
            if (a->pid <= 0)
                continue;
            if (em_reaped(a->pid, &status)) {
                if (a->phase == RUNNING || a->phase == STOPPING)
                    forget_pid(s, a);
                a->pid = 0;
            } else {
                running++;
            }
        }
        if (running && !killed && monotonic() >= deadline) {
            for (size_t i = 0; i < s->count; i++)
                if (s->agents[i].pid > 0)
                    (void)kill(s->agents[i].pid, SIGKILL);
            killed = true;
        }
        if (running)
            pause_briefly();
    }
    for (size_t i = 0; i < s->count; i++)
        if (s->agents[i].log >= 0)
            close(s->agents[i].log);
    free(s->agents);
    free(s);
}

/* -- forget, from another process ---------------------------------------------------- */

/* pid's command line has an argument ending in /POD.json: the pod's agent, not a process
 * that has since been given its pid */
static bool agent_of(pid_t pid, const char *pod)
{
    char path[64], want[80];
    size_t len;
    EM_FORMAT_FIXED(path, sizeof(path), "/proc/%ld/cmdline", (long)pid);
    if (!em_format(want, sizeof(want), "/%s.json", pod))
        return false;
    char *cmd = em_read_file(path, 64 * 1024, &len);
    bool found = false;
    size_t w = strlen(want);
    for (size_t i = 0; cmd && i < len && !found; i += strlen(cmd + i) + 1) {
        size_t n = strlen(cmd + i);
        found = n >= w && !strcmp(cmd + i + n - w, want);
    }
    free(cmd);
    return found;
}

static bool exited(pid_t pid, double deadline)
{
    for (;;) {
        if (kill(pid, 0) && errno == ESRCH)
            return true;
        if (monotonic() >= deadline)
            return false;
        pause_briefly();
    }
}

bool em_sup_stop_recorded(const char *log_root, const char *pod, double timeout)
{
    char path[PATH_SIZE];
    if (!pod_valid(pod) || !em_format(path, sizeof(path), "%s/%s/agent.pid", log_root, pod))
        return false;
    char *text = em_read_file(path, 64, NULL);
    if (!text)
        return true; /* none recorded: none runs */
    char *end;
    long pid = strtol(text, &end, 10);
    bool usable = pid > 1 && end != text && (*end == 0 || *end == '\n');
    free(text);
    if (!usable || !agent_of((pid_t)pid, pod))
        return true;
    (void)kill((pid_t)pid, SIGTERM);
    if (exited((pid_t)pid, monotonic() + timeout))
        return true;
    (void)kill((pid_t)pid, SIGKILL);
    return exited((pid_t)pid, monotonic() + 1.0);
}
