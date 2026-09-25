/* The agent's OVSDB session over RFC 7047 JSON-RPC. Mirrors emosa.opensync.session:
 * the pod dials the agent, the agent reads the schema, monitors its tables and keeps
 * a cache; transactions are serialized. */
#define _GNU_SOURCE
#include "ovsdb.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define DATABASE "Open_vSwitch"
#define MAX_MESSAGE (16 * 1024 * 1024)

struct em_ovsdb {
    int listener, conn;
    int generation;
    unsigned long revision;
    bool monitored;
    long next_id, monitor_id, pending_id;
    cJSON *tables;         /* the cache */
    cJSON *pending_reply;  /* the reply awaited by transact */
    char **table_names;
    size_t ntables;
    char *buf;
    size_t len, cap;
};

static double now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

em_ovsdb *em_ovsdb_open(const char *endpoint, const char *const *tables, size_t ntables)
{
    int port;
    char address[64];
    if (sscanf(endpoint, "ptcp:%d:%63s", &port, address) != 2 || port < 1 || port > 65535)
        return NULL;
    struct sockaddr_in sa = {.sin_family = AF_INET, .sin_port = htons((uint16_t)port)};
    if (inet_pton(AF_INET, address, &sa.sin_addr) != 1)
        return NULL;
    int fd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0), one = 1;
    if (fd < 0)
        return NULL;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    if (bind(fd, (struct sockaddr *)&sa, sizeof(sa)) || listen(fd, 4)) {
        close(fd);
        return NULL;
    }
    em_ovsdb *s = calloc(1, sizeof(*s));
    s->listener = fd;
    s->conn = -1;
    s->tables = cJSON_CreateObject();
    s->table_names = calloc(ntables, sizeof(char *));
    for (size_t i = 0; i < ntables; i++)
        s->table_names[i] = strdup(tables[i]);
    s->ntables = ntables;
    return s;
}

static void disconnect(em_ovsdb *s)
{
    if (s->conn >= 0)
        close(s->conn);
    s->conn = -1;
    s->monitored = false;
    s->len = 0;
    cJSON_Delete(s->tables);
    s->tables = cJSON_CreateObject();
    cJSON_Delete(s->pending_reply);
    s->pending_reply = NULL;
}

void em_ovsdb_close(em_ovsdb *s)
{
    if (!s)
        return;
    disconnect(s);
    close(s->listener);
    for (size_t i = 0; i < s->ntables; i++)
        free(s->table_names[i]);
    free(s->table_names);
    cJSON_Delete(s->tables);
    free(s->buf);
    free(s);
}

size_t em_ovsdb_fds(const em_ovsdb *s, int *fds, size_t max)
{
    size_t n = 0;
    if (n < max)
        fds[n++] = s->listener;
    if (s->conn >= 0 && n < max)
        fds[n++] = s->conn;
    return n;
}

bool em_ovsdb_ready(const em_ovsdb *s) { return s->conn >= 0 && s->monitored; }
int em_ovsdb_generation(const em_ovsdb *s) { return s->generation; }
unsigned long em_ovsdb_revision(const em_ovsdb *s) { return s->revision; }
const cJSON *em_ovsdb_tables(const em_ovsdb *s) { return s->tables; }

static bool send_json(em_ovsdb *s, cJSON *message)
{
    char *text = cJSON_PrintUnformatted(message);
    size_t n = strlen(text), off = 0;
    bool ok = true;
    while (ok && off < n) {
        ssize_t w = send(s->conn, text + off, n - off, MSG_NOSIGNAL);
        if (w > 0) {
            off += (size_t)w;
        } else if (w < 0 && (errno == EAGAIN || errno == EINTR)) {
            struct pollfd p = {s->conn, POLLOUT, 0};
            ok = poll(&p, 1, 2000) > 0;
        } else {
            ok = false;
        }
    }
    free(text);
    return ok;
}

static long request(em_ovsdb *s, const char *method, cJSON *params)
{
    cJSON *m = cJSON_CreateObject();
    long id = ++s->next_id;
    cJSON_AddStringToObject(m, "method", method);
    cJSON_AddItemToObject(m, "params", params);
    cJSON_AddNumberToObject(m, "id", (double)id);
    bool ok = send_json(s, m);
    cJSON_Delete(m);
    return ok ? id : -1;
}

/* monitor updates: {table: {uuid: {old, new}}}; no "new" deletes the row */
static void apply(em_ovsdb *s, const cJSON *updates)
{
    const cJSON *table, *row;
    cJSON_ArrayForEach(table, updates)
    {
        cJSON *cached = cJSON_GetObjectItemCaseSensitive(s->tables, table->string);
        if (!cached)
            cached = cJSON_AddObjectToObject(s->tables, table->string);
        cJSON_ArrayForEach(row, table)
        {
            const cJSON *fresh = cJSON_GetObjectItemCaseSensitive(row, "new");
            if (!fresh) {
                cJSON_DeleteItemFromObjectCaseSensitive(cached, row->string);
                continue;
            }
            cJSON *target = cJSON_GetObjectItemCaseSensitive(cached, row->string);
            if (!target)
                target = cJSON_AddObjectToObject(cached, row->string);
            const cJSON *column;
            cJSON_ArrayForEach(column, fresh)
            {
                cJSON_DeleteItemFromObjectCaseSensitive(target, column->string);
                cJSON_AddItemToObject(target, column->string, cJSON_Duplicate(column, 1));
            }
        }
    }
    s->revision++;
}

static void on_message(em_ovsdb *s, cJSON *m)
{
    const cJSON *method = cJSON_GetObjectItemCaseSensitive(m, "method");
    const cJSON *id = cJSON_GetObjectItemCaseSensitive(m, "id");
    if (cJSON_IsString(method)) {
        if (!strcmp(method->valuestring, "echo") && id && !cJSON_IsNull(id)) {
            cJSON *reply = cJSON_CreateObject();
            cJSON_AddItemToObject(reply, "result",
                                  cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(m, "params"), 1));
            cJSON_AddNullToObject(reply, "error");
            cJSON_AddItemToObject(reply, "id", cJSON_Duplicate(id, 1));
            send_json(s, reply);
            cJSON_Delete(reply);
        } else if (!strcmp(method->valuestring, "update")) {
            const cJSON *params = cJSON_GetObjectItemCaseSensitive(m, "params");
            const cJSON *which = cJSON_GetArrayItem(params, 0);
            if (cJSON_IsNumber(which) && (long)which->valuedouble == s->monitor_id)
                apply(s, cJSON_GetArrayItem(params, 1));
        }
        return;
    }
    if (cJSON_IsNumber(id) && (long)id->valuedouble == s->pending_id) {
        cJSON_Delete(s->pending_reply);
        s->pending_reply = cJSON_Duplicate(m, 1);
    }
}

/* complete JSON objects at the start of the buffer, handled and removed */
static bool drain(em_ovsdb *s)
{
    size_t start = 0;
    while (start < s->len) {
        int depth = 0;
        bool string = false, escape = false;
        size_t i = start, end = 0;
        while (i < s->len && (s->buf[i] == ' ' || s->buf[i] == '\n' || s->buf[i] == '\r' || s->buf[i] == '\t'))
            i++;
        start = i;
        for (; i < s->len; i++) {
            char c = s->buf[i];
            if (string) {
                if (escape) escape = false;
                else if (c == '\\') escape = true;
                else if (c == '"') string = false;
            } else if (c == '"') {
                string = true;
            } else if (c == '{' || c == '[') {
                depth++;
            } else if (c == '}' || c == ']') {
                if (--depth == 0) {
                    end = i + 1;
                    break;
                }
            }
        }
        if (!end)
            break;
        cJSON *m = cJSON_ParseWithLength(s->buf + start, end - start);
        if (!m)
            return false;
        on_message(s, m);
        cJSON_Delete(m);
        start = end;
    }
    memmove(s->buf, s->buf + start, s->len - start);
    s->len -= start;
    return s->len <= MAX_MESSAGE;
}

static bool read_conn(em_ovsdb *s)
{
    for (;;) {
        if (s->cap - s->len < 65536) {
            size_t cap = s->cap ? s->cap * 2 : 131072;
            char *grown = realloc(s->buf, cap);
            if (!grown)
                return false;
            s->buf = grown;
            s->cap = cap;
        }
        ssize_t r = recv(s->conn, s->buf + s->len, s->cap - s->len, MSG_DONTWAIT);
        if (r > 0) {
            s->len += (size_t)r;
            continue;
        }
        if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
            return drain(s);
        return false; /* closed or failed */
    }
}

/* wait for the reply to `id`, applying everything else meanwhile */
static cJSON *await_reply(em_ovsdb *s, long id, double timeout)
{
    s->pending_id = id;
    double deadline = now() + timeout;
    while (s->conn >= 0 && !s->pending_reply) {
        double left = deadline - now();
        if (left <= 0)
            break;
        struct pollfd p = {s->conn, POLLIN, 0};
        if (poll(&p, 1, (int)(left * 1000) + 1) > 0 && !read_conn(s)) {
            disconnect(s);
            break;
        }
    }
    s->pending_id = 0;
    cJSON *reply = s->pending_reply;
    s->pending_reply = NULL;
    return reply;
}

/* a new pod connection: the monitor of every table the agent uses (all columns) */
static bool start_monitor(em_ovsdb *s)
{
    cJSON *params = cJSON_CreateArray(), *requests = cJSON_CreateObject();
    long monitor = s->generation * 1000 + 1;
    cJSON_AddItemToArray(params, cJSON_CreateString(DATABASE));
    cJSON_AddItemToArray(params, cJSON_CreateNumber((double)monitor));
    for (size_t i = 0; i < s->ntables; i++)
        cJSON_AddObjectToObject(requests, s->table_names[i]);
    cJSON_AddItemToArray(params, requests);
    long id = request(s, "monitor", params);
    if (id < 0)
        return false;
    cJSON *reply = await_reply(s, id, 5.0);
    const cJSON *result = cJSON_GetObjectItemCaseSensitive(reply, "result");
    bool ok = reply && cJSON_IsObject(result);
    if (ok) {
        s->monitor_id = monitor;
        cJSON_Delete(s->tables);
        s->tables = cJSON_CreateObject();
        apply(s, result);
        s->monitored = true;
    }
    cJSON_Delete(reply);
    return ok;
}

bool em_ovsdb_pump(em_ovsdb *s)
{
    int fd = accept4(s->listener, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
    if (fd >= 0) {
        disconnect(s); /* a new connection replaces the old one */
        s->conn = fd;
        s->generation++;
        if (!start_monitor(s)) {
            disconnect(s);
            return false;
        }
    }
    if (s->conn < 0)
        return true;
    struct pollfd p = {s->conn, POLLIN, 0};
    if (poll(&p, 1, 0) > 0 && !read_conn(s)) {
        disconnect(s);
        return false;
    }
    return true;
}

cJSON *em_ovsdb_transact(em_ovsdb *s, const cJSON *operations, double timeout)
{
    if (!em_ovsdb_ready(s))
        return NULL;
    cJSON *params = cJSON_Duplicate(operations, 1);
    cJSON_InsertItemInArray(params, 0, cJSON_CreateString(DATABASE));
    long id = request(s, "transact", params);
    if (id < 0) {
        disconnect(s);
        return NULL;
    }
    cJSON *reply = await_reply(s, id, timeout);
    cJSON *result = reply ? cJSON_DetachItemFromObjectCaseSensitive(reply, "result") : NULL;
    cJSON_Delete(reply);
    if (result && !cJSON_IsArray(result)) {
        cJSON_Delete(result);
        result = NULL;
    }
    return result;
}
