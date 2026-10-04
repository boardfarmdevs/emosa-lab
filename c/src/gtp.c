/* The GRE termination point (spec §8.2). Mirrors emosa.gtp. */
#include "gtp.h"

#include "log.h"
#include "proc.h"

#include <regex.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>

#define LEASES_MAX (4 * 1024 * 1024)

static void info(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void info(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    em_vlog(EM_LOG_INFO, "emosa.gtp", fmt, ap);
    va_end(ap);
}

/* -- addresses --------------------------------------------------------------------- */

/* a dotted IPv4 address, each octet 0..255 without a sign or leading junk */
static bool parse_ip(const char *text, uint32_t *out)
{
    uint32_t v = 0;
    const char *p = text;
    for (int i = 0; i < 4; i++) {
        if (*p < '0' || *p > '9')
            return false;
        unsigned octet = 0;
        int digits = 0;
        while (*p >= '0' && *p <= '9') {
            octet = octet * 10 + (unsigned)(*p++ - '0');
            if (++digits > 3 || octet > 255)
                return false;
        }
        v = (v << 8) | octet;
        if (i < 3 && *p++ != '.')
            return false;
    }
    if (*p)
        return false;
    *out = v;
    return true;
}

/* "a.b.c.d/n" */
static bool parse_interface(const char *text, uint32_t *ip, int *prefix)
{
    char copy[32];
    if (!em_copy(copy, sizeof(copy), text))
        return false;
    char *slash = strchr(copy, '/');
    if (!slash)
        return false;
    *slash = 0;
    const char *p = slash + 1;
    int n = 0, digits = 0;
    while (*p >= '0' && *p <= '9' && digits < 3) {
        n = n * 10 + (*p++ - '0');
        digits++;
    }
    if (!digits || *p || n > 32)
        return false;
    *prefix = n;
    return parse_ip(copy, ip);
}

static uint32_t prefix_mask(int prefix) { return prefix ? 0xFFFFFFFFu << (32 - prefix) : 0; }

static void format_ip(uint32_t v, char out[16])
{
    EM_FORMAT_FIXED(out, 16, "%u.%u.%u.%u", v >> 24, (v >> 16) & 255, (v >> 8) & 255, v & 255);
}

static const cJSON *underlay_of(const cJSON *config) { return cJSON_GetObjectItemCaseSensitive(config, "underlay"); }
static const char *text_of(const cJSON *o, const char *key)
{
    const char *v = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, key));
    return v ? v : "";
}
static int int_of(const cJSON *o, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, key);
    return cJSON_IsNumber(v) ? v->valueint : 0;
}

bool em_gtp_check(const cJSON *config, char *why, size_t size)
{
    const cJSON *u = underlay_of(config), *range = cJSON_GetObjectItemCaseSensitive(u, "dhcp_range");
    uint32_t ip, first, last;
    int prefix;
    if (!parse_interface(text_of(u, "address"), &ip, &prefix) ||
        !parse_ip(cJSON_GetStringValue(cJSON_GetArrayItem(range, 0)) ? cJSON_GetStringValue(cJSON_GetArrayItem(range, 0)) : "", &first) ||
        !parse_ip(cJSON_GetStringValue(cJSON_GetArrayItem(range, 1)) ? cJSON_GetStringValue(cJSON_GetArrayItem(range, 1)) : "", &last)) {
        (void)em_format(why, size, "the underlay address or DHCP range is not an IPv4 address");
        return false;
    }
    uint32_t mask = prefix_mask(prefix), network = ip & mask;
    if (prefix < 16 || (network & 0xFFFF0000u) != 0xA9FE0000u) {
        (void)em_format(why, size, "the underlay must be link-local (169.254.0.0/16): OpenSync builds no GRE otherwise");
        return false;
    }
    if (ip != network + 1) {
        (void)em_format(why, size, "the GTP must be the first host (.1) of the underlay subnet: OpenSync's GRE remote");
        return false;
    }
    if ((first & mask) != network || (last & mask) != network || !(ip < first && first <= last)) {
        (void)em_format(why, size, "the DHCP range must lie in the underlay subnet, above .1");
        return false;
    }
    if (int_of(config, "tunnel_mtu") + EM_GRETAP_OVERHEAD > int_of(u, "mtu")) {
        (void)em_format(why, size, "underlay MTU too small for the tunnel MTU plus 38 bytes of gretap");
        return false;
    }
    return true;
}

bool em_gtp_tunnel_name(const char *ip, char out[16])
{
    uint32_t v;
    if (!parse_ip(ip, &v))
        return false;
    EM_FORMAT_FIXED(out, 16, "gtp%u_%u", (v >> 8) & 255, v & 255);
    return true;
}

/* -- dnsmasq ------------------------------------------------------------------------- */

/* the state directory as pathlib writes it: no trailing slash */
static void state_path(const cJSON *config, char *out, size_t size)
{
    (void)em_copy(out, size, text_of(config, "state_dir"));
    for (size_t n = strlen(out); n > 1 && out[n - 1] == '/'; n--)
        out[n - 1] = 0;
}

char *em_gtp_dnsmasq(const cJSON *config)
{
    const cJSON *u = underlay_of(config), *range = cJSON_GetObjectItemCaseSensitive(u, "dhcp_range");
    uint32_t ip = 0;
    int prefix = 0;
    char netmask[16], state[512];
    (void)parse_interface(text_of(u, "address"), &ip, &prefix);
    format_ip(prefix_mask(prefix), netmask);
    state_path(config, state, sizeof(state));
    em_buf b = {0};
    char line[1200];
    bool ok = true;
#define LINE(...) ok = ok && em_format(line, sizeof(line), __VA_ARGS__) && em_buf_put(&b, line, strlen(line))
    LINE("# generated by emosa-gtp setup; do not edit\n");
    LINE("interface=%s\n", text_of(u, "interface"));
    LINE("bind-dynamic\nexcept-interface=lo\nport=0\n");
    LINE("dhcp-range=%s,%s,%s,%s\n", cJSON_GetStringValue(cJSON_GetArrayItem(range, 0)),
         cJSON_GetStringValue(cJSON_GetArrayItem(range, 1)), netmask, text_of(u, "lease_time"));
    LINE("dhcp-option=option:mtu,%d\n", int_of(u, "mtu"));
    LINE("dhcp-option=option:router\ndhcp-option=option:dns-server\n");
    LINE("dhcp-leasefile=%s/leases\n", state);
    LINE("dhcp-script=%s/hook\n", state);
    LINE("script-on-renewal\nuser=root\n");
    LINE("pid-file=%s/dnsmasq.pid\n", state);
#undef LINE
    ok = ok && em_buf_u8(&b, 0);
    if (!ok) {
        em_buf_free(&b);
        return NULL;
    }
    return (char *)b.data;
}

char *em_gtp_hook(const char *exe, const char *config_path)
{
    size_t n = strlen(exe) + strlen(config_path) + 64;
    char *text = em_malloc(n);
    if (!em_format(text, n, "#!/bin/sh\nexec %s lease \"%s\" \"$@\"\n", exe, config_path)) {
        free(text);
        return NULL;
    }
    return text;
}

/* -- the links ---------------------------------------------------------------------- */

static char *ip_run(em_gtp *g, bool check, const char *const *args, size_t n)
{
    return g->ip.run(g->ip.ctx, args, n, check);
}
#define IP(g, check, ...) ip_run((g), (check), (const char *const[]){__VA_ARGS__}, \
                                 sizeof((const char *const[]){__VA_ARGS__}) / sizeof(const char *))

static bool exists(em_gtp *g, const char *name)
{
    char *out = IP(g, false, "-br", "link", "show", "dev", name);
    bool found = false;
    for (const char *p = out ? out : ""; *p && !found; p++)
        found = *p != ' ' && *p != '\n' && *p != '\t' && *p != '\r';
    free(out);
    return found;
}

typedef struct {
    char name[16], local[16], remote[16];
    bool pinned;
} tunnel;

typedef struct {
    tunnel *items;
    size_t n;
} tunnels;

/* one regex match group as a bounded string; false when it does not fit */
static bool group(const char *line, const regmatch_t *m, char *out, size_t size)
{
    size_t len = (size_t)(m->rm_eo - m->rm_so);
    if (m->rm_so < 0 || len >= size)
        return false;
    memcpy(out, line + m->rm_so, len);
    out[len] = 0;
    return true;
}

/* our tunnels: name, local, remote and whether pinned to a device, in ip's order */
static tunnels gretaps(em_gtp *g)
{
    tunnels t = {0};
    char *out = IP(g, false, "-d", "-o", "link", "show", "type", "gretap");
    regex_t re;
    if (!out || regcomp(&re, "^[0-9]+: (gtp[0-9]+_[0-9]+)[@:].* remote ([^[:space:]]+) local ([^[:space:]]+)( dev ([^[:space:]]+))?",
                        REG_EXTENDED)) {
        free(out);
        return t;
    }
    for (char *line = out, *next; line && *line; line = next) {
        next = strchr(line, '\n');
        if (next)
            *next++ = 0;
        regmatch_t m[6];
        tunnel one = {0};
        if (regexec(&re, line, 6, m, 0) || !group(line, &m[1], one.name, sizeof(one.name)) ||
            !group(line, &m[2], one.remote, sizeof(one.remote)) || !group(line, &m[3], one.local, sizeof(one.local)))
            continue;
        one.pinned = m[5].rm_so >= 0;
        bool seen = false; /* a name again replaces it, as the reference's dict does */
        for (size_t i = 0; i < t.n && !seen; i++)
            if (!strcmp(t.items[i].name, one.name)) {
                t.items[i] = one;
                seen = true;
            }
        if (!seen) {
            t.items = em_realloc(t.items, (t.n + 1) * sizeof(*t.items));
            t.items[t.n++] = one;
        }
    }
    regfree(&re);
    free(out);
    return t;
}

static const tunnel *find_tunnel(const tunnels *t, const char *name)
{
    for (size_t i = 0; i < t->n; i++)
        if (!strcmp(t->items[i].name, name))
            return &t->items[i];
    return NULL;
}

/* the bridge's (or interface's) ports */
static size_t ports(em_gtp *g, const char *bridge, char (*out)[16], size_t max)
{
    char *text = IP(g, false, "-o", "link", "show", "master", bridge);
    regex_t re;
    size_t n = 0;
    if (!text || regcomp(&re, "^[0-9]+: ([^:@[:space:]]+)", REG_EXTENDED)) {
        free(text);
        return 0;
    }
    for (char *line = text, *next; line && *line && n < max; line = next) {
        next = strchr(line, '\n');
        if (next)
            *next++ = 0;
        regmatch_t m[2];
        if (!regexec(&re, line, 2, m, 0) && group(line, &m[1], out[n], sizeof(out[n])))
            n++;
    }
    regfree(&re);
    free(text);
    return n;
}

/* -- leases ------------------------------------------------------------------------- */

typedef struct {
    char ip[16], mac[18];
} lease;

typedef struct {
    lease *items;
    size_t n;
} leases;

static void lower(char *s)
{
    for (; *s; s++)
        if (*s >= 'A' && *s <= 'Z')
            *s = (char)(*s - 'A' + 'a');
}

/* dnsmasq's lease file: "EXPIRY MAC IP ..." per line, 0 never expiring; the current ones */
static leases read_leases(em_gtp *g)
{
    leases l = {0};
    char path[600];
    if (!em_format(path, sizeof(path), "%s/leases", g->state_dir))
        return l;
    char *text = em_read_file(path, LEASES_MAX, NULL);
    long long now = (long long)time(NULL);
    for (char *line = text, *next; line && *line; line = next) {
        next = strchr(line, '\n');
        if (next)
            *next++ = 0;
        char expiry[24], mac[64], ip[64];
        int fields = sscanf(line, "%23s %63s %63s", expiry, mac, ip); /* NOLINT(cert-err34-c): strings only */
        if (fields < 3)
            continue;
        char *end;
        long long when = strtoll(expiry, &end, 10);
        if (*end || (strcmp(expiry, "0") && when <= now))
            continue;
        lease one;
        if (!em_copy(one.ip, sizeof(one.ip), ip) || !em_copy(one.mac, sizeof(one.mac), mac))
            continue;
        lower(one.mac);
        bool seen = false;
        for (size_t i = 0; i < l.n && !seen; i++)
            if (!strcmp(l.items[i].ip, one.ip)) {
                l.items[i] = one;
                seen = true;
            }
        if (!seen) {
            l.items = em_realloc(l.items, (l.n + 1) * sizeof(*l.items));
            l.items[l.n++] = one;
        }
    }
    free(text);
    return l;
}

static const char *lease_mac(const leases *l, const char *ip)
{
    for (size_t i = 0; i < l->n; i++)
        if (!strcmp(l->items[i].ip, ip))
            return l->items[i].mac;
    return NULL;
}

/* -- the commands --------------------------------------------------------------------- */

bool em_gtp_init(em_gtp *g, const cJSON *config, em_ip ip, char *why, size_t size)
{
    memset(g, 0, sizeof(*g));
    g->ip = ip;
    g->config = config;
    const cJSON *u = underlay_of(config);
    uint32_t address;
    int prefix;
    if (!parse_interface(text_of(u, "address"), &address, &prefix)) {
        (void)em_format(why, size, "the underlay address is not an IPv4 interface address");
        return false;
    }
    g->mask = prefix_mask(prefix);
    g->network = address & g->mask;
    format_ip(address, g->local);
    g->tunnel_mtu = int_of(config, "tunnel_mtu");
    state_path(config, g->state_dir, sizeof(g->state_dir));
    if (!em_copy(g->underlay, sizeof(g->underlay), text_of(u, "interface")) ||
        !em_copy(g->bridge, sizeof(g->bridge), text_of(cJSON_GetObjectItemCaseSensitive(config, "lan"), "bridge"))) {
        (void)em_format(why, size, "an interface name is longer than 15 characters");
        return false;
    }
    return true;
}

/* a checked ip command: false (why) when it failed */
static bool run_checked(em_gtp *g, char *why, size_t size, const char *const *args, size_t n)
{
    char *out = ip_run(g, true, args, n);
    if (!out) {
        (void)em_format(why, size, "ip %s %s %s failed", args[0], n > 1 ? args[1] : "", n > 2 ? args[2] : "");
        return false;
    }
    free(out);
    return true;
}
#define RUN(g, why, size, ...) run_checked((g), (why), (size), (const char *const[]){__VA_ARGS__}, \
                                           sizeof((const char *const[]){__VA_ARGS__}) / sizeof(const char *))

static bool ensure(em_gtp *g, const char *ip, char *why, size_t size)
{
    char name[16], mtu[12];
    if (!em_gtp_tunnel_name(ip, name)) {
        (void)em_format(why, size, "lease outside the underlay");
        return false;
    }
    tunnels t = gretaps(g);
    const tunnel *current = find_tunnel(&t, name);
    /* routed by address, never pinned to the underlay device: an AP that recreates its
     * bridge (hostapd does on restart) must not leave a dead tunnel */
    bool same = current && !strcmp(current->local, g->local) && !strcmp(current->remote, ip) && !current->pinned;
    bool present = current != NULL;
    free(t.items);
    if (same)
        return true;
    if (present && !RUN(g, why, size, "link", "del", name))
        return false;
    EM_FORMAT_FIXED(mtu, sizeof(mtu), "%d", g->tunnel_mtu);
    if (!RUN(g, why, size, "link", "add", name, "type", "gretap", "local", g->local, "remote", ip) ||
        !RUN(g, why, size, "link", "set", name, "mtu", mtu) ||
        !RUN(g, why, size, "link", "set", name, "master", g->bridge, "up"))
        return false;
    info("tunnel %s: %s -> %s in %s", name, g->local, ip, g->bridge);
    return true;
}

static bool remove_tunnel(em_gtp *g, const char *ip, char *why, size_t size)
{
    char name[16];
    if (!em_gtp_tunnel_name(ip, name))
        return true;
    tunnels t = gretaps(g);
    bool present = find_tunnel(&t, name) != NULL;
    free(t.items);
    if (!present)
        return true;
    if (!RUN(g, why, size, "link", "del", name))
        return false;
    info("tunnel %s removed (%s)", name, ip);
    return true;
}

static int compare_names(const void *a, const void *b)
{
    return strcmp(((const tunnel *)a)->name, ((const tunnel *)b)->name);
}

cJSON *em_gtp_list(em_gtp *g, char *why, size_t size)
{
    (void)why;
    (void)size;
    leases l = read_leases(g);
    tunnels t = gretaps(g);
    em_sort(t.items, t.n, sizeof(*t.items), compare_names);
    cJSON *out = cJSON_CreateObject();
    for (size_t i = 0; i < t.n; i++) {
        cJSON *one = cJSON_CreateObject();
        const char *mac = lease_mac(&l, t.items[i].remote);
        cJSON_AddStringToObject(one, "remote", t.items[i].remote);
        cJSON_AddItemToObject(one, "mac", mac ? cJSON_CreateString(mac) : cJSON_CreateNull());
        cJSON_AddItemToObject(out, t.items[i].name, one);
    }
    free(t.items);
    free(l.items);
    return out;
}

static int compare_ips(const void *a, const void *b) { return strcmp(((const lease *)a)->ip, ((const lease *)b)->ip); }

cJSON *em_gtp_reconcile(em_gtp *g, char *why, size_t size)
{
    leases l = read_leases(g);
    tunnels t = gretaps(g);
    bool ok = true;
    for (size_t i = 0; ok && i < t.n; i++)
        if (!lease_mac(&l, t.items[i].remote))
            ok = RUN(g, why, size, "link", "del", t.items[i].name);
    free(t.items);
    em_sort(l.items, l.n, sizeof(*l.items), compare_ips); /* the reference's sorted(): as text */
    for (size_t i = 0; ok && i < l.n; i++)
        ok = ensure(g, l.items[i].ip, why, size);
    free(l.items);
    return ok ? em_gtp_list(g, why, size) : NULL;
}

static bool valid_mac(const char *mac)
{
    if (strlen(mac) != 17)
        return false;
    for (int i = 0; i < 17; i++) {
        char c = mac[i];
        bool hex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
        if (i % 3 == 2 ? c != ':' : !hex)
            return false;
    }
    return true;
}

cJSON *em_gtp_lease(em_gtp *g, const char *action, const char *mac, const char *ip, char *why, size_t size)
{
    uint32_t address;
    if (!parse_ip(ip, &address) || (address & g->mask) != g->network || !valid_mac(mac)) {
        (void)em_format(why, size, "lease outside the underlay");
        return NULL;
    }
    bool ok = true;
    if (!strcmp(action, "add") || !strcmp(action, "old"))
        ok = ensure(g, ip, why, size);
    else if (!strcmp(action, "del"))
        ok = remove_tunnel(g, ip, why, size);
    return ok ? em_gtp_list(g, why, size) : NULL;
}

cJSON *em_gtp_setup(em_gtp *g, const char *config_path, const char *exe, char *why, size_t size)
{
    const cJSON *u = underlay_of(g->config), *lan = cJSON_GetObjectItemCaseSensitive(g->config, "lan"), *port;
    if (!exists(g, g->underlay)) {
        (void)em_format(why, size, "underlay interface %s does not exist", g->underlay);
        return NULL;
    }
    char mtu[12], found[64][16];
    EM_FORMAT_FIXED(mtu, sizeof(mtu), "%d", int_of(u, "mtu"));
    /* a bridge carries only what its smallest port does: raise the ports too (the
     * pod-backhaul AP interface), as mv3 runs its backhaul AP at 1600 */
    size_t n = ports(g, g->underlay, found, 64);
    for (size_t i = 0; i < n; i++)
        if (!RUN(g, why, size, "link", "set", found[i], "mtu", mtu))
            return NULL;
    if (!RUN(g, why, size, "link", "set", g->underlay, "mtu", mtu, "up") ||
        !RUN(g, why, size, "addr", "replace", text_of(u, "address"), "dev", g->underlay))
        return NULL;
    if (!exists(g, g->bridge) && !RUN(g, why, size, "link", "add", g->bridge, "type", "bridge"))
        return NULL;
    if (!RUN(g, why, size, "link", "set", g->bridge, "up"))
        return NULL;
    cJSON_ArrayForEach(port, cJSON_GetObjectItemCaseSensitive(lan, "ports"))
    {
        const char *name = cJSON_GetStringValue(port);
        if (!name || !exists(g, name)) {
            (void)em_format(why, size, "LAN port %s does not exist", name ? name : "");
            return NULL;
        }
        if (!RUN(g, why, size, "link", "set", name, "master", g->bridge, "up"))
            return NULL;
    }
    char path[600];
    char *conf = em_gtp_dnsmasq(g->config), *hook = em_gtp_hook(exe, config_path);
    bool ok = conf && hook && em_mkdirs(g->state_dir, 0755) &&
              em_format(path, sizeof(path), "%s/dnsmasq.conf", g->state_dir) && em_write_file(path, conf, false) &&
              em_format(path, sizeof(path), "%s/hook", g->state_dir) && em_write_file(path, hook, false) &&
              !chmod(path, 0755);
    free(conf);
    free(hook);
    if (!ok) {
        (void)em_format(why, size, "%s: dnsmasq's configuration not written", g->state_dir);
        return NULL;
    }
    return em_gtp_reconcile(g, why, size);
}
