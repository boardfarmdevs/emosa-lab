/* The pod scopes' OVSDB writes. Mirrors emosa.opensync.pod_profile (AP scope) and
 * emosa.opensync.steering (client steering scope): the same guards, operations
 * and order, so the transactions equal the reference's (translation-southbound.json,
 * steering.json). */
#include "southbound.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ovs.h"
#include "view.h"

/* -- profile ------------------------------------------------------------------------ */

em_reason em_profile_load(const char *path, em_profile *out)
{
    memset(out, 0, sizeof(*out));
    FILE *f = fopen(path, "rb");
    if (!f)
        return EM_NOT_READY;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *text = malloc((size_t)n + 1);
    size_t got = text ? fread(text, 1, (size_t)n, f) : 0;
    fclose(f);
    if (!text || got != (size_t)n) {
        free(text);
        return EM_NOT_READY;
    }
    text[n] = 0;
    out->doc = cJSON_Parse(text);
    free(text);
    const cJSON *d = out->doc, *radio = cJSON_GetObjectItemCaseSensitive(d, "radio"),
                *front = cJSON_GetObjectItemCaseSensitive(d, "fronthaul"), *slot;
    if (!d || !radio || !front)
        goto invalid;
    out->id = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(d, "id"));
    out->band = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(radio, "band"));
    out->ht_mode = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(radio, "ht_mode"));
    out->channel = cJSON_GetObjectItemCaseSensitive(radio, "channel")->valueint;
    out->fronthaul_if = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(front, "if_name"));
    out->fronthaul_vif = cJSON_GetObjectItemCaseSensitive(front, "vif");
    out->backhaul_vif = cJSON_GetObjectItemCaseSensitive(d, "backhaul_vif");
    out->inet = cJSON_GetObjectItemCaseSensitive(d, "inet");
    out->uplink_station = cJSON_GetStringValue(
        cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(d, "uplink"), "station"));
    cJSON_ArrayForEach(slot, cJSON_GetObjectItemCaseSensitive(d, "extra_slots"))
    {
        if (out->nslots == 8)
            goto invalid;
        out->slots[out->nslots].if_name = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(slot, "if_name"));
        out->slots[out->nslots].role = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(slot, "role"));
        out->slots[out->nslots].vif_radio_idx = cJSON_GetObjectItemCaseSensitive(slot, "vif_radio_idx")->valueint;
        out->nslots++;
    }
    if (!out->id || !out->band || !out->ht_mode || !out->fronthaul_if || !out->fronthaul_vif ||
        !out->backhaul_vif || !out->inet)
        goto invalid;
    return EM_OK;
invalid:
    em_profile_free(out);
    return EM_INVALID_INPUT;
}

void em_profile_free(em_profile *p)
{
    cJSON_Delete(p->doc);
    memset(p, 0, sizeof(*p));
}

/* -- transaction building ----------------------------------------------------------- */

static cJSON *uuid_ref(const char *uuid)
{
    cJSON *a = cJSON_CreateArray();
    cJSON_AddItemToArray(a, cJSON_CreateString("uuid"));
    cJSON_AddItemToArray(a, cJSON_CreateString(uuid));
    return a;
}

static cJSON *clause(const char *column, const char *fn, cJSON *value)
{
    cJSON *c = cJSON_CreateArray();
    cJSON_AddItemToArray(c, cJSON_CreateString(column));
    cJSON_AddItemToArray(c, cJSON_CreateString(fn));
    cJSON_AddItemToArray(c, value);
    return c;
}

static cJSON *where_uuid(const char *uuid)
{
    cJSON *w = cJSON_CreateArray();
    cJSON_AddItemToArray(w, clause("_uuid", "==", uuid_ref(uuid)));
    return w;
}

static cJSON *where_eq(const char *column, const char *value)
{
    cJSON *w = cJSON_CreateArray();
    cJSON_AddItemToArray(w, clause(column, "==", cJSON_CreateString(value)));
    return w;
}

static cJSON *op(const char *kind, const char *table, cJSON *where)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "op", kind);
    cJSON_AddStringToObject(o, "table", table);
    if (where)
        cJSON_AddItemToObject(o, "where", where);
    return o;
}

static cJSON *strings(const char *const *items, size_t n)
{
    cJSON *a = cJSON_CreateArray();
    for (size_t i = 0; i < n; i++)
        cJSON_AddItemToArray(a, cJSON_CreateString(items[i]));
    return a;
}

/* wait until the row's columns equal their current raw values (mapping.guard) */
static cJSON *guard(const char *table, const char *uuid, const cJSON *row, const char *const *columns,
                    size_t n)
{
    cJSON *o = op("wait", table, where_uuid(uuid)), *rows = cJSON_CreateArray(),
          *values = cJSON_CreateObject();
    for (size_t i = 0; i < n; i++)
        cJSON_AddItemToObject(values, columns[i],
                              cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(row, columns[i]), 1));
    cJSON_AddItemToArray(rows, values);
    cJSON_AddItemToObject(o, "columns", strings(columns, n));
    cJSON_AddStringToObject(o, "until", "==");
    cJSON_AddItemToObject(o, "rows", rows);
    cJSON_AddNumberToObject(o, "timeout", 0);
    return o;
}

/* wait until no row matches where (the columns listed) */
static cJSON *absent(const char *table, cJSON *where, const char *column)
{
    cJSON *o = op("wait", table, where);
    cJSON_AddItemToObject(o, "columns", strings(&column, 1));
    cJSON_AddStringToObject(o, "until", "==");
    cJSON_AddItemToObject(o, "rows", cJSON_CreateArray());
    cJSON_AddNumberToObject(o, "timeout", 0);
    return o;
}

static cJSON *psk_map(const char *key)
{
    cJSON *m = cJSON_CreateArray(), *pairs = cJSON_CreateArray(), *pair = cJSON_CreateArray();
    cJSON_AddItemToArray(pair, cJSON_CreateString("key"));
    cJSON_AddItemToArray(pair, cJSON_CreateString(key));
    cJSON_AddItemToArray(pairs, pair);
    cJSON_AddItemToArray(m, cJSON_CreateString("map"));
    cJSON_AddItemToArray(m, pairs);
    return m;
}

static cJSON *tagged(const char *tag, cJSON *items)
{
    cJSON *a = cJSON_CreateArray();
    cJSON_AddItemToArray(a, cJSON_CreateString(tag));
    cJSON_AddItemToArray(a, items);
    return a;
}

static cJSON *mutation(const char *column, const char *mutator, cJSON *value)
{
    return clause(column, mutator, value);
}

/* replace every PSK slot of a VIF with the single slot "key" */
static cJSON *psk_mutate(const char *uuid, const cJSON *row, const char *key)
{
    const char *slots[64];
    size_t n = ovs_map_keys(row, "wpa_psks", slots, 64);
    cJSON *o = op("mutate", "Wifi_VIF_Config", where_uuid(uuid)), *m = cJSON_CreateArray();
    cJSON_AddItemToArray(m, mutation("wpa_psks", "delete", tagged("set", strings(slots, n))));
    cJSON_AddItemToArray(m, mutation("wpa_psks", "insert", psk_map(key)));
    cJSON_AddItemToObject(o, "mutations", m);
    return o;
}

static cJSON *vif_configs_mutate(const char *radio_uuid, const char *mutator, cJSON *ref)
{
    cJSON *o = op("mutate", "Wifi_Radio_Config", where_uuid(radio_uuid)), *m = cJSON_CreateArray(),
          *set = cJSON_CreateArray();
    cJSON_AddItemToArray(set, ref);
    cJSON_AddItemToArray(m, mutation("vif_configs", mutator, tagged("set", set)));
    cJSON_AddItemToObject(o, "mutations", m);
    return o;
}

static cJSON *named(const char *name)
{
    cJSON *a = cJSON_CreateArray();
    cJSON_AddItemToArray(a, cJSON_CreateString("named-uuid"));
    cJSON_AddItemToArray(a, cJSON_CreateString(name));
    return a;
}

/* the profile's row object, shallow-merged (later keys win, keeping the first position) */
static void merge(cJSON *into, const cJSON *from, const char *skip)
{
    const cJSON *item;
    cJSON_ArrayForEach(item, from)
    {
        if (skip && !strcmp(item->string, skip))
            continue;
        cJSON_DeleteItemFromObjectCaseSensitive(into, item->string);
        cJSON_AddItemToObject(into, item->string, cJSON_Duplicate(item, 1));
    }
}

em_reason em_check_results(const cJSON *results, const int *counts, size_t n)
{
    if (!cJSON_IsArray(results) || (size_t)cJSON_GetArraySize(results) != n)
        return EM_OUTCOME_UNKNOWN;
    for (size_t i = 0; i < n; i++) {
        const cJSON *r = cJSON_GetArrayItem(results, (int)i);
        if (!cJSON_IsObject(r))
            return EM_OUTCOME_UNKNOWN;
        const cJSON *e = cJSON_GetObjectItemCaseSensitive(r, "error");
        if (e && !cJSON_IsNull(e))
            return cJSON_IsString(e) && !strcmp(e->valuestring, "timed out") ? EM_PRECONDITION_FAILED
                                                                             : EM_INVALID_INPUT;
        const cJSON *c = cJSON_GetObjectItemCaseSensitive(r, "count");
        if (counts[i] >= 0 && (!cJSON_IsNumber(c) || c->valueint != counts[i]))
            return EM_OUTCOME_UNKNOWN;
    }
    return EM_OK;
}

/* Send ops; map the outcome like PodBackend.submit. */
static em_reason commit(em_ovs_session *s, cJSON *ops, const int *counts, em_submit_result *out,
                        cJSON **results_out)
{
    cJSON *results = s->transact(s->ctx, ops);
    em_reason r = results ? em_check_results(results, counts, (size_t)cJSON_GetArraySize(ops))
                          : EM_OUTCOME_UNKNOWN;
    out->reason = r;
    strcpy(out->status, r == EM_OK ? "committed"
                        : r == EM_PRECONDITION_FAILED ? "conflict"
                        : r == EM_OUTCOME_UNKNOWN ? "unknown" : "rejected");
    if (results_out && r == EM_OK)
        *results_out = results;
    else
        cJSON_Delete(results);
    return EM_OK;
}

/* -- AP scope ----------------------------------------------------------------------- */

static const char *const VIF_GUARDS[] = {"if_name", "mode", "enabled", "ssid", "wpa",
                                         "wpa_key_mgmt", "wpa_psks", "security",
                                         "rsn_pairwise_ccmp", "wpa_pairwise_tkip",
                                         "wpa_pairwise_ccmp"};

static bool wpa2_psk(const cJSON *row)
{
    const char *akm[8];
    size_t n = ovs_strings(row, "wpa_key_mgmt", akm, 8);
    return ovs_true(row, "wpa") && n == 1 && !strcmp(akm[0], "wpa-psk") &&
           ovs_true(row, "rsn_pairwise_ccmp") && ovs_map_size(row, "security") == 0 &&
           !ovs_true(row, "wpa_pairwise_tkip") && !ovs_true(row, "wpa_pairwise_ccmp") &&
           ovs_map_size(row, "wpa_psks") == 1;
}

static bool contains(const cJSON *row, const char *column, const char *uuid)
{
    const char *uuids[256];
    size_t n = ovs_uuids(row, column, uuids, 256);
    for (size_t i = 0; i < n; i++)
        if (!strcmp(uuids[i], uuid))
            return true;
    return false;
}

static const cJSON *table(const em_ovs_session *s, const char *name)
{
    return cJSON_GetObjectItemCaseSensitive(s->tables, name);
}

typedef struct {
    const char *node_uuid, *radio_uuid, *vif_uuid; /* vif_uuid NULL: absent (cold start) */
    const cJSON *radio, *config, *state;
    bool identity;
} binding;

static em_reason bind(const em_profile *p, const char *serial, const em_ovs_session *s, binding *b)
{
    memset(b, 0, sizeof(*b));
    const cJSON *nodes = table(s, "AWLAN_Node"), *row;
    if (cJSON_GetArraySize(nodes) != 1 || !ovs_str(nodes->child, "serial_number") ||
        strcmp(ovs_str(nodes->child, "serial_number"), serial))
        return EM_NOT_READY; /* pod identity absent or not the bound serial */
    b->node_uuid = nodes->child->string;
    int radios = 0;
    cJSON_ArrayForEach(row, table(s, "Wifi_Radio_Config"))
    {
        const char *band = ovs_str(row, "freq_band");
        if (band && !strcmp(band, p->band)) {
            radios++;
            b->radio_uuid = row->string;
            b->radio = row;
        }
    }
    if (radios != 1)
        return EM_NOT_READY;
    int configs = 0;
    cJSON_ArrayForEach(row, table(s, "Wifi_VIF_Config"))
    {
        const char *name = ovs_str(row, "if_name");
        if (name && !strcmp(name, p->fronthaul_if)) {
            configs++;
            b->vif_uuid = row->string;
            b->config = row;
        }
    }
    if (configs > 1)
        return EM_NOT_READY;
    if (b->vif_uuid && !contains(b->radio, "vif_configs", b->vif_uuid))
        return EM_NOT_READY;
    const cJSON *radio_state = NULL, *state = NULL;
    const char *state_uuid = NULL;
    int nradio_states = 0, nstates = 0;
    cJSON_ArrayForEach(row, table(s, "Wifi_Radio_State"))
    {
        const char *ref = ovs_str(row, "radio_config");
        if (ref && !strcmp(ref, b->radio_uuid)) {
            nradio_states++;
            radio_state = row;
        }
    }
    cJSON_ArrayForEach(row, table(s, "Wifi_VIF_State"))
    {
        const char *ref = ovs_str(row, "vif_config"), *name = ovs_str(row, "if_name");
        if (b->vif_uuid && ref && !strcmp(ref, b->vif_uuid) && name && !strcmp(name, p->fronthaul_if)) {
            nstates++;
            state = row;
            state_uuid = row->string;
        }
    }
    if (nstates == 1 && nradio_states == 1 && contains(radio_state, "vif_states", state_uuid))
        b->state = state;
    b->identity = nradio_states == 1 && ovs_str(radio_state, "mac") && *ovs_str(radio_state, "mac");
    return EM_OK;
}

/* Slot VIFs present on the pod (pod_profile._extras). */
typedef struct {
    const char *uuid;
    const cJSON *config;
} extra;

static em_reason extras(const em_profile *p, const em_ovs_session *s, const binding *b, extra *found)
{
    memset(found, 0, sizeof(extra) * p->nslots);
    const cJSON *row;
    cJSON_ArrayForEach(row, table(s, "Wifi_VIF_Config"))
    {
        const char *name = ovs_str(row, "if_name");
        for (size_t i = 0; name && i < p->nslots; i++) {
            if (strcmp(name, p->slots[i].if_name))
                continue;
            if (found[i].uuid || !contains(b->radio, "vif_configs", row->string))
                return EM_NOT_READY; /* slot VIF ambiguous or on another radio */
            found[i].uuid = row->string;
            found[i].config = row;
        }
    }
    return EM_OK;
}

/* Slot index for each additional BSS, by role in slot order (-1: unassigned). */
static em_reason assign(const em_profile *p, const em_ap_intent *intent, int *bss_of_slot)
{
    for (size_t i = 0; i < p->nslots; i++)
        bss_of_slot[i] = -1;
    for (size_t k = 0; k < intent->nadditional; k++) {
        size_t i = 0;
        while (i < p->nslots &&
               (bss_of_slot[i] >= 0 || strcmp(p->slots[i].role, intent->additional[k].role)))
            i++;
        if (i == p->nslots)
            return EM_UNSUPPORTED_OPERATION; /* more BSSes of a role than the radio maps */
        bss_of_slot[i] = (int)k;
    }
    return EM_OK;
}

/* the Inet interface names the pod has (one select, like the reference) */
static bool inet_names(em_ovs_session *s, cJSON *names)
{
    cJSON *ops = cJSON_CreateArray(), *sel = op("select", "Wifi_Inet_Config", cJSON_CreateArray());
    const char *col = "if_name";
    cJSON_AddItemToObject(sel, "columns", strings(&col, 1));
    cJSON_AddItemToArray(ops, sel);
    cJSON *results = s->transact(s->ctx, ops);
    cJSON_Delete(ops);
    if (!results)
        return false;
    const cJSON *row;
    cJSON_ArrayForEach(row, cJSON_GetObjectItemCaseSensitive(cJSON_GetArrayItem(results, 0), "rows"))
    {
        const char *name = ovs_str(row, "if_name");
        if (name)
            cJSON_AddItemToArray(names, cJSON_CreateString(name));
    }
    cJSON_Delete(results);
    return true;
}

static bool has_name(const cJSON *names, const char *name)
{
    const cJSON *n;
    cJSON_ArrayForEach(n, names)
    {
        if (!strcmp(n->valuestring, name))
            return true;
    }
    return false;
}

static cJSON *inet_row(const em_profile *p, const char *name)
{
    cJSON *row = cJSON_CreateObject();
    merge(row, p->inet, NULL);
    cJSON_AddStringToObject(row, "if_name", name);
    return row;
}

#define PUSH(o, n) (cJSON_AddItemToArray(ops, (o)), counts[nops++] = (n))

em_reason em_ap_submit(const em_profile *p, const char *serial, bool multi_bss,
                       em_ovs_session *s, const em_ap_intent *intent,
                       em_secret_resolver resolve, void *resolve_ctx, em_submit_result *out)
{
    memset(out, 0, sizeof(*out));
    size_t nslots = multi_bss ? p->nslots : 0;
    if (intent->nadditional && !nslots)
        return EM_UNSUPPORTED_OPERATION; /* this radio maps one BSS */
    em_profile slots_view = *p;
    slots_view.nslots = nslots;
    const char *key = resolve(resolve_ctx, intent->secret_ref);
    if (!key)
        return EM_INVALID_INPUT;
    int bss_of_slot[8];
    em_reason r = assign(&slots_view, intent, bss_of_slot);
    if (r != EM_OK)
        return r;
    binding b;
    if ((r = bind(p, serial, s, &b)) != EM_OK)
        return r;
    if (!b.identity)
        return EM_NOT_READY; /* complete current references and state required */
    extra found[8];
    if (nslots && (r = extras(&slots_view, s, &b, found)) != EM_OK)
        return r;
    if (b.vif_uuid) {
        if (!b.state)
            return EM_NOT_READY;
        const char *mode = ovs_str(b.config, "mode");
        if (!mode || strcmp(mode, "ap") || !ovs_true(b.config, "enabled") || !wpa2_psk(b.config))
            return EM_UNSUPPORTED_OPERATION; /* current VIF/security representation unqualified */
    }
    cJSON *ops = cJSON_CreateArray();
    int counts[64];
    size_t nops = 0;
    /* the node guard */
    {
        cJSON *o = op("wait", "AWLAN_Node", cJSON_CreateArray()), *rows = cJSON_CreateArray(),
              *v = cJSON_CreateObject();
        const char *cols[2] = {"_uuid", "serial_number"};
        cJSON_AddItemToObject(v, "_uuid", uuid_ref(b.node_uuid));
        cJSON_AddStringToObject(v, "serial_number", serial);
        cJSON_AddItemToArray(rows, v);
        cJSON_AddItemToObject(o, "columns", strings(cols, 2));
        cJSON_AddStringToObject(o, "until", "==");
        cJSON_AddItemToObject(o, "rows", rows);
        cJSON_AddNumberToObject(o, "timeout", 0);
        PUSH(o, -1);
    }
    const char *radio_cols[2] = {"if_name", "vif_configs"};
    PUSH(guard("Wifi_Radio_Config", b.radio_uuid, b.radio, radio_cols, 2), -1);
    if (!b.vif_uuid) {
        /* cold start: the fronthaul VIF from the M2 (pod_profile._create) */
        cJSON *sel = cJSON_CreateArray(), *o = op("select", "Wifi_Inet_Config", where_eq("if_name", p->fronthaul_if));
        const char *uuid_col = "_uuid";
        cJSON_AddItemToObject(o, "columns", strings(&uuid_col, 1));
        cJSON_AddItemToArray(sel, o);
        cJSON *res = s->transact(s->ctx, sel);
        cJSON_Delete(sel);
        const cJSON *rows = cJSON_GetObjectItemCaseSensitive(cJSON_GetArrayItem(res, 0), "rows");
        bool missing_inet = !res || !cJSON_GetArraySize(rows);
        cJSON_Delete(res);
        PUSH(absent("Wifi_VIF_Config", where_eq("if_name", p->fronthaul_if), "if_name"), -1);
        cJSON *ins = op("insert", "Wifi_VIF_Config", NULL), *row = cJSON_CreateObject();
        cJSON_AddStringToObject(ins, "uuid-name", "fh");
        merge(row, p->fronthaul_vif, NULL);
        cJSON_DeleteItemFromObjectCaseSensitive(row, "if_name");
        cJSON_AddStringToObject(row, "if_name", p->fronthaul_if);
        cJSON_DeleteItemFromObjectCaseSensitive(row, "ssid");
        cJSON_AddStringToObject(row, "ssid", intent->ssid);
        cJSON_AddItemToObject(row, "wpa_psks", psk_map(key));
        cJSON_AddItemToObject(ins, "row", row);
        PUSH(ins, -1);
        PUSH(vif_configs_mutate(b.radio_uuid, "insert", named("fh")), 1);
        cJSON *upd = op("update", "Wifi_Radio_Config", where_uuid(b.radio_uuid)), *urow = cJSON_CreateObject();
        cJSON_AddNumberToObject(urow, "channel", p->channel);
        cJSON_AddStringToObject(urow, "ht_mode", p->ht_mode);
        cJSON_AddTrueToObject(urow, "enabled");
        cJSON_AddItemToObject(upd, "row", urow);
        PUSH(upd, 1);
        if (missing_inet) {
            PUSH(absent("Wifi_Inet_Config", where_eq("if_name", p->fronthaul_if), "if_name"), -1);
            cJSON *io = op("insert", "Wifi_Inet_Config", NULL);
            cJSON_AddItemToObject(io, "row", inet_row(p, p->fronthaul_if));
            PUSH(io, -1);
        }
    } else {
        PUSH(guard("Wifi_VIF_Config", b.vif_uuid, b.config, VIF_GUARDS, 11), -1);
        cJSON *upd = op("update", "Wifi_VIF_Config", where_uuid(b.vif_uuid)), *row = cJSON_CreateObject();
        cJSON_AddStringToObject(row, "ssid", intent->ssid);
        cJSON_AddItemToObject(upd, "row", row);
        PUSH(upd, 1);
        PUSH(psk_mutate(b.vif_uuid, b.config, key), 1);
    }
    if (nslots && intent->has_additional) {
        /* make the managed slot VIFs exactly the intent's additional BSSes */
        cJSON *names = cJSON_CreateArray();
        if (!inet_names(s, names)) {
            cJSON_Delete(names);
            cJSON_Delete(ops);
            strcpy(out->status, "unknown");
            out->reason = EM_OUTCOME_UNKNOWN;
            return EM_OK;
        }
        for (size_t i = 0; i < nslots; i++) {
            const char *name = p->slots[i].if_name;
            if (found[i].uuid) {
                const char *cols[12];
                size_t nc = 0;
                for (size_t k = 0; k < 11; k++)
                    if (cJSON_GetObjectItemCaseSensitive(found[i].config, VIF_GUARDS[k]))
                        cols[nc++] = VIF_GUARDS[k];
                if (cJSON_GetObjectItemCaseSensitive(found[i].config, "multi_ap"))
                    cols[nc++] = "multi_ap";
                PUSH(guard("Wifi_VIF_Config", found[i].uuid, found[i].config, cols, nc), -1);
            }
            int k = bss_of_slot[i];
            if (k < 0 && found[i].uuid) {
                PUSH(vif_configs_mutate(b.radio_uuid, "delete", uuid_ref(found[i].uuid)), 1);
                PUSH(op("delete", "Wifi_VIF_Config", where_uuid(found[i].uuid)), 1);
                if (has_name(names, name))
                    PUSH(op("delete", "Wifi_Inet_Config", where_eq("if_name", name)), -1);
            } else if (k >= 0) {
                cJSON *base = cJSON_CreateObject();
                merge(base, p->fronthaul_vif, NULL);
                if (!strcmp(p->slots[i].role, "backhaul"))
                    merge(base, p->backhaul_vif, NULL);
                const char *bkey = resolve(resolve_ctx, intent->additional[k].secret_ref);
                if (!bkey) {
                    cJSON_Delete(base);
                    cJSON_Delete(names);
                    cJSON_Delete(ops);
                    return EM_INVALID_INPUT;
                }
                if (found[i].uuid) {
                    cJSON *upd = op("update", "Wifi_VIF_Config", where_uuid(found[i].uuid)),
                          *row = cJSON_CreateObject();
                    merge(row, base, "vif_radio_idx");
                    cJSON_DeleteItemFromObjectCaseSensitive(row, "ssid");
                    cJSON_AddStringToObject(row, "ssid", intent->additional[k].ssid);
                    cJSON_AddItemToObject(upd, "row", row);
                    PUSH(upd, 1);
                    PUSH(psk_mutate(found[i].uuid, found[i].config, bkey), 1);
                } else {
                    char label[16];
                    snprintf(label, sizeof(label), "extra%zu", i);
                    PUSH(absent("Wifi_VIF_Config", where_eq("if_name", name), "if_name"), -1);
                    cJSON *ins = op("insert", "Wifi_VIF_Config", NULL), *row = cJSON_CreateObject();
                    cJSON_AddStringToObject(ins, "uuid-name", label);
                    merge(row, base, NULL);
                    const char *keys[4] = {"if_name", "ssid", "vif_radio_idx", "wpa_psks"};
                    for (int x = 0; x < 4; x++)
                        cJSON_DeleteItemFromObjectCaseSensitive(row, keys[x]);
                    cJSON_AddStringToObject(row, "if_name", name);
                    cJSON_AddStringToObject(row, "ssid", intent->additional[k].ssid);
                    cJSON_AddNumberToObject(row, "vif_radio_idx", p->slots[i].vif_radio_idx);
                    cJSON_AddItemToObject(row, "wpa_psks", psk_map(bkey));
                    cJSON_AddItemToObject(ins, "row", row);
                    PUSH(ins, -1);
                    PUSH(vif_configs_mutate(b.radio_uuid, "insert", named(label)), 1);
                }
                cJSON_Delete(base);
                if (!has_name(names, name)) {
                    cJSON *io = op("insert", "Wifi_Inet_Config", NULL);
                    cJSON_AddItemToObject(io, "row", inet_row(p, name));
                    PUSH(io, -1);
                }
            }
        }
        cJSON_Delete(names);
    }
    commit(s, ops, counts, out, NULL);
    cJSON_Delete(ops);
    return EM_OK;
}

/* -- client steering scope (spec §3.7) ----------------------------------------------- */

static cJSON *present(const char *table, const char *uuid, const char *column, const char *value,
                      const char *column2, const char *value2)
{
    cJSON *o = op("wait", table, where_uuid(uuid)), *rows = cJSON_CreateArray(),
          *v = cJSON_CreateObject(), *cols = cJSON_CreateArray();
    cJSON_AddItemToArray(cols, cJSON_CreateString(column));
    cJSON_AddStringToObject(v, column, value);
    if (column2) {
        cJSON_AddItemToArray(cols, cJSON_CreateString(column2));
        cJSON_AddStringToObject(v, column2, value2);
    }
    cJSON_AddItemToArray(rows, v);
    cJSON_AddItemToObject(o, "columns", cols);
    cJSON_AddStringToObject(o, "until", "==");
    cJSON_AddItemToObject(o, "rows", rows);
    cJSON_AddNumberToObject(o, "timeout", 0);
    return o;
}

static const char *ht_mode(int op_class)
{
    switch (op_class) {
    case 83: case 84: case 116: case 117: case 119: case 120: case 122: case 123: case 126:
    case 127: return "HT40";
    case 128: case 133: return "HT80";
    case 129: case 134: return "HT160";
    default: return "HT20";
    }
}

static cJSON *map2(const char *k1, const char *v1, const char *k2, const char *v2)
{
    cJSON *pairs = cJSON_CreateArray();
    const char *k[2] = {k1, k2}, *v[2] = {v1, v2};
    for (int i = 0; i < 2 && k[i]; i++) {
        cJSON *pair = cJSON_CreateArray();
        cJSON_AddItemToArray(pair, cJSON_CreateString(k[i]));
        cJSON_AddItemToArray(pair, cJSON_CreateString(v[i]));
        cJSON_AddItemToArray(pairs, pair);
    }
    return tagged("map", pairs);
}

static const char *node_uuid(const em_ovs_session *s, const char *serial)
{
    const cJSON *nodes = table(s, "AWLAN_Node");
    if (cJSON_GetArraySize(nodes) != 1 || !ovs_str(nodes->child, "serial_number") ||
        strcmp(ovs_str(nodes->child, "serial_number"), serial))
        return NULL;
    return nodes->child->string;
}

em_reason em_steering_open(const char *serial, em_ovs_session *s, const em_steering_intent *in,
                           em_submit_result *result, em_steering_rows *created)
{
    memset(result, 0, sizeof(*result));
    memset(created, 0, sizeof(*created));
    const char *node = node_uuid(s, serial);
    if (!node)
        return EM_NOT_READY;
    if (in->window < 5 || in->window > 120 || !strcmp(in->target_bssid, in->source_bssid) ||
        in->op_class < 1 || in->op_class > 255 || in->channel < 1 || in->channel > 233)
        return EM_INVALID_INPUT;
    /* the source: an enabled AP VIF of the pod, its group column and its stations */
    const cJSON *row, *vif = NULL;
    cJSON_ArrayForEach(row, table(s, "Wifi_VIF_State"))
    {
        const char *mac = ovs_str(row, "mac"), *mode = ovs_str(row, "mode");
        if (mac && !strcmp(mac, in->source_bssid) && mode && !strcmp(mode, "ap") &&
            ovs_true(row, "enabled")) {
            vif = row;
            break;
        }
    }
    if (!vif)
        return EM_UNSUPPORTED_OPERATION; /* the source is not an AP BSS of the pod */
    const char *if_name = ovs_str(vif, "if_name"), *column = NULL;
    cJSON_ArrayForEach(row, table(s, "Wifi_Radio_State"))
    {
        if (!contains(row, "vif_states", vif->string))
            continue;
        const char *band = ovs_str(row, "freq_band");
        column = !band ? NULL : !strcmp(band, "2.4G") ? "if_name_2g"
                 : (!strcmp(band, "5G") || !strcmp(band, "5GL") || !strcmp(band, "5GU")) ? "if_name_5g"
                                                                                       : NULL;
        break;
    }
    if (!column)
        return EM_UNSUPPORTED_OPERATION; /* the source radio's band has no group */
    bool on_source = false;
    const char *assoc[256];
    size_t na = ovs_uuids(vif, "associated_clients", assoc, 256);
    for (size_t i = 0; i < na; i++) {
        const cJSON *c = cJSON_GetObjectItemCaseSensitive(table(s, "Wifi_Associated_Clients"), assoc[i]);
        const char *mac = c ? ovs_str(c, "mac") : NULL;
        on_source = on_source || (mac && !strcmp(mac, in->station));
    }
    if (!on_source)
        return EM_NOT_READY; /* the station is not associated with the source */
    const char *group = NULL, *neighbor = NULL;
    int ngroups = 0, nneighbors = 0;
    cJSON_ArrayForEach(row, table(s, "Band_Steering_Config"))
    {
        const char *a = ovs_str(row, "if_name_2g"), *b = ovs_str(row, "if_name_5g");
        if ((a && !strcmp(a, if_name)) || (b && !strcmp(b, if_name))) {
            ngroups++;
            group = row->string;
        }
    }
    cJSON_ArrayForEach(row, table(s, "Wifi_VIF_Neighbors"))
    {
        const char *bssid = ovs_str(row, "bssid"), *name = ovs_str(row, "if_name");
        if (bssid && !strcmp(bssid, in->target_bssid) && name && !strcmp(name, if_name)) {
            nneighbors++;
            neighbor = row->string;
        }
    }
    cJSON_ArrayForEach(row, table(s, "Band_Steering_Clients"))
    {
        const char *mac = ovs_str(row, "mac");
        if (mac && !strcmp(mac, in->station))
            return EM_OWNERSHIP_CONFLICT; /* the station has a steering row already */
    }
    if (ngroups > 1 || nneighbors > 1)
        return EM_OWNERSHIP_CONFLICT;
    cJSON *ops = cJSON_CreateArray();
    int counts[16];
    size_t nops = 0;
    int group_at = -1, neighbor_at = -1, client_at;
    PUSH(present("AWLAN_Node", node, "serial_number", serial, NULL, NULL), -1);
    {
        cJSON *w = cJSON_CreateArray();
        cJSON_AddItemToArray(w, clause("mac", "==", cJSON_CreateString(in->station)));
        PUSH(absent("Band_Steering_Clients", w, "_uuid"), -1);
    }
    if (group) {
        PUSH(present("Band_Steering_Config", group, column, if_name, NULL, NULL), -1);
    } else {
        PUSH(absent("Band_Steering_Config", where_eq("if_name_2g", if_name), "_uuid"), -1);
        PUSH(absent("Band_Steering_Config", where_eq("if_name_5g", if_name), "_uuid"), -1);
        cJSON *ins = op("insert", "Band_Steering_Config", NULL), *r = cJSON_CreateObject();
        cJSON_AddStringToObject(r, column, if_name);
        cJSON_AddItemToObject(ins, "row", r);
        group_at = (int)nops;
        PUSH(ins, -1);
    }
    if (neighbor) {
        PUSH(present("Wifi_VIF_Neighbors", neighbor, "bssid", in->target_bssid, "if_name", if_name), -1);
    } else {
        cJSON *w = cJSON_CreateArray();
        cJSON_AddItemToArray(w, clause("bssid", "==", cJSON_CreateString(in->target_bssid)));
        cJSON_AddItemToArray(w, clause("if_name", "==", cJSON_CreateString(if_name)));
        PUSH(absent("Wifi_VIF_Neighbors", w, "_uuid"), -1);
        cJSON *ins = op("insert", "Wifi_VIF_Neighbors", NULL), *r = cJSON_CreateObject();
        cJSON_AddStringToObject(r, "bssid", in->target_bssid);
        cJSON_AddStringToObject(r, "if_name", if_name);
        cJSON_AddNumberToObject(r, "channel", in->channel);
        cJSON_AddNumberToObject(r, "op_class", in->op_class);
        cJSON_AddStringToObject(r, "ht_mode", ht_mode(in->op_class));
        cJSON_AddNumberToObject(r, "priority", 1);
        cJSON_AddItemToObject(ins, "row", r);
        neighbor_at = (int)nops;
        PUSH(ins, -1);
    }
    /* the complete client row: owm dereferences its fields unchecked */
    static const char *const zero[] = {"hwm", "lwm", "bottom_lwm", "kick_reason", "max_rejects",
        "rejects_tmout_secs", "backoff_secs", "pref_5g_pre_assoc_block_timeout_msecs",
        "pref_6g_pre_assoc_block_timeout_msecs", "kick_debounce_period", "sc_kick_reason",
        "sc_kick_debounce_period", "sticky_kick_debounce_period", "sticky_kick_reason",
        "steering_success_cnt", "steering_fail_cnt", "steering_kick_cnt", "sticky_kick_cnt"};
    static const char *const off[] = {"kick_upon_idle", "pre_assoc_auth_block",
        "send_rrm_after_assoc", "neighbor_list_filter_by_beacon_report"};
    cJSON *ins = op("insert", "Band_Steering_Clients", NULL), *r = cJSON_CreateObject();
    for (size_t i = 0; i < sizeof(zero) / sizeof(*zero); i++)
        cJSON_AddNumberToObject(r, zero[i], 0);
    for (size_t i = 0; i < sizeof(off) / sizeof(*off); i++)
        cJSON_AddFalseToObject(r, off[i]);
    cJSON_AddStringToObject(r, "kick_type", "none");
    cJSON_AddStringToObject(r, "reject_detection", "none");
    cJSON_AddStringToObject(r, "sticky_kick_type", "none");
    cJSON_AddStringToObject(r, "pref_5g", "never");
    cJSON_AddStringToObject(r, "pref_bs_allowed", "never");
    cJSON_AddStringToObject(r, "pref_6g", "never");
    cJSON_AddStringToObject(r, "mac", in->station);
    cJSON_AddStringToObject(r, "cs_mode", "away");
    char window[8];
    snprintf(window, sizeof(window), "%d", in->window);
    cJSON_AddItemToObject(r, "cs_params", map2("cs_enforce_period", window, NULL, NULL));
    cJSON_AddStringToObject(r, "sc_kick_type", "btm_deauth");
    cJSON_AddItemToObject(r, "sc_btm_params",
                          map2("bssid", in->target_bssid, "disassoc_imminent",
                               in->disassoc_imminent ? "1" : "0"));
    cJSON_AddItemToObject(ins, "row", r);
    client_at = (int)nops;
    PUSH(ins, -1);
    cJSON *results = NULL;
    commit(s, ops, counts, result, &results);
    cJSON_Delete(ops);
    if (results) {
        struct { int at; char *out; } rows[3] = {{client_at, created->client},
                                                {neighbor_at, created->neighbor},
                                                {group_at, created->group}};
        for (int i = 0; i < 3; i++) {
            if (rows[i].at < 0)
                continue;
            const cJSON *u = cJSON_GetObjectItemCaseSensitive(cJSON_GetArrayItem(results, rows[i].at), "uuid");
            const char *text = cJSON_GetStringValue(cJSON_GetArrayItem(u, 1));
            if (text && strlen(text) < 37)
                strcpy(rows[i].out, text);
        }
        cJSON_Delete(results);
    }
    return EM_OK;
}

em_reason em_steering_kick(const char *serial, em_ovs_session *s, const char *station,
                           const char *client_row)
{
    const char *node = node_uuid(s, serial);
    if (!node)
        return EM_NOT_READY;
    cJSON *ops = cJSON_CreateArray(), *w = where_uuid(client_row);
    int counts[2];
    size_t nops = 0;
    PUSH(present("AWLAN_Node", node, "serial_number", serial, NULL, NULL), -1);
    cJSON_AddItemToArray(w, clause("mac", "==", cJSON_CreateString(station)));
    cJSON_AddItemToArray(w, clause("cs_state", "==", cJSON_CreateString("steering")));
    cJSON *upd = op("update", "Band_Steering_Clients", w), *row = cJSON_CreateObject();
    cJSON_AddStringToObject(row, "force_kick", "directed");
    cJSON_AddItemToObject(upd, "row", row);
    PUSH(upd, 1);
    cJSON *results = s->transact(s->ctx, ops);
    cJSON_Delete(ops);
    em_reason r = results ? em_check_results(results, counts, 2) : EM_OUTCOME_UNKNOWN;
    cJSON_Delete(results);
    return r;
}

em_reason em_steering_close(em_ovs_session *s, const em_steering_intent *in,
                            const em_steering_rows *created)
{
    cJSON *ops = cJSON_CreateArray();
    int counts[3];
    size_t nops = 0;
    if (*created->client) {
        cJSON *w = where_uuid(created->client);
        cJSON_AddItemToArray(w, clause("mac", "==", cJSON_CreateString(in->station)));
        PUSH(op("delete", "Band_Steering_Clients", w), -1);
    }
    if (*created->neighbor) {
        cJSON *w = where_uuid(created->neighbor);
        cJSON_AddItemToArray(w, clause("bssid", "==", cJSON_CreateString(in->target_bssid)));
        PUSH(op("delete", "Wifi_VIF_Neighbors", w), -1);
    }
    if (*created->group)
        PUSH(op("delete", "Band_Steering_Config", where_uuid(created->group)), -1);
    if (!nops) {
        cJSON_Delete(ops);
        return EM_OK;
    }
    cJSON *results = s->transact(s->ctx, ops);
    cJSON_Delete(ops);
    em_reason r = results ? em_check_results(results, counts, nops) : EM_OUTCOME_UNKNOWN;
    cJSON_Delete(results);
    return r;
}

/* -- uplink scope (spec §8.3) ------------------------------------------------------- */

static bool lower_mac(const char *text)
{
    if (!text || strlen(text) != 17)
        return false;
    for (int i = 0; i < 17; i++) {
        char c = text[i];
        if (i % 3 == 2 ? c != ':' : !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
            return false;
    }
    return true;
}

static bool same_mac(const char *a, const char *b)
{
    if (!a || !b || strlen(a) != strlen(b))
        return false;
    for (size_t i = 0; a[i]; i++) {
        char x = a[i] >= 'A' && a[i] <= 'F' ? (char)(a[i] + 32) : a[i];
        if (x != b[i])
            return false;
    }
    return true;
}

em_reason em_uplink_submit(const char *serial, em_ovs_session *s, const em_uplink_intent *in,
                           em_secret_resolver resolve, void *resolve_ctx, em_submit_result *out,
                           const char **refusal)
{
    memset(out, 0, sizeof(*out));
    *refusal = NULL;
    size_t slen = in->ssid ? strlen(in->ssid) : 0;
    if (slen < 1 || slen > 32) {
        *refusal = "SSID must contain 1–32 UTF-8 bytes, no NUL";
        return EM_INVALID_INPUT;
    }
    const char *key = in->secret_ref ? resolve(resolve_ctx, in->secret_ref) : NULL;
    if (!in->station || !*in->station || !key) {
        *refusal = "explicit station and secret reference required";
        return EM_INVALID_INPUT;
    }
    if (!in->bssid) {
        *refusal = "the upstream BSSID is required";
        return EM_INVALID_INPUT;
    }
    if (!lower_mac(in->bssid)) {
        *refusal = "BSSID must be a lower-case MAC address";
        return EM_INVALID_INPUT;
    }
    const char *node = node_uuid(s, serial);
    if (!node) {
        *refusal = "pod identity absent or not the bound serial";
        return EM_NOT_READY;
    }
    if (!cJSON_GetArraySize(table(s, "Wifi_Radio_Config"))) {
        *refusal = "the pod's radios are not configured yet";
        return EM_NOT_READY;
    }
    const cJSON *row, *vif = NULL;
    int nvifs = 0;
    cJSON_ArrayForEach(row, table(s, "Wifi_VIF_Config"))
    {
        const char *name = ovs_str(row, "if_name");
        if (name && !strcmp(name, in->station)) {
            nvifs++;
            vif = row;
        }
    }
    if (nvifs > 1) {
        *refusal = "backhaul station ambiguous";
        return EM_NOT_READY;
    }
    if (vif && (!ovs_str(vif, "mode") || strcmp(ovs_str(vif, "mode"), "sta"))) {
        *refusal = "the bound uplink VIF is not a station";
        return EM_UNSUPPORTED_OPERATION;
    }
    /* joining one of the pod's own BSSes would bridge its backhaul BSS into br-home */
    const char *tables[2] = {"Wifi_VIF_State", "Wifi_Radio_State"};
    for (int t = 0; t < 2; t++)
        cJSON_ArrayForEach(row, table(s, tables[t]))
        {
            if (same_mac(ovs_str(row, "mac"), in->bssid)) {
                *refusal = "the upstream BSSID is one of the pod's own";
                return EM_INVALID_INPUT;
            }
        }
    if (!vif) {
        *refusal = "the pod's backhaul station row is required";
        return EM_NOT_READY;
    }
    em_uplink_state state;
    em_uplink_state_of(s->tables, in->station, &state);
    if (!*state.kind) {
        *refusal = "no working uplink to switch from";
        return EM_NOT_READY;
    }
    cJSON *ops = cJSON_CreateArray();
    int counts[4];
    size_t nops = 0;
    {
        cJSON *o = op("wait", "AWLAN_Node", cJSON_CreateArray()), *rows = cJSON_CreateArray(),
              *v = cJSON_CreateObject();
        const char *cols[2] = {"_uuid", "serial_number"};
        cJSON_AddItemToObject(v, "_uuid", uuid_ref(node));
        cJSON_AddStringToObject(v, "serial_number", serial);
        cJSON_AddItemToArray(rows, v);
        cJSON_AddItemToObject(o, "columns", strings(cols, 2));
        cJSON_AddStringToObject(o, "until", "==");
        cJSON_AddItemToObject(o, "rows", rows);
        cJSON_AddNumberToObject(o, "timeout", 0);
        PUSH(o, -1);
    }
    static const char *const station_guards[] = {"if_name", "mode", "enabled", "ssid",
                                                 "credential_configs", "multi_ap"};
    const char *cols[6];
    size_t nc = 0;
    for (size_t i = 0; i < 6; i++)
        if (cJSON_GetObjectItemCaseSensitive(vif, station_guards[i]))
            cols[nc++] = station_guards[i];
    PUSH(guard("Wifi_VIF_Config", vif->string, vif, cols, nc), -1);
    cJSON *ins = op("insert", "Wifi_Credential_Config", NULL), *cred = cJSON_CreateObject();
    cJSON_AddStringToObject(ins, "uuid-name", "backhaul");
    cJSON_AddStringToObject(cred, "ssid", in->ssid);
    cJSON_AddItemToObject(cred, "security", map2("encryption", "WPA-PSK", "key", key));
    cJSON_AddStringToObject(cred, "onboard_type", "multi_ap");
    cJSON_AddNumberToObject(cred, "priority", 1);
    cJSON_AddTrueToObject(cred, "enabled");
    cJSON_AddStringToObject(cred, "bssid", in->bssid);
    cJSON_AddItemToObject(ins, "row", cred);
    PUSH(ins, -1);
    /* credential-list mode: owm then sets multi_ap, 4-address and br-home itself */
    cJSON *upd = op("update", "Wifi_VIF_Config", where_uuid(vif->string)), *r = cJSON_CreateObject(),
          *links = cJSON_CreateArray();
    cJSON_AddTrueToObject(r, "enabled");
    cJSON_AddStringToObject(r, "ssid", "");
    cJSON_AddItemToObject(r, "security", tagged("map", cJSON_CreateArray()));
    cJSON_AddItemToObject(r, "multi_ap", tagged("set", cJSON_CreateArray()));
    cJSON_AddItemToObject(r, "wds", tagged("set", cJSON_CreateArray()));
    cJSON_AddItemToArray(links, named("backhaul"));
    cJSON_AddItemToObject(r, "credential_configs", tagged("set", links));
    cJSON_AddItemToObject(upd, "row", r);
    PUSH(upd, 1);
    commit(s, ops, counts, out, NULL);
    cJSON_Delete(ops);
    return EM_OK;
}
