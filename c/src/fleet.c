/* SPDX-License-Identifier: Apache-2.0 */
/* The fleet (spec §4). Mirrors emosa.agent.fleet. */
#include "fleet.h"

#include "canon.h"
#include "vault.h"

#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <openssl/crypto.h>
#include <openssl/sha.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define REGISTRY_MAX (16 * 1024 * 1024)
#define CONFIG_MAX (1024 * 1024)

bool em_derive_al(const char *serial, const char *const *taken, size_t ntaken, char out[18])
{
    for (int n = 0; n < 256; n++) {
        char seed[160];
        uint8_t digest[32];
        if (!em_format(seed, sizeof(seed), "emosa-agent-al:%s:%d", serial, n))
            return false;
        SHA256((const uint8_t *)seed, strlen(seed), digest);
        EM_FORMAT_FIXED(out, 18, "02:%02x:%02x:%02x:%02x:%02x", digest[0], digest[1], digest[2], digest[3], digest[4]);
        bool used = false;
        for (size_t i = 0; i < ntaken && !used; i++)
            used = !strcmp(taken[i], out);
        if (!used)
            return true;
    }
    return false;
}

static bool serial_char(char c, bool first)
{
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
           (!first && (c == '.' || c == '_' || c == '-'));
}

bool em_serial_valid(const char *serial)
{
    size_t n = 0;
    for (; serial[n]; n++)
        if (n == 64 || !serial_char(serial[n], n == 0))
            return false;
    return n > 0;
}

/* -- the registry ------------------------------------------------------------------- */

void em_fleet_entry_clear(em_fleet_entry *e)
{
    free(e->node_id);
    free(e->model);
    free(e->firmware);
    e->node_id = e->model = e->firmware = NULL;
}

void em_registry_free(em_registry *r)
{
    for (size_t i = 0; i < r->count; i++)
        em_fleet_entry_clear(&r->entries[i]);
    free(r->entries);
    r->entries = NULL;
    r->count = r->cap = 0;
}

static em_fleet_entry *find(const em_registry *r, const char *serial)
{
    for (size_t i = 0; r->entries && i < r->count; i++)
        if (!strcmp(r->entries[i].pod_id, serial))
            return &r->entries[i];
    return NULL;
}

static em_fleet_entry *append(em_registry *r)
{
    if (r->count == r->cap) {
        r->cap = r->cap ? r->cap * 2 : 16;
        r->entries = em_realloc(r->entries, r->cap * sizeof(*r->entries));
    }
    em_fleet_entry *e = &r->entries[r->count++];
    memset(e, 0, sizeof(*e));
    return e;
}

static char *dup_or_null(const char *s) { return s ? em_strdup(s) : NULL; }

/* -- the underlays of GRE parents (spec 8.6) ----------------------------------------- */

static uint32_t prefix_mask(int len) { return len ? 0xffffffffu << (32 - len) : 0; }

/* a.b.c.d/len as its network, host bits cleared (ipaddress.ip_network(strict=False)) */
static bool parse_network(const char *text, uint32_t *net, int *len)
{
    char address[16];
    const char *slash = strchr(text, '/');
    if (!slash || (size_t)(slash - text) >= sizeof(address) || !isdigit((unsigned char)slash[1]))
        return false;
    memcpy(address, text, (size_t)(slash - text));
    address[slash - text] = 0;
    char *end = NULL;
    long n = strtol(slash + 1, &end, 10);
    struct in_addr in;
    if (*end || n > 32 || inet_pton(AF_INET, address, &in) != 1)
        return false;
    *len = (int)n;
    *net = ntohl(in.s_addr) & prefix_mask(*len);
    return true;
}

static bool overlaps(uint32_t a, int alen, uint32_t b, int blen)
{
    uint32_t mask = prefix_mask(alen < blen ? alen : blen);
    return (a & mask) == (b & mask);
}

bool em_free_underlay(const em_registry *r, const char *gtp, const em_fleet_entry *skip, char out[20])
{
    uint32_t g;
    int glen;
    if (!parse_network(gtp, &g, &glen))
        return false;
    for (int n = 1; n < 255; n++) {
        char candidate[20];
        EM_FORMAT_FIXED(candidate, sizeof(candidate), "169.254.%d.0/24", n);
        bool used = overlaps(0xa9fe0000u | (uint32_t)n << 8, 24, g, glen);
        for (size_t i = 0; i < r->count && !used; i++)
            used = &r->entries[i] != skip && !strcmp(r->entries[i].underlay, candidate);
        if (!used) {
            memcpy(out, candidate, sizeof(candidate));
            return true;
        }
    }
    return false;
}

/* the entry's underlay brought in line with the registry's GTP underlay: kept while it does
 * not overlap it, else the lowest free one ("" when none is left); whether it changed */
static bool place(em_registry *r, em_fleet_entry *e)
{
    uint32_t g, mine;
    int glen, mlen;
    if (!parse_network(r->gtp_underlay, &g, &glen))
        return false;
    if (e->underlay[0] && parse_network(e->underlay, &mine, &mlen) && !overlaps(mine, mlen, g, glen))
        return false;
    char underlay[20] = "";
    (void)em_free_underlay(r, r->gtp_underlay, e, underlay);
    bool changed = strcmp(underlay, e->underlay) != 0;
    memcpy(e->underlay, underlay, sizeof(underlay));
    return changed;
}

bool em_registry_settle(em_registry *r)
{
    bool changed = false;
    for (size_t i = 0; r->gtp_underlay[0] && i < r->count; i++)
        changed = place(r, &r->entries[i]) || changed;
    return changed;
}

static void set_identity(em_fleet_entry *e, const char *node_id, const char *model, const char *firmware)
{
    em_fleet_entry_clear(e);
    e->node_id = dup_or_null(node_id);
    e->model = dup_or_null(model);
    e->firmware = dup_or_null(firmware);
}

bool em_registry_port_free(const em_registry *r, int port)
{
    if (port < r->port_low || port > r->port_high)
        return false;
    for (size_t i = 0; i < r->count; i++)
        if (r->entries[i].port == port)
            return false;
    return true;
}

/* A new entry on `port`: its interface em<index> the port's place in the range, its AL MAC derived
 * from the serial (spec 2.2), so a pod given its port again gets them again. NULL when the serial
 * is too long or no AL MAC is left. */
static em_fleet_entry *create(em_registry *r, const char *serial, int port, double now)
{
    if (strlen(serial) > 64)
        return NULL;
    const char **taken = em_calloc(r->count + 1, sizeof(*taken));
    size_t ntaken = 0;
    taken[ntaken++] = r->reserved_al;
    for (size_t i = 0; i < r->count; i++)
        taken[ntaken++] = r->entries[i].al_mac;
    char al[18];
    bool derived = em_derive_al(serial, taken, ntaken, al);
    free(taken);
    if (!derived)
        return NULL;
    em_fleet_entry *e = append(r);
    EM_FORMAT_FIXED(e->pod_id, sizeof(e->pod_id), "%s", serial); /* at most 64, checked above */
    memcpy(e->al_mac, al, sizeof(al));
    e->port = port;
    EM_FORMAT_FIXED(e->interface, sizeof(e->interface), "em%d", port - r->port_low + 1);
    e->first_seen = now;
    return e;
}

static em_fleet_entry *seen(em_registry *r, em_fleet_entry *e, const char *node_id, const char *model,
                            const char *firmware, double now)
{
    set_identity(e, node_id, model, firmware);
    e->last_seen = now;
    e->handovers++;
    if (r->gtp_underlay[0])
        (void)place(r, e);
    return e;
}

em_fleet_entry *em_registry_assign(em_registry *r, const char *serial, const char *node_id,
                                   const char *model, const char *firmware, double now)
{
    em_fleet_entry *e = find(r, serial);
    if (!e) {
        int port = 0;
        for (int p = r->port_low; p <= r->port_high && !port; p++)
            if (em_registry_port_free(r, p))
                port = p;
        if (!port || !(e = create(r, serial, port, now)))
            return NULL;
    }
    return seen(r, e, node_id, model, firmware, now);
}

em_fleet_entry *em_registry_adopt(em_registry *r, const char *serial, const char *node_id, const char *model,
                                  const char *firmware, int port, double now, em_fleet_placed *placed)
{
    em_fleet_entry *e = find(r, serial);
    *placed = e ? EM_FLEET_REGISTERED : EM_FLEET_ADOPTED;
    if (!e && (!em_registry_port_free(r, port) || !(e = create(r, serial, port, now))))
        return NULL;
    return seen(r, e, node_id, model, firmware, now);
}

bool em_registry_forget(em_registry *r, const char *serial, em_fleet_entry *out)
{
    em_fleet_entry *e = find(r, serial);
    if (!e)
        return false;
    *out = *e;
    size_t i = (size_t)(e - r->entries);
    memmove(e, e + 1, (r->count - i - 1) * sizeof(*e));
    r->count--;
    return true;
}

static cJSON *string_or_null(const char *s) { return s ? cJSON_CreateString(s) : cJSON_CreateNull(); }

cJSON *em_fleet_entry_json(const em_fleet_entry *e)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "pod_id", e->pod_id);
    cJSON_AddStringToObject(o, "al_mac", e->al_mac);
    cJSON_AddNumberToObject(o, "port", e->port);
    cJSON_AddStringToObject(o, "interface", e->interface);
    cJSON_AddNumberToObject(o, "first_seen", e->first_seen);
    cJSON_AddNumberToObject(o, "handovers", e->handovers);
    cJSON_AddItemToObject(o, "node_id", string_or_null(e->node_id));
    cJSON_AddItemToObject(o, "model", string_or_null(e->model));
    cJSON_AddItemToObject(o, "firmware", string_or_null(e->firmware));
    cJSON_AddNumberToObject(o, "last_seen", e->last_seen);
    if (e->underlay[0])
        cJSON_AddStringToObject(o, "underlay", e->underlay);
    return o;
}

static char *with_newline(char *text)
{
    if (!text)
        return NULL;
    size_t n = strlen(text);
    char *out = em_realloc(text, n + 2);
    out[n] = '\n';
    out[n + 1] = 0;
    return out;
}

char *em_registry_dump(const em_registry *r)
{
    cJSON *o = cJSON_CreateObject();
    for (size_t i = 0; r->entries && i < r->count; i++)
        cJSON_AddItemToObject(o, r->entries[i].pod_id, em_fleet_entry_json(&r->entries[i]));
    char *text = with_newline(em_json_dumps(o, EM_JSON_INDENT2, true));
    cJSON_Delete(o);
    return text;
}

/* a string member that fits size (with its NUL), or false */
static bool member_text(const cJSON *o, const char *key, char *out, size_t size)
{
    const char *v = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, key));
    return v && em_copy(out, size, v);
}

/* a string-or-null member: false when it is neither */
static bool member_optional(const cJSON *o, const char *key, char **out)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, key);
    *out = NULL;
    if (!v || cJSON_IsNull(v))
        return true;
    if (!cJSON_IsString(v))
        return false;
    *out = em_strdup(v->valuestring);
    return true;
}

static bool member_number(const cJSON *o, const char *key, double low, double high, double *out)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, key);
    if (!cJSON_IsNumber(v) || v->valuedouble < low || v->valuedouble > high)
        return false;
    *out = v->valuedouble;
    return true;
}

bool em_registry_load(em_registry *r, const char *text)
{
    cJSON *doc = cJSON_Parse(text);
    bool ok = cJSON_IsObject(doc);
    const cJSON *item, *entries = ok ? doc : NULL;
    cJSON_ArrayForEach(item, entries)
    {
        double port = 0, handovers = 0;
        em_fleet_entry e = {0};
        ok = em_serial_valid(item->string) && cJSON_IsObject(item) &&
             member_text(item, "pod_id", e.pod_id, sizeof(e.pod_id)) && !strcmp(e.pod_id, item->string) &&
             member_text(item, "al_mac", e.al_mac, sizeof(e.al_mac)) &&
             member_text(item, "interface", e.interface, sizeof(e.interface)) &&
             member_number(item, "port", 1, 65535, &port) && port == (double)(int)port &&
             member_number(item, "handovers", 1, 4294967295.0, &handovers) &&
             handovers == (double)(unsigned)handovers &&
             member_number(item, "first_seen", -1e18, 1e18, &e.first_seen) &&
             member_number(item, "last_seen", -1e18, 1e18, &e.last_seen) && !find(r, e.pod_id);
        if (ok && !(member_optional(item, "node_id", &e.node_id) && member_optional(item, "model", &e.model) &&
                    member_optional(item, "firmware", &e.firmware)))
            ok = false;
        uint32_t net;
        int len;
        if (ok && cJSON_GetObjectItemCaseSensitive(item, "underlay") &&
            !(member_text(item, "underlay", e.underlay, sizeof(e.underlay)) && parse_network(e.underlay, &net, &len)))
            ok = false;
        if (!ok) {
            em_fleet_entry_clear(&e);
            break;
        }
        e.port = (int)port;
        e.handovers = (unsigned)handovers;
        *append(r) = e;
    }
    cJSON_Delete(doc);
    return ok;
}

/* -- the agent configuration and the handover ---------------------------------------- */

const cJSON *em_fleet_model(const cJSON *fleet, const char *model, const char *firmware, int *index)
{
    int i = 0;
    const cJSON *own;
    *index = -1;
    if (!model)
        return NULL;
    cJSON_ArrayForEach(own, cJSON_GetObjectItemCaseSensitive(fleet, "models"))
    {
        const char *name = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(own, "model"));
        const char *prefix = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(own, "firmware_prefix"));
        if (name && !strcmp(name, model) && (!prefix || (firmware && !strncmp(firmware, prefix, strlen(prefix))))) {
            *index = i;
            return own;
        }
        i++;
    }
    return NULL;
}

/* pods.<serial>[key], else the pod's model's (its models entry, spec 4), else fleet[key], else
 * a default */
static cJSON *setting(const cJSON *fleet, const char *serial, const cJSON *model, const char *key, cJSON *fallback)
{
    const cJSON *own = cJSON_GetObjectItemCaseSensitive(
        cJSON_GetObjectItemCaseSensitive(fleet, "pods"), serial);
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(own, key);
    if (!v)
        v = cJSON_GetObjectItemCaseSensitive(model, key);
    if (!v)
        v = cJSON_GetObjectItemCaseSensitive(fleet, key);
    if (v) {
        cJSON_Delete(fallback);
        return cJSON_Duplicate(v, 1);
    }
    return fallback;
}

static cJSON *mode_off(void)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "mode", "off");
    return o;
}

/* -- the pods' modules (spec 4) --------------------------------------------------------- */

static int name_order(const void *a, const void *b) { return strcmp(*(const char *const *)a, *(const char *const *)b); }

/* an object's member names in order, at most max */
static size_t member_names(const cJSON *o, const char **out, size_t max)
{
    size_t n = 0;
    const cJSON *m;
    cJSON_ArrayForEach(m, o)
    {
        if (n < max && m->string)
            out[n++] = m->string;
    }
    qsort(out, n, sizeof(*out), name_order);
    return n;
}

cJSON *em_module_settings(const cJSON *fleet, const char *serial, const cJSON *model)
{
    cJSON *modules = setting(fleet, serial, model, "modules", NULL);
    const cJSON *port = cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(fleet, "files"), "port");
    const char *advertise = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(fleet, "advertise"));
    const char *names[64], *keys[64];
    size_t n = member_names(modules, names, 64);
    cJSON *out = cJSON_CreateObject();
    for (size_t i = 0; i < n; i++) {
        const cJSON *own = cJSON_GetObjectItemCaseSensitive(modules, names[i]);
        const cJSON *config = cJSON_GetObjectItemCaseSensitive(own, "config");
        const char *update = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(own, "update"));
        size_t k = member_names(config, keys, 64);
        cJSON *values = cJSON_CreateObject();
        for (size_t j = 0; j < k; j++)
            cJSON_AddItemToObject(values, keys[j], cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(config, keys[j]), 1));
        char url[400]; /* a host name of 253 and a SHA-256 */
        if (update && advertise &&
            em_format(url, sizeof(url), "http://%s:%d/%s", advertise, cJSON_IsNumber(port) ? port->valueint : EM_FILES_PORT,
                      update)) {
            cJSON_AddStringToObject(values, "update_url", url);
            cJSON_AddStringToObject(values, "update_sha256", update);
        }
        if (cJSON_GetArraySize(values))
            cJSON_AddItemToObject(out, names[i], values);
        else
            cJSON_Delete(values);
    }
    cJSON_Delete(modules);
    if (!cJSON_GetArraySize(out)) {
        cJSON_Delete(out);
        return NULL;
    }
    return out;
}

char *em_files_servable(const char *directory, const char *name, size_t *len, const char **why)
{
    *len = 0;
    size_t n = name ? strlen(name) : 0;
    bool hex = n == 64;
    for (size_t i = 0; hex && i < n; i++)
        hex = (name[i] >= '0' && name[i] <= '9') || (name[i] >= 'a' && name[i] <= 'f');
    char path[600];
    if (!hex || !em_format(path, sizeof(path), "%s/%s", directory, name)) {
        *why = "not a SHA-256";
        return NULL;
    }
    struct stat st;
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC); /* a link is not served */
    if (fd < 0 || fstat(fd, &st) || !S_ISREG(st.st_mode)) {
        if (fd >= 0)
            close(fd);
        *why = "no such file";
        return NULL;
    }
    if (st.st_size > EM_FILES_MAX) {
        close(fd);
        *why = "too large";
        return NULL;
    }
    size_t size = (size_t)st.st_size, got = 0;
    char *content = malloc(size + 1);
    while (content && got < size) {
        ssize_t r = read(fd, content + got, size - got);
        if (r <= 0)
            break;
        got += (size_t)r;
    }
    close(fd);
    char digest[65];
    if (content && got == size)
        em_sha256_hex(content, size, digest);
    if (!content || got != size || strcmp(digest, name)) {
        *why = content && got == size ? "its content is not its name" : "unreadable";
        free(content);
        return NULL;
    }
    *len = size;
    return content;
}

char *em_files_answer(const char *directory, const char *request, time_t now, bool synchronized, size_t *len,
                      int *status, char *note, size_t note_size)
{
    /* the request line: METHOD SP PATH SP VERSION */
    char method[8] = "", path[200] = "";
    const char *sp = strchr(request, ' '), *sp2 = sp ? strchr(sp + 1, ' ') : NULL, *eol = strpbrk(request, "\r\n");
    bool parsed = sp && sp2 && (!eol || sp2 < eol) && (size_t)(sp - request) < sizeof(method) &&
                  (size_t)(sp2 - sp - 1) < sizeof(path);
    if (parsed) {
        memcpy(method, request, (size_t)(sp - request));
        method[sp - request] = 0;
        memcpy(path, sp + 1, (size_t)(sp2 - sp - 1));
        path[sp2 - sp - 1] = 0;
        char *query = strchr(path, '?');
        if (query)
            *query = 0;
    }
    bool get = !strcmp(method, "GET"), head = !strcmp(method, "HEAD");
    const char *why = NULL;
    char *content = NULL;
    size_t clen = 0;
    int code = 404;
    if (!parsed) {
        code = 400;
        why = "not a request";
    } else if (!get && !head) {
        code = 501;
        why = "not GET or HEAD";
    } else if (!strcmp(path, "/")) {
        code = synchronized ? 200 : 503; /* no time to give without a clock to trust */
        why = synchronized ? NULL : "the clock is not synchronized";
    } else if (path[0] != '/') {
        why = "not a path";
    } else if ((content = em_files_servable(directory, path + 1, &clen, &why))) {
        code = 200;
    }
    /* a Date only from a clock to trust (RFC 7231: a server without a reliable clock sends none) */
    char date[60] = "", head_text[400];
    struct tm tm;
    if (synchronized && gmtime_r(&now, &tm) && strftime(date, sizeof(date), "Date: %a, %d %b %Y %H:%M:%S GMT\r\n", &tm))
        ;
    else
        date[0] = 0;
    const char *phrase = code == 200   ? "OK"
                         : code == 404 ? "Not Found"
                         : code == 400 ? "Bad Request"
                         : code == 503 ? "Service Unavailable"
                                       : "Not Implemented";
    (void)em_format(head_text, sizeof(head_text),
                    "HTTP/1.0 %d %s\r\nServer: emosa-fleet\r\n%s%sContent-Type: application/octet-stream\r\n"
                    "Content-Length: %zu\r\nConnection: close\r\n\r\n",
                    code, phrase, date, code == 503 ? "Retry-After: 60\r\n" : "", clen); /* fits: 400 bytes */
    size_t h = strlen(head_text), body = get && content ? clen : 0;
    char *out = malloc(h + body + 1);
    if (out) {
        memcpy(out, head_text, h);
        if (body)
            memcpy(out + h, content, body);
        out[h + body] = 0; /* a string too, its body text or not (a caller's strstr stops here) */
        *len = h + body;
    }
    free(content);
    *status = code;
    if (code == 200 && !strcmp(path, "/"))
        note[0] = 0;
    else
        (void)em_format(note, note_size, "%s %s: %d%s%s", method[0] ? method : "?", path[0] ? path : "?", code,
                        why ? ", " : "", why ? why : ""); /* a log line, cut when long */
    return out;
}

cJSON *em_agent_config(const em_fleet_entry *e, const cJSON *fleet)
{
    cJSON *c = cJSON_CreateObject();
    char text[512];
    cJSON_AddStringToObject(c, "pod_id", e->pod_id);
    cJSON_AddStringToObject(c, "serial", e->pod_id);
    EM_FORMAT_FIXED(text, sizeof(text), "ptcp:%d:127.0.0.1", e->port);
    cJSON_AddStringToObject(c, "ovsdb", text);
    cJSON_AddStringToObject(c, "interface", e->interface);
    cJSON_AddStringToObject(c, "al_mac", e->al_mac);
    cJSON_AddItemToObject(c, "controller_al",
                          cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(fleet, "controller_al"), 1));
    /* the pod's model's settings (spec 4, the pods' models), by the model and firmware it reported */
    int index;
    const cJSON *model = em_fleet_model(fleet, e->model, e->firmware, &index);
    const char *id = e->pod_id;
    cJSON_AddItemToObject(c, "message_set", setting(fleet, id, model, "message_set", cJSON_CreateString("easymesh-6.1")));
    cJSON_AddItemToObject(c, "multi_bss", setting(fleet, id, model, "multi_bss", cJSON_CreateFalse()));
    cJSON_AddItemToObject(c, "m2_session", setting(fleet, id, model, "m2_session", cJSON_CreateString("distinct")));
    cJSON_AddItemToObject(c, "profile",
                          setting(fleet, id, model, "profile", cJSON_CreateString("opensync-lab-hwsim-6.6.1-v1")));
    cJSON_AddItemToObject(c, "uplink", setting(fleet, id, model, "uplink", mode_off()));
    cJSON_AddItemToObject(c, "telemetry", setting(fleet, id, model, "telemetry", mode_off()));
    /* the radios' own netdevs (spec 3.10, em_phy_netdev_ops, on by default), the pod's, its model's
     * or the fleet's, only when set: the configurations of a fleet without it stay as they were */
    cJSON *phy = setting(fleet, id, model, "phy_netdev_up", NULL);
    if (phy)
        cJSON_AddItemToObject(c, "phy_netdev_up", phy);
    /* a property of the controller, so the fleet's only, and only when set */
    const cJSON *window = cJSON_GetObjectItemCaseSensitive(fleet, "topology_query_window");
    if (window)
        cJSON_AddItemToObject(c, "topology_query_window", cJSON_Duplicate(window, 1));
    /* its modules' settings (spec 4), only when there are any */
    cJSON *modules = em_module_settings(fleet, id, model);
    if (modules)
        cJSON_AddItemToObject(c, "modules", modules);
    /* its underlay as a GRE parent (spec 8.6), while the fleet allocates them */
    if (e->underlay[0] && cJSON_GetObjectItemCaseSensitive(fleet, "gtp_underlay"))
        cJSON_AddStringToObject(c, "underlay", e->underlay);
    const char *root = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(fleet, "state_root"));
    /* a state directory that does not fit is left out, and the agent refuses its
     * configuration, rather than given a cut path, which would be another directory */
    if (em_format(text, sizeof(text), "%s/%s", root ? root : "", e->pod_id))
        cJSON_AddStringToObject(c, "state_dir", text);
    /* the status's place (a RAM disk on a gateway), left out the same way when too long */
    const char *run = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(fleet, "run_root"));
    if (run && em_format(text, sizeof(text), "%s/%s", run, e->pod_id))
        cJSON_AddStringToObject(c, "run_dir", text);
    cJSON_AddStringToObject(c, "run_id", e->pod_id);
    return c;
}

char *em_agent_config_text(const em_fleet_entry *e, const cJSON *fleet)
{
    cJSON *c = em_agent_config(e, fleet);
    char *text = with_newline(em_json_dumps(c, EM_JSON_INDENT2, false));
    cJSON_Delete(c);
    return text;
}

cJSON *em_manager_update(const em_fleet_entry *e, const cJSON *fleet)
{
    char target[320];
    const char *advertise = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(fleet, "advertise"));
    /* a host name is at most 253 characters; an address that does not fit is not written */
    if (!advertise || !em_format(target, sizeof(target), "tcp:%s:%d", advertise, e->port))
        return NULL;
    cJSON *o = cJSON_CreateObject(), *row = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "op", "update");
    cJSON_AddStringToObject(o, "table", "AWLAN_Node");
    cJSON_AddItemToObject(o, "where", cJSON_CreateArray());
    cJSON_AddStringToObject(row, "manager_addr", target);
    cJSON_AddItemToObject(o, "row", row);
    return o;
}

/* -- a fleet ------------------------------------------------------------------------- */

static const char *config_text(const cJSON *config, const char *key, const char *fallback)
{
    const char *v = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(config, key));
    return v ? v : fallback;
}

/* the registry as its file has it now: the file is the state, and another process (a
 * forget while the fleet serves) may have changed it (box finding 14) */
static bool reload(em_fleet *f)
{
    em_registry_free(&f->registry);
    if (access(f->registry_path, F_OK) && errno == ENOENT)
        return true; /* a new fleet */
    char *text = em_read_file(f->registry_path, REGISTRY_MAX, NULL);
    bool ok = text && em_registry_load(&f->registry, text);
    free(text);
    return ok;
}

bool em_fleet_open(em_fleet *f, cJSON *config, char *why, size_t size)
{
    memset(f, 0, sizeof(*f));
    f->config = config;
    f->database = config_text(config, "database", "Open_vSwitch");
    f->state_root = config_text(config, "state_root", "");
    f->config_dir = config_text(config, "config_dir", "");
    const cJSON *ports = cJSON_GetObjectItemCaseSensitive(config, "ports");
    f->registry.port_low = cJSON_GetArrayItem(ports, 0) ? cJSON_GetArrayItem(ports, 0)->valueint : 0;
    f->registry.port_high = cJSON_GetArrayItem(ports, 1) ? cJSON_GetArrayItem(ports, 1)->valueint : -1;
    if (!em_copy(f->registry.reserved_al, sizeof(f->registry.reserved_al), config_text(config, "controller_al", "")) ||
        !em_format(f->registry_path, sizeof(f->registry_path), "%s/fleet.json", f->state_root)) {
        (void)em_format(why, size, "state_root or controller_al too long");
        return false;
    }
    if (!em_copy(f->registry.gtp_underlay, sizeof(f->registry.gtp_underlay), config_text(config, "gtp_underlay", ""))) {
        (void)em_format(why, size, "gtp_underlay too long");
        return false;
    }
    /* a module's update is a file the fleet serves (spec 4, the pods' modules) */
    bool update = false;
    const cJSON *own, *module;
    cJSON_ArrayForEach(module, cJSON_GetObjectItemCaseSensitive(config, "modules"))
        update = update || cJSON_GetObjectItemCaseSensitive(module, "update");
    cJSON_ArrayForEach(own, cJSON_GetObjectItemCaseSensitive(config, "pods"))
    {
        cJSON_ArrayForEach(module, cJSON_GetObjectItemCaseSensitive(own, "modules"))
            update = update || cJSON_GetObjectItemCaseSensitive(module, "update");
    }
    cJSON_ArrayForEach(own, cJSON_GetObjectItemCaseSensitive(config, "models"))
    {
        cJSON_ArrayForEach(module, cJSON_GetObjectItemCaseSensitive(own, "modules"))
            update = update || cJSON_GetObjectItemCaseSensitive(module, "update");
    }
    if (update && !cJSON_GetObjectItemCaseSensitive(config, "files")) {
        (void)em_format(why, size, "a module's update needs the fleet's files");
        return false;
    }
    bool ok = reload(f);
    if (!ok)
        (void)em_format(why, size, "%s is unreadable or not a fleet registry", f->registry_path);
    return ok;
}

void em_fleet_close(em_fleet *f)
{
    em_registry_free(&f->registry);
    cJSON_Delete(f->config);
    f->config = NULL;
}

static const char *const IDENTITY[] = {"id", "serial_number", "model", "firmware_version"};

cJSON *em_fleet_select(const em_fleet *f)
{
    cJSON *params = cJSON_CreateArray(), *op = cJSON_CreateObject();
    cJSON_AddItemToArray(params, cJSON_CreateString(f->database));
    cJSON_AddStringToObject(op, "op", "select");
    cJSON_AddStringToObject(op, "table", "AWLAN_Node");
    cJSON_AddItemToObject(op, "where", cJSON_CreateArray());
    cJSON_AddItemToObject(op, "columns", cJSON_CreateStringArray(IDENTITY, 4));
    cJSON_AddItemToArray(params, op);
    return params;
}

void em_fleet_handover_clear(em_fleet_handover *h)
{
    cJSON_Delete(h->update);
    h->update = NULL;
}

/* how the fleet admits a pod (spec 4 step 2): "all" (admit is *), "serial" (admit lists it) or
 * "model" (admit_by_model on and one of its models recognises it); NULL when not admitted. With
 * how (when not NULL) the route and the recognising models entry, for the log. */
static const char *admitted(const cJSON *config, const char *serial, const char *model, const char *firmware,
                            char *how, size_t size)
{
    const cJSON *admit = cJSON_GetObjectItemCaseSensitive(config, "admit"), *s;
    int index;
    const cJSON *known = em_fleet_model(config, model, firmware, &index);
    const char *route = NULL;
    if (!cJSON_IsArray(admit)) {
        route = "all"; /* "*" or absent */
    } else {
        cJSON_ArrayForEach(s, admit)
        {
            if (!route && cJSON_IsString(s) && !strcmp(s->valuestring, serial))
                route = "serial";
        }
    }
    if (!route && known && cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(config, "admit_by_model")))
        route = "model";
    if (how && route) {
        const char *name = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(known, "model"));
        if (known)
            (void)em_format(how, size, "%s, models[%d] (%s)", route, index, name ? name : "?");
        else
            (void)em_format(how, size, "%s", route);
    }
    return route;
}

/* the value of a row's string column, or NULL */
static const char *row_text(const cJSON *row, const char *key)
{
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(row, key));
}

/* text as written atomically (durable: a handover must survive a power loss) when it
 * differs from the file's; *changed when there was another one */
static bool write_if_changed(const char *path, const char *text, bool *changed)
{
    char *old = em_read_file(path, CONFIG_MAX, NULL);
    *changed = old && strcmp(old, text);
    bool same = old && !*changed;
    free(old);
    return same || em_write_file(path, text, true);
}

em_fleet_outcome em_fleet_identify(em_fleet *f, const cJSON *select_result, double now,
                                   em_fleet_handover *out)
{
    return em_fleet_identify_at(f, select_result, 0, now, out);
}

size_t em_fleet_free_ports(em_fleet *f, int *ports, size_t cap)
{
    if (!reload(f))
        return SIZE_MAX;
    size_t n = 0;
    for (int p = f->registry.port_low; p <= f->registry.port_high && n < cap; p++)
        if (em_registry_port_free(&f->registry, p))
            ports[n++] = p;
    return n;
}

em_fleet_outcome em_fleet_identify_at(em_fleet *f, const cJSON *select_result, int port, double now,
                                      em_fleet_handover *out)
{
    memset(out, 0, sizeof(*out));
    out->dialled = port;
    const cJSON *first = cJSON_GetArrayItem(select_result, 0);
    const cJSON *rows = cJSON_GetObjectItemCaseSensitive(cJSON_IsObject(first) ? first : NULL, "rows");
    int n = cJSON_IsArray(rows) ? cJSON_GetArraySize(rows) : 0;
    if (n != 1) {
        (void)em_format(out->why, sizeof(out->why), "expected one AWLAN_Node row, got %d", n);
        return EM_FLEET_INVALID;
    }
    const cJSON *row = cJSON_GetArrayItem(rows, 0);
    const char *serial = row_text(row, "serial_number");
    if (!serial || !em_serial_valid(serial)) {
        /* the value is the pod's: shown escaped and cut, never as it came */
        const cJSON *value = cJSON_GetObjectItemCaseSensitive(row, "serial_number");
        char *shown = value ? em_json_dumps(value, EM_JSON_COMPACT, true) : NULL;
        (void)em_format(out->why, sizeof(out->why), "unusable serial %.64s", shown ? shown : "None");
        free(shown);
        return EM_FLEET_INVALID;
    }
    EM_FORMAT_FIXED(out->serial, sizeof(out->serial), "%s", serial); /* valid: at most 64 */
    (void)em_copy(out->node, sizeof(out->node), row_text(row, "id") ? row_text(row, "id") : "None");
    if (!admitted(f->config, serial, row_text(row, "model"), row_text(row, "firmware_version"), out->admission,
                  sizeof(out->admission))) {
        EM_FORMAT_FIXED(out->why, sizeof(out->why), "pod %s not admitted: left unchanged", serial);
        return EM_FLEET_REFUSED;
    }
    if (!reload(f)) {
        EM_FORMAT_FIXED(out->why, sizeof(out->why), "pod %s: the fleet registry is unreadable", serial);
        return EM_FLEET_FAILED;
    }
    em_fleet_entry *e;
    if (port) {
        e = em_registry_adopt(&f->registry, serial, row_text(row, "id"), row_text(row, "model"),
                              row_text(row, "firmware_version"), port, now, &out->placed);
        if (!e) {
            EM_FORMAT_FIXED(out->why, sizeof(out->why), "pod %s on agent port %d, another pod's: left unchanged",
                            serial, port);
            return EM_FLEET_REFUSED;
        }
    } else {
        out->placed = EM_FLEET_FRONT;
        e = em_registry_assign(&f->registry, serial, row_text(row, "id"), row_text(row, "model"),
                               row_text(row, "firmware_version"), now);
        if (!e) {
            EM_FORMAT_FIXED(out->why, sizeof(out->why), "pod %s: no free agent port, left unchanged", serial);
            return EM_FLEET_REFUSED;
        }
    }
    out->entry = *e;
    out->entry.node_id = out->entry.model = out->entry.firmware = NULL;
    char *registry = em_registry_dump(&f->registry);
    bool saved = registry && em_write_file(f->registry_path, registry, true);
    free(registry);
    char path[600];
    cJSON *agent = em_agent_config(e, f->config);
    if (agent)
        em_fleet_provision_secrets(f, agent);
    cJSON_Delete(agent);
    char *config = em_agent_config_text(e, f->config);
    bool written = saved && config &&
                   em_format(path, sizeof(path), "%s/%s.json", f->config_dir, e->pod_id) &&
                   write_if_changed(path, config, &out->changed);
    free(config);
    cJSON *update = written ? em_manager_update(e, f->config) : NULL;
    if (!update) {
        (void)em_format(out->why, sizeof(out->why), "pod %s: %s not written", serial,
                        !saved ? "the registry" : !written ? "its agent configuration" : "manager_addr");
        return EM_FLEET_FAILED;
    }
    out->update = cJSON_CreateArray();
    cJSON_AddItemToArray(out->update, cJSON_CreateString(f->database));
    cJSON_AddItemToArray(out->update, update);
    return EM_FLEET_HANDOVER;
}

size_t em_fleet_start_registered(em_fleet *f, void (*start)(void *ctx, const char *pod_id, bool changed),
                                 void *ctx)
{
    if (!reload(f))
        return SIZE_MAX;
    /* the underlays first, so the agents start with theirs: saved before any is handed out */
    if (em_registry_settle(&f->registry)) {
        char *registry = em_registry_dump(&f->registry);
        bool saved = registry && em_write_file(f->registry_path, registry, true);
        free(registry);
        if (!saved)
            return SIZE_MAX;
    }
    size_t failed = 0;
    for (size_t i = 0; i < f->registry.count; i++) {
        const em_fleet_entry *e = &f->registry.entries[i];
        if (!admitted(f->config, e->pod_id, e->model, e->firmware, NULL, 0))
            continue;
        char path[600];
        bool changed = false;
        cJSON *agent = em_agent_config(e, f->config);
        if (agent)
            em_fleet_provision_secrets(f, agent);
        cJSON_Delete(agent);
        char *config = em_agent_config_text(e, f->config);
        bool written = config && em_format(path, sizeof(path), "%s/%s.json", f->config_dir, e->pod_id) &&
                       write_if_changed(path, config, &changed);
        free(config);
        if (written)
            start(ctx, e->pod_id, changed);
        else
            failed++;
    }
    return failed;
}

bool em_fleet_update_ok(const cJSON *result)
{
    const cJSON *first = cJSON_GetArrayItem(result, 0);
    const cJSON *error = cJSON_GetObjectItemCaseSensitive(first, "error");
    const cJSON *count = cJSON_GetObjectItemCaseSensitive(first, "count");
    return cJSON_IsObject(first) && (!error || cJSON_IsNull(error) || cJSON_IsFalse(error)) &&
           cJSON_IsNumber(count) && count->valuedouble == 1;
}

static int by_ref(const void *a, const void *b) { return strcmp(*(const char *const *)a, *(const char *const *)b); }

void em_fleet_provision_secrets(const em_fleet *f, const cJSON *agent_config)
{
    const char *source = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(f->config, "secrets"));
    const char *pod = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(agent_config, "pod_id"));
    const char *state = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(agent_config, "state_dir"));
    const cJSON *uplink = cJSON_GetObjectItemCaseSensitive(agent_config, "uplink");
    const char *refs[2] = {
        cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(uplink, "secret_ref")),
        cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(uplink, "bootstrap"),
                                                              "secret_ref"))};
    size_t n = 0;
    for (size_t i = 0; i < 2; i++)
        if (refs[i])
            refs[n++] = refs[i];
    if (!source || !pod || !state || !n)
        return;
    em_sort(refs, n, sizeof(*refs), by_ref);
    if (n == 2 && !strcmp(refs[0], refs[1]))
        n = 1;
    char dir[600];
    em_vault fleet, own;
    /* the agent's state directory may not exist before its first start */
    if ((mkdir(state, 0700) && errno != EEXIST) || !em_format(dir, sizeof(dir), "%s/secrets", state) ||
        em_vault_open(&fleet, source) != EM_OK || em_vault_open(&own, dir) != EM_OK)
        return;
    for (size_t i = 0; i < n; i++) {
        size_t len = 0;
        uint8_t *have = own.backend.read(&own, refs[i], &len);
        if (have) { /* the agent's own: never replaced */
            OPENSSL_cleanse(have, len);
            free(have);
            continue;
        }
        uint8_t *value = fleet.backend.read(&fleet, refs[i], &len);
        if (!value) {
            if (f->secret_note)
                f->secret_note(pod, refs[i], false);
            continue;
        }
        (void)own.backend.create(&own, refs[i], value, len); /* made meanwhile: that one stands */
        OPENSSL_cleanse(value, len);
        free(value);
        if (f->secret_note)
            f->secret_note(pod, refs[i], true);
    }
}

cJSON *em_fleet_forget(em_fleet *f, const char *serial, const char *stamp,
                       void (*stop)(void *ctx, const char *pod_id), void *ctx)
{
    em_fleet_entry e;
    bool found = reload(f) && em_registry_forget(&f->registry, serial, &e);
    char *registry = em_registry_dump(&f->registry);
    if (registry)
        (void)em_write_file(f->registry_path, registry, true); /* as the reference: saved even unchanged */
    free(registry);
    if (!found)
        return NULL;
    cJSON *out = em_fleet_entry_json(&e);
    stop(ctx, e.pod_id);
    char path[600], archive[700];
    if (em_format(path, sizeof(path), "%s/%s.json", f->config_dir, e.pod_id))
        (void)unlink(path); /* missing_ok */
    struct stat st;
    if (em_format(path, sizeof(path), "%s/%s", f->state_root, e.pod_id) && !stat(path, &st) &&
        S_ISDIR(st.st_mode) && em_format(archive, sizeof(archive), "%s.released-%s", path, stamp) &&
        !rename(path, archive)) {
        cJSON_AddStringToObject(out, "archived_state", archive);
        /* not the status: a running agent's (in the run directory, its link would show
         * the next agent's) */
        char status[720];
        if (em_format(status, sizeof(status), "%s/status.json", archive))
            (void)unlink(status); /* missing_ok */
    }
    em_fleet_entry_clear(&e);
    return out;
}
