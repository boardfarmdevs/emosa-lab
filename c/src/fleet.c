/* SPDX-License-Identifier: Apache-2.0 */
/* The fleet (spec §4). Mirrors emosa.agent.fleet. */
#include "fleet.h"

#include "canon.h"

#include <errno.h>
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

static void set_identity(em_fleet_entry *e, const char *node_id, const char *model, const char *firmware)
{
    em_fleet_entry_clear(e);
    e->node_id = dup_or_null(node_id);
    e->model = dup_or_null(model);
    e->firmware = dup_or_null(firmware);
}

em_fleet_entry *em_registry_assign(em_registry *r, const char *serial, const char *node_id,
                                   const char *model, const char *firmware, double now)
{
    em_fleet_entry *e = find(r, serial);
    if (!e) {
        if (strlen(serial) > 64)
            return NULL;
        int port = 0;
        for (int p = r->port_low; p <= r->port_high && !port; p++) {
            bool used = false;
            for (size_t i = 0; i < r->count && !used; i++)
                used = r->entries[i].port == p;
            if (!used)
                port = p;
        }
        if (!port)
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
        e = append(r);
        EM_FORMAT_FIXED(e->pod_id, sizeof(e->pod_id), "%s", serial); /* at most 64, checked above */
        memcpy(e->al_mac, al, sizeof(al));
        e->port = port;
        EM_FORMAT_FIXED(e->interface, sizeof(e->interface), "em%d", port - r->port_low + 1);
        e->first_seen = now;
    }
    set_identity(e, node_id, model, firmware);
    e->last_seen = now;
    e->handovers++;
    return e;
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

/* fleet[key] (or pods.<serial>[key]) with a default */
static cJSON *setting(const cJSON *fleet, const char *serial, const char *key, cJSON *fallback)
{
    const cJSON *own = cJSON_GetObjectItemCaseSensitive(
        cJSON_GetObjectItemCaseSensitive(fleet, "pods"), serial);
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(own, key);
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
    cJSON_AddItemToObject(c, "message_set", setting(fleet, e->pod_id, "message_set", cJSON_CreateString("easymesh-6.1")));
    cJSON_AddItemToObject(c, "multi_bss", setting(fleet, e->pod_id, "multi_bss", cJSON_CreateFalse()));
    cJSON_AddItemToObject(c, "m2_session", setting(fleet, e->pod_id, "m2_session", cJSON_CreateString("distinct")));
    cJSON_AddItemToObject(c, "profile", setting(fleet, e->pod_id, "profile", cJSON_CreateString("opensync-lab-hwsim-6.6.1-v1")));
    cJSON_AddItemToObject(c, "uplink", setting(fleet, e->pod_id, "uplink", mode_off()));
    cJSON_AddItemToObject(c, "telemetry", setting(fleet, e->pod_id, "telemetry", mode_off()));
    const char *root = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(fleet, "state_root"));
    /* a state directory that does not fit is left out, and the agent refuses its
     * configuration, rather than given a cut path, which would be another directory */
    if (em_format(text, sizeof(text), "%s/%s", root ? root : "", e->pod_id))
        cJSON_AddStringToObject(c, "state_dir", text);
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

static bool admitted(const cJSON *config, const char *serial)
{
    const cJSON *admit = cJSON_GetObjectItemCaseSensitive(config, "admit"), *s;
    if (!cJSON_IsArray(admit))
        return true; /* "*" or absent */
    cJSON_ArrayForEach(s, admit)
    {
        if (cJSON_IsString(s) && !strcmp(s->valuestring, serial))
            return true;
    }
    return false;
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
    memset(out, 0, sizeof(*out));
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
    if (!admitted(f->config, serial)) {
        EM_FORMAT_FIXED(out->why, sizeof(out->why), "pod %s not admitted: left unchanged", serial);
        return EM_FLEET_REFUSED;
    }
    if (!reload(f)) {
        EM_FORMAT_FIXED(out->why, sizeof(out->why), "pod %s: the fleet registry is unreadable", serial);
        return EM_FLEET_FAILED;
    }
    em_fleet_entry *e = em_registry_assign(&f->registry, serial, row_text(row, "id"), row_text(row, "model"),
                                           row_text(row, "firmware_version"), now);
    if (!e) {
        EM_FORMAT_FIXED(out->why, sizeof(out->why), "pod %s: no free agent port, left unchanged", serial);
        return EM_FLEET_REFUSED;
    }
    out->entry = *e;
    out->entry.node_id = out->entry.model = out->entry.firmware = NULL;
    char *registry = em_registry_dump(&f->registry);
    bool saved = registry && em_write_file(f->registry_path, registry, true);
    free(registry);
    char path[600];
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

bool em_fleet_update_ok(const cJSON *result)
{
    const cJSON *first = cJSON_GetArrayItem(result, 0);
    const cJSON *error = cJSON_GetObjectItemCaseSensitive(first, "error");
    const cJSON *count = cJSON_GetObjectItemCaseSensitive(first, "count");
    return cJSON_IsObject(first) && (!error || cJSON_IsNull(error) || cJSON_IsFalse(error)) &&
           cJSON_IsNumber(count) && count->valuedouble == 1;
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
        !rename(path, archive))
        cJSON_AddStringToObject(out, "archived_state", archive);
    em_fleet_entry_clear(&e);
    return out;
}
