/* SPDX-License-Identifier: Apache-2.0 */
/* A GRE parent's tunnels (emosa.opensync.gre_parent, emosa.agent.gre_parent; spec 8.6). */
#include "scope_gre_parent.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "canon.h"
#include "log.h"
#include "ovs.h"
#include "scope.h"
#include "southbound.h"

#define MODE "opensync-6.6-gre-parent"
#define PROVENANCE "opensync-nm:Wifi_Inet_State"
#define SOURCE "gre-parent-policy"
#define DEADLINE 30
#define TUNNEL_MTU 1562
#define MAX_ROWS 512

static const char *const INET_GUARDS[] = {"if_name", "if_type", "gre_ifname", "gre_local_inet_addr",
                                          "gre_remote_inet_addr"};

static const char *str(const cJSON *o, const char *k)
{
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, k));
}

/* a.b.c.d: four decimal octets (ipaddress.IPv4Address); false otherwise */
static bool ipv4(const char *text, uint32_t *out)
{
    uint32_t address = 0;
    const char *at = text;
    for (int part = 0; at && part < 4; part++) {
        if (*at < '0' || *at > '9')
            return false;
        char *end = NULL;
        long v = strtol(at, &end, 10);
        if (end - at > 3 || v > 255 || *end != (part < 3 ? '.' : '\0'))
            return false;
        address = address << 8 | (uint32_t)v;
        at = end + 1;
    }
    *out = address;
    return at != NULL;
}

static void ipv4_text(uint32_t address, char out[16])
{
    EM_FORMAT_FIXED(out, 16, "%u.%u.%u.%u", address >> 24, (address >> 16) & 0xff, (address >> 8) & 0xff,
                    address & 0xff);
}

/* pgd<b3>_<b4>: the child's address's last two octets, as the OpenSync cloud names it */
static bool tunnel_name(const char *remote, char out[16])
{
    uint32_t a;
    if (!ipv4(remote, &a))
        return false;
    EM_FORMAT_FIXED(out, 16, "pgd%u_%u", (a >> 8) & 0xff, a & 0xff);
    return true;
}

/* an interface name: 1 to 15 of [A-Za-z0-9_.-] (the reference's INTERFACE) */
static bool interface_name(const char *s)
{
    size_t n = s ? strlen(s) : 0;
    if (n < 1 || n > 15)
        return false;
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '.' ||
              c == '-'))
            return false;
    }
    return true;
}

/* TunnelIntent.validate */
static em_reason validate(const em_gre_intent *i)
{
    if (!interface_name(i->ap) || !interface_name(i->bridge))
        return EM_INVALID_INPUT;
    char names[EM_GRE_CHILDREN][16];
    for (size_t k = 0; k < i->nremotes; k++) {
        if (!tunnel_name(i->remotes[k], names[k]))
            return EM_INVALID_INPUT;
        for (size_t m = 0; m < k; m++)
            if (!strcmp(names[m], names[k]))
                return EM_INVALID_INPUT; /* two children share a tunnel name */
    }
    return EM_OK;
}

/* TunnelIntent.record() */
static cJSON *record(const em_gre_intent *i)
{
    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "pod_id", i->pod_id);
    cJSON_AddStringToObject(r, "ap", i->ap);
    cJSON_AddStringToObject(r, "bridge", i->bridge);
    cJSON *remotes = cJSON_AddArrayToObject(r, "remotes");
    for (size_t k = 0; k < i->nremotes; k++)
        cJSON_AddItemToArray(remotes, cJSON_CreateString(i->remotes[k]));
    return r;
}

static bool from_record(const cJSON *r, em_gre_intent *i)
{
    memset(i, 0, sizeof(*i));
    const cJSON *remotes = cJSON_GetObjectItemCaseSensitive(r, "remotes"), *remote;
    if (!cJSON_IsArray(remotes) || cJSON_GetArraySize(remotes) > EM_GRE_CHILDREN ||
        !em_copy(i->pod_id, sizeof(i->pod_id), str(r, "pod_id") ? str(r, "pod_id") : "") || !*i->pod_id ||
        !em_copy(i->ap, sizeof(i->ap), str(r, "ap") ? str(r, "ap") : "") ||
        !em_copy(i->bridge, sizeof(i->bridge), str(r, "bridge") ? str(r, "bridge") : ""))
        return false;
    cJSON_ArrayForEach(remote, remotes)
    {
        if (!cJSON_IsString(remote) || !em_copy(i->remotes[i->nremotes], 16, remote->valuestring))
            return false;
        i->nremotes++;
    }
    return validate(i) == EM_OK;
}

static int compare_text(const void *a, const void *b) { return strcmp(*(const char *const *)a, *(const char *const *)b); }

/* TunnelIntent.target(): {"ap", "bridge", "tunnels": {name: remote}} */
static cJSON *target_of(const em_gre_intent *i)
{
    cJSON *t = cJSON_CreateObject();
    cJSON_AddStringToObject(t, "ap", i->ap);
    cJSON_AddStringToObject(t, "bridge", i->bridge);
    cJSON *tunnels = cJSON_AddObjectToObject(t, "tunnels");
    for (size_t k = 0; k < i->nremotes; k++) {
        char name[16];
        if (tunnel_name(i->remotes[k], name))
            cJSON_AddStringToObject(tunnels, name, i->remotes[k]);
    }
    return t;
}

static cJSON *target(void *ctx, const cJSON *intent, em_reason *why)
{
    (void)ctx;
    em_gre_intent *i = em_malloc(sizeof(*i));
    cJSON *t = from_record(intent, i) ? target_of(i) : NULL;
    free(i);
    if (!t)
        *why = EM_INVALID_INPUT;
    return t;
}

/* -- the pod's rows -------------------------------------------------------------------- */

/* the bridge's row, when exactly one has its name */
static const cJSON *bridge_row(const em_gre_scope *g, const cJSON *tables, const char **uuid)
{
    const cJSON *found = NULL, *r;
    size_t n = 0;
    cJSON_ArrayForEach(r, em_table(tables, "Bridge"))
    {
        const char *name = ovs_str(r, "name");
        if (name && !strcmp(name, g->bridge)) {
            found = r;
            n++;
        }
    }
    if (n != 1)
        return NULL;
    if (uuid)
        *uuid = found->string;
    return found;
}

typedef struct {
    size_t n;
    const char *names[MAX_ROWS], *uuids[MAX_ROWS];
} named_rows;

static const char *find(const named_rows *rows, const char *name)
{
    for (size_t k = 0; k < rows->n; k++)
        if (!strcmp(rows->names[k], name))
            return rows->uuids[k];
    return NULL;
}

/* the bridge's ports by name (Port uuids) */
static void ports_of(const cJSON *tables, const cJSON *bridge, named_rows *out)
{
    out->n = 0;
    const char *uuids[MAX_ROWS];
    size_t n = bridge ? ovs_uuids(bridge, "ports", uuids, MAX_ROWS) : 0;
    const cJSON *ports = em_table(tables, "Port");
    for (size_t k = 0; k < n; k++) {
        const char *name = ovs_str(cJSON_GetObjectItemCaseSensitive(ports, uuids[k]), "name");
        if (name && out->n < MAX_ROWS) {
            out->names[out->n] = name;
            out->uuids[out->n++] = uuids[k];
        }
    }
}

/* the gre Wifi_Inet_Config rows on the AP, by if_name */
static void gre_rows(const em_gre_scope *g, const cJSON *tables, named_rows *out)
{
    out->n = 0;
    const cJSON *r;
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_Inet_Config"))
    {
        const char *type = ovs_str(r, "if_type"), *on = ovs_str(r, "gre_ifname"), *name = ovs_str(r, "if_name");
        if (type && !strcmp(type, "gre") && on && !strcmp(on, g->ap) && name && out->n < MAX_ROWS) {
            out->names[out->n] = name;
            out->uuids[out->n++] = r->string;
        }
    }
}

static const char *local_address(const em_gre_scope *g, const cJSON *tables)
{
    const cJSON *r;
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_Inet_State"))
    {
        const char *name = ovs_str(r, "if_name"), *address = ovs_str(r, "inet_addr");
        if (name && !strcmp(name, g->ap) && address && *address && strcmp(address, "0.0.0.0"))
            return address;
    }
    return NULL;
}

static int compare_address(const void *a, const void *b)
{
    uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b;
    return x < y ? -1 : x > y;
}

static bool same_mac(const char *a, const char *b)
{
    if (strlen(a) != strlen(b))
        return false;
    for (size_t k = 0; a[k]; k++) {
        char x = a[k] >= 'A' && a[k] <= 'Z' ? (char)(a[k] + 32) : a[k];
        char y = b[k] >= 'A' && b[k] <= 'Z' ? (char)(b[k] + 32) : b[k];
        if (x != y)
            return false;
    }
    return true;
}

/* GreParentBackend.child_addresses: the leases in the underlay whose MAC is associated to the
 * AP, sorted by address */
static void children_of(em_gre_scope *g, const cJSON *tables)
{
    const char *macs[MAX_ROWS];
    size_t nmacs = 0;
    const cJSON *vif, *lease, *clients = em_table(tables, "Wifi_Associated_Clients");
    cJSON_ArrayForEach(vif, em_table(tables, "Wifi_VIF_State"))
    {
        const char *name = ovs_str(vif, "if_name"), *mode = ovs_str(vif, "mode");
        if (!name || strcmp(name, g->ap) || !mode || strcmp(mode, "ap"))
            continue;
        const char *uuids[MAX_ROWS];
        size_t n = ovs_uuids(vif, "associated_clients", uuids, MAX_ROWS);
        for (size_t k = 0; k < n; k++) {
            const cJSON *client = cJSON_GetObjectItemCaseSensitive(clients, uuids[k]);
            const cJSON *state_column = cJSON_GetObjectItemCaseSensitive(client, "state");
            const char *state = ovs_str(client, "state"), *mac = ovs_str(client, "mac");
            bool active = state_column ? state && !strcmp(state, "active") : client != NULL;
            if (active && mac && *mac && nmacs < MAX_ROWS)
                macs[nmacs++] = mac;
        }
    }
    uint32_t found[EM_GRE_CHILDREN];
    size_t nfound = 0;
    cJSON_ArrayForEach(lease, em_table(tables, "DHCP_leased_IP"))
    {
        uint32_t a;
        const char *address = ovs_str(lease, "inet_addr"), *hw = ovs_str(lease, "hwaddr");
        if (!address || !ipv4(address, &a) || (a & g->mask) != g->net || a == g->net + 1 || !hw)
            continue;
        bool associated = false, seen = false;
        for (size_t k = 0; k < nmacs && !associated; k++)
            associated = same_mac(macs[k], hw);
        for (size_t k = 0; k < nfound && !seen; k++)
            seen = found[k] == a;
        if (associated && !seen && nfound < EM_GRE_CHILDREN)
            found[nfound++] = a;
    }
    qsort(found, nfound, sizeof(*found), compare_address);
    em_gre_intent *c = &g->children;
    memset(c, 0, sizeof(*c));
    EM_FORMAT_FIXED(c->pod_id, sizeof(c->pod_id), "%s", g->pod_id);
    EM_FORMAT_FIXED(c->ap, sizeof(c->ap), "%s", g->ap);
    EM_FORMAT_FIXED(c->bridge, sizeof(c->bridge), "%s", g->bridge);
    for (size_t k = 0; k < nfound; k++)
        ipv4_text(found[k], c->remotes[c->nremotes++]);
}

/* GreParentBackend._configured: the tunnels whose gre row and port are both there */
static cJSON *configured(const em_gre_scope *g, const cJSON *tables)
{
    named_rows *ports = em_malloc(sizeof(*ports)), *rows = em_malloc(sizeof(*rows));
    ports_of(tables, bridge_row(g, tables, NULL), ports);
    gre_rows(g, tables, rows);
    cJSON *c = cJSON_CreateObject();
    cJSON_AddStringToObject(c, "ap", g->ap);
    cJSON_AddStringToObject(c, "bridge", g->bridge);
    cJSON *tunnels = cJSON_AddObjectToObject(c, "tunnels");
    const cJSON *inet = em_table(tables, "Wifi_Inet_Config");
    for (size_t k = 0; k < rows->n; k++) {
        if (!find(ports, rows->names[k]))
            continue;
        const char *remote = ovs_str(cJSON_GetObjectItemCaseSensitive(inet, rows->uuids[k]), "gre_remote_inet_addr");
        cJSON_AddItemToObject(tunnels, rows->names[k], remote ? cJSON_CreateString(remote) : cJSON_CreateNull());
    }
    free(ports);
    free(rows);
    return c;
}

static const cJSON *bind(em_gre_scope *g, const char **node_uuid)
{
    const cJSON *tables = em_ovsdb_tables(g->ovs);
    const cJSON *node = em_bound_node(tables, g->serial, node_uuid);
    if (!node)
        return NULL;
    g->has_instance = em_start_instance(tables, g->instance);
    return g->has_instance ? node : NULL;
}

static bool snapshot(void *ctx, em_snapshot *out)
{
    em_gre_scope *g = ctx;
    memset(out, 0, sizeof(*out));
    const char *node_uuid;
    const cJSON *node = em_ovsdb_ready(g->ovs) ? bind(g, &node_uuid) : NULL;
    if (!node) {
        if (!g->has_last)
            return false;
        out->config = cJSON_Duplicate(g->last.config, true);
        out->observed = cJSON_Duplicate(g->last.observed, true);
        cJSON_ReplaceItemInObjectCaseSensitive(out->observed, "fresh", cJSON_CreateFalse());
        out->generation = g->last.generation;
        memcpy(out->schema_fingerprint, g->last.schema_fingerprint, 65);
        return true;
    }
    const cJSON *tables = em_ovsdb_tables(g->ovs);
    out->config = configured(g, tables);
    children_of(g, tables);
    /* nm's own table: a tunnel's interface is up once it has a State row */
    cJSON *observed = cJSON_Duplicate(out->config, true), *tunnels = cJSON_GetObjectItemCaseSensitive(observed, "tunnels");
    for (cJSON *t = tunnels ? tunnels->child : NULL; t;) {
        cJSON *next = t->next;
        bool up = false;
        const cJSON *r;
        cJSON_ArrayForEach(r, em_table(tables, "Wifi_Inet_State"))
        {
            const char *name = ovs_str(r, "if_name");
            up = up || (name && !strcmp(name, t->string));
        }
        if (!up)
            cJSON_DeleteItemFromObjectCaseSensitive(tunnels, t->string);
        t = next;
    }
    out->ready = true;
    out->generation = em_ovsdb_generation(g->ovs);
    EM_FORMAT_FIXED(out->schema_fingerprint, 65, "%s", em_ovsdb_schema_fingerprint(g->ovs)); /* SHA-256 hex */
    out->observed = em_observation(g->pod_id, "gre-parent", observed, MODE, out->generation, true, PROVENANCE,
                                   em_ovsdb_revision(g->ovs));
    em_snapshot_clear(&g->last);
    g->last.config = cJSON_Duplicate(out->config, true);
    g->last.observed = cJSON_Duplicate(out->observed, true);
    g->last.generation = out->generation;
    memcpy(g->last.schema_fingerprint, out->schema_fingerprint, 65);
    g->has_last = true;
    return true;
}

/* GreParentBackend._check */
static em_reason check(const em_gre_scope *g, const em_gre_intent *i)
{
    if (strcmp(i->pod_id, g->pod_id) || strcmp(i->ap, g->ap) || strcmp(i->bridge, g->bridge))
        return EM_UNSUPPORTED_OPERATION; /* the request exceeds the bound parent AP */
    for (size_t k = 0; k < i->nremotes; k++) {
        uint32_t a;
        if (!ipv4(i->remotes[k], &a) || (a & g->mask) != g->net)
            return EM_INVALID_INPUT; /* a child address outside the underlay */
    }
    return EM_OK;
}

static cJSON *plan_of(em_gre_scope *g, const em_gre_intent *i, em_reason *why)
{
    if ((*why = check(g, i)) != EM_OK)
        return NULL;
    const char *node_uuid;
    if (!em_ovsdb_ready(g->ovs) || !bind(g, &node_uuid)) {
        *why = EM_NOT_READY;
        return NULL;
    }
    const cJSON *tables = em_ovsdb_tables(g->ovs);
    if (!bridge_row(g, tables, NULL) || (i->nremotes && !local_address(g, tables))) {
        *why = EM_NOT_READY; /* no bridge, or the parent AP has no address yet */
        return NULL;
    }
    cJSON *p = cJSON_CreateObject();
    cJSON_AddStringToObject(p, "mapping", MODE);
    cJSON_AddStringToObject(p, "action", "gre-parent-tunnels");
    cJSON_AddStringToObject(p, "ap", i->ap);
    const char *names[EM_GRE_CHILDREN];
    char buf[EM_GRE_CHILDREN][16];
    for (size_t k = 0; k < i->nremotes; k++) {
        (void)tunnel_name(i->remotes[k], buf[k]); /* validated */
        names[k] = buf[k];
    }
    qsort(names, i->nremotes, sizeof(*names), compare_text);
    cJSON_AddItemToObject(p, "tunnels", em_ovs_strings(names, i->nremotes));
    cJSON_AddStringToObject(p, "instance", g->instance);
    const char *fields[] = {"Wifi_Inet_Config gre rows", "Interface", "Port", "Bridge.ports"};
    cJSON_AddItemToObject(p, "fields", em_ovs_strings(fields, 4));
    cJSON_AddStringToObject(p, "guard", "pod serial, the bridge's ports and each changed gre row");
    return p;
}

static cJSON *plan(void *ctx, const cJSON *intent, em_reason *why)
{
    em_gre_intent *i = em_malloc(sizeof(*i));
    cJSON *p = NULL;
    if (!from_record(intent, i))
        *why = EM_INVALID_INPUT;
    else
        p = plan_of(ctx, i, why);
    free(i);
    return p;
}

static cJSON *wait_rows(const char *table, const char *uuid, const char *const *columns, size_t n, cJSON *row)
{
    cJSON *o = em_ovs_op("wait", table, em_ovs_where_uuid(uuid));
    cJSON_AddItemToObject(o, "columns", em_ovs_strings(columns, n));
    cJSON_AddStringToObject(o, "until", "==");
    cJSON_AddItemToArray(cJSON_AddArrayToObject(o, "rows"), row);
    cJSON_AddNumberToObject(o, "timeout", 0);
    return o;
}

static cJSON *uuid_atom(const char *kind, const char *value)
{
    cJSON *a = cJSON_CreateArray();
    cJSON_AddItemToArray(a, cJSON_CreateString(kind));
    cJSON_AddItemToArray(a, cJSON_CreateString(value));
    return a;
}

/* ["set", [[kind, value]]] */
static cJSON *one_set(const char *kind, const char *value)
{
    cJSON *s = cJSON_CreateArray(), *items = cJSON_CreateArray();
    cJSON_AddItemToArray(items, uuid_atom(kind, value));
    cJSON_AddItemToArray(s, cJSON_CreateString("set"));
    cJSON_AddItemToArray(s, items);
    return s;
}

static cJSON *ports_mutate(const char *bridge_uuid, const char *mutator, const char *kind, const char *value)
{
    cJSON *o = em_ovs_op("mutate", "Bridge", em_ovs_where_uuid(bridge_uuid)), *m = cJSON_CreateArray();
    cJSON *mutation = cJSON_CreateArray();
    cJSON_AddItemToArray(mutation, cJSON_CreateString("ports"));
    cJSON_AddItemToArray(mutation, cJSON_CreateString(mutator));
    cJSON_AddItemToArray(mutation, one_set(kind, value));
    cJSON_AddItemToArray(m, mutation);
    cJSON_AddItemToObject(o, "mutations", m);
    return o;
}

/* the guarded columns a row has (GreParentBackend: [c for c in INET_GUARDS if c in row]) */
static cJSON *inet_guard(const char *uuid, const cJSON *row)
{
    const char *columns[5];
    size_t n = 0;
    for (size_t k = 0; k < 5; k++)
        if (cJSON_GetObjectItemCaseSensitive(row, INET_GUARDS[k]))
            columns[n++] = INET_GUARDS[k];
    return em_ovs_guard("Wifi_Inet_Config", uuid, row, columns, n);
}

#define PUSH(o, n) (cJSON_AddItemToArray(ops, (o)), counts[nops++] = (n))

/* GreParentBackend._transaction */
static cJSON *transaction(em_gre_scope *g, const em_gre_intent *i, const char *node_uuid, int **counts_out,
                          size_t *nops_out)
{
    const cJSON *tables = em_ovsdb_tables(g->ovs), *inet = em_table(tables, "Wifi_Inet_Config");
    const char *bridge_uuid;
    const cJSON *bridge = bridge_row(g, tables, &bridge_uuid);
    const char *local = local_address(g, tables);
    named_rows *ports = em_malloc(sizeof(*ports)), *rows = em_malloc(sizeof(*rows));
    ports_of(tables, bridge, ports);
    gre_rows(g, tables, rows);
    cJSON *target = target_of(i), *wanted = cJSON_GetObjectItemCaseSensitive(target, "tunnels");
    int *counts = em_malloc(sizeof(int) * (2 + 3 * (ports->n + rows->n) + 6 * i->nremotes));
    size_t nops = 0;
    cJSON *ops = cJSON_CreateArray();
    /* the pod; the bridge's ports as they are now */
    const char *node_cols[] = {"serial_number"};
    cJSON *serial = cJSON_CreateObject();
    cJSON_AddStringToObject(serial, "serial_number", g->serial);
    PUSH(wait_rows("AWLAN_Node", node_uuid, node_cols, 1, serial), -1);
    const char *bridge_cols[] = {"name", "ports"};
    cJSON *now = cJSON_CreateObject(), *set = cJSON_CreateArray(), *items = cJSON_CreateArray();
    const char *uuids[MAX_ROWS];
    size_t n = ovs_uuids(bridge, "ports", uuids, MAX_ROWS);
    for (size_t k = 0; k < n; k++)
        cJSON_AddItemToArray(items, uuid_atom("uuid", uuids[k]));
    cJSON_AddItemToArray(set, cJSON_CreateString("set"));
    cJSON_AddItemToArray(set, items);
    cJSON_AddStringToObject(now, "name", ovs_str(bridge, "name"));
    cJSON_AddItemToObject(now, "ports", set);
    PUSH(wait_rows("Bridge", bridge_uuid, bridge_cols, 2, now), -1);
    /* a child gone: its gre row and its port (the names in either, sorted) */
    const char **names = em_malloc(sizeof(char *) * (ports->n + rows->n + 1));
    size_t nnames = 0;
    for (size_t k = 0; k < rows->n; k++)
        names[nnames++] = rows->names[k];
    for (size_t k = 0; k < ports->n; k++)
        if (!find(rows, ports->names[k]))
            names[nnames++] = ports->names[k];
    qsort(names, nnames, sizeof(*names), compare_text);
    for (size_t k = 0; k < nnames; k++) {
        const char *name = names[k];
        if (cJSON_GetObjectItemCaseSensitive(wanted, name) || strncmp(name, "pgd", 3))
            continue;
        const char *row_uuid = find(rows, name), *port_uuid = find(ports, name);
        if (row_uuid) {
            PUSH(inet_guard(row_uuid, cJSON_GetObjectItemCaseSensitive(inet, row_uuid)), -1);
            PUSH(em_ovs_op("delete", "Wifi_Inet_Config", em_ovs_where_uuid(row_uuid)), 1);
            if (port_uuid)
                PUSH(ports_mutate(bridge_uuid, "delete", "uuid", port_uuid), 1);
        }
    }
    free((void *)names);
    /* each child's tunnel, by name */
    const cJSON *t;
    size_t index = 0;
    const char **wanted_names = em_malloc(sizeof(char *) * (i->nremotes + 1));
    size_t nwanted = 0;
    cJSON_ArrayForEach(t, wanted)
    wanted_names[nwanted++] = t->string;
    qsort(wanted_names, nwanted, sizeof(*wanted_names), compare_text);
    for (size_t k = 0; k < nwanted; k++, index++) {
        const char *name = wanted_names[k], *remote = str(wanted, name);
        cJSON *want = cJSON_CreateObject();
        cJSON_AddStringToObject(want, "if_name", name);
        cJSON_AddStringToObject(want, "if_type", "gre");
        cJSON_AddTrueToObject(want, "enabled");
        cJSON_AddTrueToObject(want, "network");
        cJSON_AddNumberToObject(want, "mtu", TUNNEL_MTU);
        cJSON_AddStringToObject(want, "ip_assign_scheme", "none");
        cJSON_AddStringToObject(want, "gre_ifname", g->ap);
        cJSON_AddStringToObject(want, "gre_local_inet_addr", local ? local : "");
        cJSON_AddStringToObject(want, "gre_remote_inet_addr", remote);
        const char *row_uuid = find(rows, name);
        if (!row_uuid) {
            PUSH(em_ovs_absent("Wifi_Inet_Config", em_ovs_where_eq("if_name", name), "if_name"), -1);
            cJSON *insert = em_ovs_op("insert", "Wifi_Inet_Config", NULL);
            cJSON_AddItemToObject(insert, "row", want);
            PUSH(insert, -1);
        } else {
            const cJSON *row = cJSON_GetObjectItemCaseSensitive(inet, row_uuid), *w;
            bool differs = false;
            cJSON_ArrayForEach(w, want)
            {
                const cJSON *have = cJSON_GetObjectItemCaseSensitive(row, w->string);
                differs = differs || !have || !cJSON_Compare(have, w, true);
            }
            if (differs) {
                PUSH(inet_guard(row_uuid, row), -1);
                cJSON *update = em_ovs_op("update", "Wifi_Inet_Config", em_ovs_where_uuid(row_uuid));
                cJSON_DeleteItemFromObjectCaseSensitive(want, "if_name");
                cJSON_AddItemToObject(update, "row", want);
                PUSH(update, 1);
            } else {
                cJSON_Delete(want);
            }
        }
        if (!find(ports, name)) {
            char interface[16], port[16];
            EM_FORMAT_FIXED(interface, sizeof(interface), "i%zu", index);
            EM_FORMAT_FIXED(port, sizeof(port), "p%zu", index);
            PUSH(em_ovs_absent("Port", em_ovs_where_eq("name", name), "name"), -1);
            cJSON *insert = em_ovs_op("insert", "Interface", NULL), *row = cJSON_CreateObject();
            cJSON_AddStringToObject(insert, "uuid-name", interface);
            cJSON_AddStringToObject(row, "name", name);
            cJSON_AddItemToObject(insert, "row", row);
            PUSH(insert, -1);
            insert = em_ovs_op("insert", "Port", NULL);
            row = cJSON_CreateObject();
            cJSON_AddStringToObject(insert, "uuid-name", port);
            cJSON_AddStringToObject(row, "name", name);
            cJSON_AddItemToObject(row, "interfaces", uuid_atom("named-uuid", interface));
            cJSON_AddItemToObject(insert, "row", row);
            PUSH(insert, -1);
            PUSH(ports_mutate(bridge_uuid, "insert", "named-uuid", port), 1);
        }
    }
    free((void *)wanted_names);
    cJSON_Delete(target);
    free(ports);
    free(rows);
    *counts_out = counts;
    *nops_out = nops;
    return ops;
}

static void submit(void *ctx, const cJSON *intent, const cJSON *attempt, em_submit_out *out)
{
    em_gre_scope *g = ctx;
    memset(out, 0, sizeof(*out));
    em_gre_intent *i = em_malloc(sizeof(*i));
    em_reason why;
    cJSON *p = from_record(intent, i) ? plan_of(g, i, &why) : NULL;
    if (!p) {
        free(i);
        strcpy(out->status, "unknown"); /* the plan raised at submission */
        out->reason = EM_OUTCOME_UNKNOWN;
        return;
    }
    cJSON_Delete(p);
    int generation = 0; /* none or no int: another session */
    (void)em_json_int(cJSON_GetObjectItemCaseSensitive(attempt, "session_generation"), &generation);
    const char *node_uuid;
    if (!bind(g, &node_uuid) || em_ovsdb_generation(g->ovs) != generation) {
        free(i);
        strcpy(out->status, "rejected");
        out->reason = EM_NOT_READY;
        return;
    }
    int *counts;
    size_t nops;
    cJSON *ops = transaction(g, i, node_uuid, &counts, &nops);
    free(i);
    cJSON *results = g->transact(g->transact_ctx, ops);
    cJSON_Delete(ops);
    if (!results) {
        free(counts);
        strcpy(out->status, "unknown");
        out->reason = EM_OUTCOME_UNKNOWN;
        out->evidence = cJSON_CreateObject();
        cJSON_AddStringToObject(out->evidence, "attribution", "unknown");
        return;
    }
    em_reason checked = em_check_results(results, counts, nops);
    free(counts);
    if (checked != EM_OK) {
        cJSON_Delete(results);
        EM_FORMAT_FIXED(out->status, sizeof(out->status), "%s",
                        checked == EM_PRECONDITION_FAILED ? "conflict" : checked == EM_OUTCOME_UNKNOWN ? "unknown" : "rejected");
        out->reason = checked;
        return;
    }
    strcpy(out->status, "committed");
    out->evidence = cJSON_CreateObject();
    cJSON_AddStringToObject(out->evidence, "attribution", "reply");
    cJSON_AddTrueToObject(out->evidence, "transaction_validated");
    cJSON_AddStringToObject(out->evidence, "transaction_id", str(attempt, "transaction_id"));
    cJSON_AddNumberToObject(out->evidence, "session_generation", generation);
    cJSON_AddStringToObject(out->evidence, "action", "gre-parent-tunnels");
    cJSON_AddStringToObject(out->evidence, "instance", g->instance);
    cJSON_AddItemToObject(out->evidence, "results", results);
}

/* -- GreTunnels ------------------------------------------------------------------------ */

bool em_gre_bind(em_gre_scope *g, const char *pod_id, const char *ap, const char *bridge, const char *underlay)
{
    if (!em_copy(g->pod_id, sizeof(g->pod_id), pod_id) || !interface_name(ap) || !interface_name(bridge) ||
        !em_copy(g->ap, sizeof(g->ap), ap) || !em_copy(g->bridge, sizeof(g->bridge), bridge) ||
        !em_copy(g->underlay, sizeof(g->underlay), underlay ? underlay : ""))
        return false;
    char address[16];
    const char *slash = strchr(g->underlay, '/');
    long len = slash ? strtol(slash + 1, NULL, 10) : -1;
    uint32_t a;
    if (!slash || slash[1] < '0' || slash[1] > '9' || len > 32 ||
        !em_format(address, sizeof(address), "%.*s", (int)(slash - g->underlay), g->underlay) || !ipv4(address, &a))
        return false;
    g->mask = len ? 0xffffffffu << (32 - len) : 0;
    g->net = a & g->mask;
    return true;
}

em_reason em_gre_open(em_gre_scope *g, const char *state_dir, const em_journal_schemas *schemas,
                      const em_vault *vault, double (*monotonic)(void))
{
    char dir[600];
    EM_FORMAT_FIXED(dir, sizeof(dir), "%s/gre-parent", state_dir); /* EM_STATE_DIR_MAX */
    em_reason why;
    g->journal = em_journal_open(dir, schemas, &why);
    if (!g->journal)
        return why;
    em_engine_init(&g->engine, g->journal, vault, g->pod_id, em_gre_backend(), g, monotonic);
    em_engine_recover(&g->engine);
    return EM_OK;
}

void em_gre_close(em_gre_scope *g)
{
    em_engine_free(&g->engine);
    em_journal_close(g->journal);
    em_snapshot_clear(&g->last);
}

static void wait_for(em_gre_scope *g, const char *why)
{
    g->has_waiting = why != NULL;
    (void)em_copy(g->waiting, sizeof(g->waiting), why ? why : ""); /* a message */
}

static void settle(em_gre_scope *g, cJSON *op, const em_snapshot *snap, bool have_snap)
{
    const char *state = em_state_of(op);
    if (strcmp(state, "CONFIG_COMMITTED") && strcmp(state, "INDETERMINATE"))
        return;
    em_reason why;
    cJSON *t = target(g, cJSON_GetObjectItemCaseSensitive(op, "intent"), &why);
    const char *planned = str(cJSON_GetObjectItemCaseSensitive(op, "plan"), "instance");
    bool same_start = have_snap && g->has_instance && planned && !strcmp(planned, g->instance);
    if (t && same_start && snap->ready && em_snapshot_satisfies(snap, t) && em_matches(snap->config, t)) {
        cJSON *evidence = em_engine_evidence(snap, false, g->instance);
        if (!strcmp(state, "INDETERMINATE"))
            cJSON_AddStringToObject(evidence, "attribution", "current_condition_only");
        if (em_transition(op, "OBSERVED_APPLIED", evidence)) {
            cJSON_ReplaceItemInObjectCaseSensitive(op, "application_evidence", evidence);
            em_set_reason(op, NULL);
            em_engine_save(&g->engine, op, NULL);
            em_log(EM_LOG_INFO, "emosa.agent.gre_parent", "the parent AP %s's tunnels: %zu", g->ap,
                   (size_t)cJSON_GetArraySize(cJSON_GetObjectItemCaseSensitive(t, "tunnels")));
        } else {
            cJSON_Delete(evidence);
        }
    } else if (em_engine_expired(&g->engine, op)) {
        cJSON_ReplaceItemInObjectCaseSensitive(op, "original_outcome", cJSON_CreateString(state));
        cJSON_ReplaceItemInObjectCaseSensitive(op, "deadline_elapsed", cJSON_CreateTrue());
        em_transition(op, "TIMED_OUT", NULL);
        em_set_reason(op, "APPLY_TIMEOUT");
        em_engine_save(&g->engine, op, NULL);
    }
    cJSON_Delete(t);
}

/* GreTunnels.key: one write per OpenSync start and set of children */
static char *key_of(const em_gre_scope *g)
{
    size_t size = sizeof(g->instance) + 1 + EM_GRE_CHILDREN * 16;
    char *key = em_malloc(size), *at = key;
    at += snprintf(at, size, "%s:", g->instance);
    for (size_t k = 0; k < g->children.nremotes; k++)
        at += snprintf(at, size - (size_t)(at - key), "%s%s", k ? "," : "", g->children.remotes[k]);
    return key;
}

void em_gre_tick(em_gre_scope *g)
{
    em_snapshot snap = {0};
    bool have = g->engine.backend.snapshot(g, &snap);
    cJSON *op = em_journal_latest(g->journal);
    if (op)
        settle(g, op, &snap, have);
    cJSON_Delete(op);
    if (!have) {
        em_snapshot_clear(&snap);
        return;
    }
    /* GreTunnels._wanted */
    op = em_journal_latest(g->journal);
    cJSON *owned = em_journal_ownership(g->journal, g->pod_id), *t = target_of(&g->children), *done = NULL;
    char *key = key_of(g);
    bool wanted = false;
    if (!snap.ready) {
        wait_for(g, "pod not bound");
    } else if (owned) {
        wait_for(g, "another manager changed the tunnels: admit the pod anew");
    } else if (op && em_state_active(em_state_of(op))) {
        wait_for(g, "write in progress");
    } else if (cJSON_Compare(cJSON_GetObjectItemCaseSensitive(snap.config, "tunnels"),
                             cJSON_GetObjectItemCaseSensitive(t, "tunnels"), true)) {
        wait_for(g, NULL);
    } else if ((done = em_journal_lookup(g->journal, SOURCE, g->pod_id, key))) {
        if (!strcmp(em_state_of(done), "OBSERVED_APPLIED")) {
            wait_for(g, NULL);
        } else {
            char text[128];
            const char *reason = str(done, "reason");
            (void)em_format(text, sizeof(text), "not made for these children: %s%s%s", em_state_of(done), /* a message */
                            reason ? " " : "", reason ? reason : "");
            wait_for(g, text);
        }
    } else {
        wait_for(g, NULL);
        wanted = true;
    }
    cJSON_Delete(done);
    cJSON_Delete(owned);
    cJSON_Delete(op);
    cJSON_Delete(t);
    em_snapshot_clear(&snap);
    if (wanted) {
        cJSON *intent = record(&g->children);
        em_reason why;
        op = em_engine_request(&g->engine, intent, SOURCE, key, g->run_id, DEADLINE, "semantic", NULL, &why);
        cJSON_Delete(intent);
        if (op && !strcmp(em_state_of(op), "REQUESTED")) {
            cJSON *result = em_engine_execute(&g->engine,
                                              cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(op, "operation_id")));
            const char *reason = str(result, "reason");
            em_log(EM_LOG_INFO, "emosa.agent.gre_parent", "gre parent %s: %s %s", str(result, "operation_id"),
                   em_state_of(result), reason ? reason : "");
            cJSON_Delete(result);
        }
        cJSON_Delete(op);
    }
    free(key);
}

cJSON *em_gre_status(em_gre_scope *g)
{
    cJSON *o = cJSON_CreateObject(), *op = em_journal_latest(g->journal);
    cJSON_AddStringToObject(o, "ap", g->ap);
    char net[16], underlay[24];
    ipv4_text(g->net, net);
    int len = 0;
    for (uint32_t m = g->mask; m; m <<= 1)
        len++;
    EM_FORMAT_FIXED(underlay, sizeof(underlay), "%s/%d", net, len);
    cJSON_AddStringToObject(o, "underlay", underlay);
    cJSON *children = cJSON_AddArrayToObject(o, "children");
    for (size_t k = 0; k < g->children.nremotes; k++)
        cJSON_AddItemToArray(children, cJSON_CreateString(g->children.remotes[k]));
    const cJSON *tunnels = g->has_last ? cJSON_GetObjectItemCaseSensitive(g->last.config, "tunnels") : NULL;
    cJSON_AddItemToObject(o, "tunnels", tunnels ? cJSON_Duplicate(tunnels, true) : cJSON_CreateNull());
    cJSON_AddItemToObject(o, "waiting", g->has_waiting ? cJSON_CreateString(g->waiting) : cJSON_CreateNull());
    if (op) {
        cJSON *x = cJSON_AddObjectToObject(o, "operation");
        cJSON_AddItemToObject(x, "operation_id", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "operation_id"), 1));
        cJSON_AddItemToObject(x, "state", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "state"), 1));
        cJSON_AddItemToObject(x, "reason", cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(op, "reason"), 1));
        const char *instance = str(cJSON_GetObjectItemCaseSensitive(op, "plan"), "instance");
        cJSON_AddItemToObject(x, "instance", instance ? cJSON_CreateString(instance) : cJSON_CreateNull());
    } else {
        cJSON_AddNullToObject(o, "operation");
    }
    cJSON_Delete(op);
    return o;
}

em_backend em_gre_backend(void) { return (em_backend){MODE, target, snapshot, plan, submit}; }
