/* The fleet (spec §4). Mirrors emosa.agent.fleet. */
#include "fleet.h"

#include <openssl/sha.h>
#include <stdio.h>
#include <string.h>

bool em_derive_al(const char *serial, const char *const *taken, size_t ntaken, char out[18])
{
    for (int n = 0; n < 256; n++) {
        char seed[160];
        uint8_t digest[32];
        int len = snprintf(seed, sizeof(seed), "emosa-agent-al:%s:%d", serial, n);
        if (len < 0 || (size_t)len >= sizeof(seed))
            return false;
        SHA256((const uint8_t *)seed, (size_t)len, digest);
        snprintf(out, 18, "02:%02x:%02x:%02x:%02x:%02x", digest[0], digest[1], digest[2],
                 digest[3], digest[4]);
        bool used = false;
        for (size_t i = 0; i < ntaken && !used; i++)
            used = !strcmp(taken[i], out);
        if (!used)
            return true;
    }
    return false;
}

em_fleet_entry *em_registry_assign(em_registry *r, const char *serial, const char *node_id,
                                   const char *model, const char *firmware, double now)
{
    em_fleet_entry *e = NULL;
    for (size_t i = 0; i < r->count && !e; i++)
        if (!strcmp(r->entries[i].pod_id, serial))
            e = &r->entries[i];
    if (!e) {
        int port = 0;
        for (int p = r->port_low; p <= r->port_high && !port; p++) {
            bool used = false;
            for (size_t i = 0; i < r->count && !used; i++)
                used = r->entries[i].port == p;
            if (!used)
                port = p;
        }
        if (!port || r->count == 256 || strlen(serial) > 64)
            return NULL;
        const char *taken[257];
        size_t ntaken = 0;
        taken[ntaken++] = r->reserved_al;
        for (size_t i = 0; i < r->count; i++)
            taken[ntaken++] = r->entries[i].al_mac;
        e = &r->entries[r->count];
        memset(e, 0, sizeof(*e));
        if (!em_derive_al(serial, taken, ntaken, e->al_mac))
            return NULL;
        r->count++;
        snprintf(e->pod_id, sizeof(e->pod_id), "%s", serial);
        e->port = port;
        snprintf(e->interface, sizeof(e->interface), "em%d", port - r->port_low + 1);
        e->first_seen = now;
    }
    snprintf(e->node_id, sizeof(e->node_id), "%s", node_id ? node_id : "");
    snprintf(e->model, sizeof(e->model), "%s", model ? model : "");
    snprintf(e->firmware, sizeof(e->firmware), "%s", firmware ? firmware : "");
    e->last_seen = now;
    e->handovers++;
    return e;
}

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
    char text[160];
    cJSON_AddStringToObject(c, "pod_id", e->pod_id);
    cJSON_AddStringToObject(c, "serial", e->pod_id);
    snprintf(text, sizeof(text), "ptcp:%d:127.0.0.1", e->port);
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
    snprintf(text, sizeof(text), "%s/%s", root ? root : "", e->pod_id);
    cJSON_AddStringToObject(c, "state_dir", text);
    cJSON_AddStringToObject(c, "run_id", e->pod_id);
    return c;
}

cJSON *em_manager_update(const em_fleet_entry *e, const cJSON *fleet)
{
    char target[128];
    snprintf(target, sizeof(target), "tcp:%s:%d",
             cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(fleet, "advertise")), e->port);
    cJSON *o = cJSON_CreateObject(), *row = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "op", "update");
    cJSON_AddStringToObject(o, "table", "AWLAN_Node");
    cJSON_AddItemToObject(o, "where", cJSON_CreateArray());
    cJSON_AddStringToObject(row, "manager_addr", target);
    cJSON_AddItemToObject(o, "row", row);
    return o;
}
