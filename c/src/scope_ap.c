/* The AP scope (emosa.opensync.pod_profile.PodBackend). */
#include "scope_ap.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "canon.h"
#include "ovs.h"
#include "scope.h"

#define MODE "opensync-6.6-hwsim"
#define PROVENANCE "opensync-owm:Wifi_VIF_State"

static const char *str(const cJSON *o, const char *k)
{
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, k));
}

cJSON *em_ap_intent_record(const char *pod_id, const char *ssid, const char *secret_ref, cJSON *additional)
{
    cJSON *i = cJSON_CreateObject();
    cJSON_AddStringToObject(i, "pod_id", pod_id);
    cJSON_AddStringToObject(i, "radio_id", "radio-1");
    cJSON_AddStringToObject(i, "bss_id", "bss-1");
    cJSON_AddStringToObject(i, "ssid", ssid);
    cJSON_AddStringToObject(i, "secret_ref", secret_ref);
    cJSON_AddTrueToObject(i, "enabled");
    cJSON_AddStringToObject(i, "security_mode", "wpa2-psk");
    if (additional)
        cJSON_AddItemToObject(i, "additional", additional);
    return i;
}

static bool ssid_ok(const char *s)
{
    size_t n = s ? strlen(s) : 0;
    return n >= 1 && n <= 32; /* a C string has no NUL inside */
}

static bool nonempty(const cJSON *o, const char *k)
{
    const char *v = str(o, k);
    return v && *v;
}

/* Intent.validate */
static em_reason validate(const cJSON *intent)
{
    if (!ssid_ok(str(intent, "ssid")))
        return EM_INVALID_INPUT;
    const cJSON *extra = cJSON_GetObjectItemCaseSensitive(intent, "additional"), *b;
    if (extra && !cJSON_IsNull(extra)) {
        if (!cJSON_IsArray(extra) || cJSON_GetArraySize(extra) > 7)
            return EM_INVALID_INPUT;
        cJSON_ArrayForEach(b, extra)
        {
            const char *role = str(b, "role");
            if (!cJSON_IsObject(b) || cJSON_GetArraySize(b) != 3 || !role ||
                (strcmp(role, "fronthaul") && strcmp(role, "backhaul")) || !ssid_ok(str(b, "ssid")) ||
                !nonempty(b, "secret_ref"))
                return EM_INVALID_INPUT;
        }
    }
    const char *mode = str(intent, "security_mode");
    if (!mode || strcmp(mode, "wpa2-psk") || !cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(intent, "enabled")))
        return EM_UNSUPPORTED_OPERATION;
    if (!nonempty(intent, "pod_id") || !nonempty(intent, "radio_id") || !nonempty(intent, "bss_id") ||
        !nonempty(intent, "secret_ref"))
        return EM_INVALID_INPUT;
    return EM_OK;
}

static cJSON *credential(const em_ap_scope *s, const char *ref, em_reason *why)
{
    char *key = em_vault_resolve(s->vault, ref, why);
    if (!key)
        return NULL;
    char fp[65];
    em_vault_fingerprint_text(s->vault, key, fp);
    free(key);
    return cJSON_CreateString(fp);
}

/* Python's list ordering of [role, ssid, fingerprint] (strings throughout) */
static int compare_lists(const void *a, const void *b)
{
    const cJSON *x = *(const cJSON *const *)a, *y = *(const cJSON *const *)b;
    for (int i = 0; i < 3; i++) {
        int c = strcmp(cJSON_GetArrayItem(x, i)->valuestring, cJSON_GetArrayItem(y, i)->valuestring);
        if (c)
            return c;
    }
    return 0;
}

static cJSON *sorted(cJSON *array, int (*compare)(const void *, const void *))
{
    size_t n = (size_t)cJSON_GetArraySize(array), i = 0;
    cJSON **items = em_calloc(n, sizeof(*items));
    while (cJSON_GetArraySize(array))
        items[i++] = cJSON_DetachItemFromArray(array, 0);
    em_sort(items, n, sizeof(*items), compare);
    for (i = 0; i < n; i++)
        cJSON_AddItemToArray(array, items[i]);
    free(items);
    return array;
}

static cJSON *target(void *ctx, const cJSON *intent, em_reason *why)
{
    em_ap_scope *s = ctx;
    if ((*why = validate(intent)) != EM_OK)
        return NULL;
    cJSON *fp = credential(s, str(intent, "secret_ref"), why);
    if (!fp)
        return NULL;
    cJSON *t = cJSON_CreateObject();
    cJSON_AddStringToObject(t, "ssid", str(intent, "ssid"));
    cJSON_AddTrueToObject(t, "enabled");
    cJSON_AddStringToObject(t, "mode", "ap");
    cJSON_AddStringToObject(t, "security_mode", "wpa2-psk");
    cJSON_AddItemToObject(t, "credential_fingerprint", fp);
    const cJSON *extra = cJSON_GetObjectItemCaseSensitive(intent, "additional"), *b;
    if (extra && !cJSON_IsNull(extra)) {
        cJSON *list = cJSON_CreateArray();
        cJSON_ArrayForEach(b, extra)
        {
            cJSON *efp = credential(s, str(b, "secret_ref"), why);
            if (!efp) {
                cJSON_Delete(list);
                cJSON_Delete(t);
                return NULL;
            }
            cJSON *entry = cJSON_CreateArray();
            cJSON_AddItemToArray(entry, cJSON_CreateString(str(b, "role")));
            cJSON_AddItemToArray(entry, cJSON_CreateString(str(b, "ssid")));
            cJSON_AddItemToArray(entry, efp);
            cJSON_AddItemToArray(list, entry);
        }
        cJSON_AddItemToObject(t, "additional", sorted(list, compare_lists));
    }
    *why = EM_OK;
    return t;
}

/* -- the binding (PodBackend._binding) --------------------------------------------------- */

typedef struct {
    const cJSON *radio, *config, *state; /* state NULL: {} */
    const char *radio_uuid, *vif_uuid;
} binding;

static em_reason bind(em_ap_scope *s, const cJSON *tables, binding *b)
{
    memset(b, 0, sizeof(*b));
    s->identity = false;
    const char *node_uuid;
    if (!em_bound_node(tables, s->serial, &node_uuid))
        return EM_NOT_READY; /* "pod identity absent or not the bound serial" */
    if (!em_copy(s->node_uuid, sizeof(s->node_uuid), node_uuid)) /* the pod's row: a UUID or nothing */
        return EM_NOT_READY;
    const cJSON *r;
    size_t radios = 0;
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_Radio_Config"))
    {
        const char *band = ovs_str(r, "freq_band");
        if (band && !strcmp(band, s->profile->band)) {
            radios++;
            b->radio = r;
            b->radio_uuid = r->string;
        }
    }
    if (radios != 1)
        return EM_NOT_READY; /* "no single radio of the bound band" */
    /* the pod's rows: a radio whose UUID or interface does not fit is not written to */
    const char *radio_if = ovs_str(b->radio, "if_name");
    if (!em_copy(s->radio_uuid, sizeof(s->radio_uuid), b->radio_uuid) ||
        !em_copy(s->radio_if, sizeof(s->radio_if), radio_if ? radio_if : ""))
        return EM_NOT_READY;
    size_t configs = 0;
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_VIF_Config"))
    {
        const char *name = ovs_str(r, "if_name");
        if (name && !strcmp(name, s->profile->fronthaul_if)) {
            configs++;
            b->config = r;
            b->vif_uuid = r->string;
        }
    }
    if (configs > 1)
        return EM_NOT_READY; /* "bound fronthaul VIF ambiguous" */
    if (b->vif_uuid && !em_row_has_uuid(b->radio, "vif_configs", b->vif_uuid))
        return EM_NOT_READY; /* "bound VIF is not on the bound band's radio" */
    const cJSON *radio_state = NULL;
    size_t radio_states = 0;
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_Radio_State"))
    {
        const char *ref = ovs_str(r, "radio_config");
        if (ref && !strcmp(ref, b->radio_uuid)) {
            radio_states++;
            radio_state = r;
        }
    }
    const cJSON *state = NULL;
    size_t states = 0;
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_VIF_State"))
    {
        const char *ref = ovs_str(r, "vif_config"), *name = ovs_str(r, "if_name");
        if (b->vif_uuid && ref && !strcmp(ref, b->vif_uuid) && name && !strcmp(name, s->profile->fronthaul_if)) {
            if (!states)
                state = r;
            states++;
        }
    }
    if (states == 1 && radio_states == 1 && em_row_has_uuid(radio_state, "vif_states", state->string))
        b->state = state;
    if (radio_states == 1 && em_row_mac(radio_state, "mac", s->radio_mac)) {
        s->identity = true;
        s->has_bssid = b->state && em_row_mac(b->state, "mac", s->bssid);
        long channel = 0;
        s->channel = ovs_int(radio_state, "channel", &channel) ? (int)channel : 0;
    }
    return EM_OK;
}

/* PodBackend._values */
static cJSON *values(const em_ap_scope *s, const cJSON *row)
{
    cJSON *v = cJSON_CreateObject();
    cJSON_AddItemToObject(v, "ssid", em_row_string(row, "ssid"));
    cJSON_AddItemToObject(v, "enabled", em_row_bool(row, "enabled"));
    cJSON_AddItemToObject(v, "mode", em_row_string(row, "mode"));
    cJSON_AddItemToObject(v, "security_mode",
                          row && em_row_wpa2_psk(row) ? cJSON_CreateString("wpa2-psk") : cJSON_CreateNull());
    const char *key = em_row_sole_map_value(row, "wpa_psks");
    char fp[65];
    if (key && em_vault_fingerprint_text(s->vault, key, fp))
        cJSON_AddStringToObject(v, "credential_fingerprint", fp);
    else
        cJSON_AddNullToObject(v, "credential_fingerprint");
    return v;
}

static int compare_dumps(const void *a, const void *b)
{
    char *x = em_json_dumps(*(const cJSON *const *)a, EM_JSON_DEFAULT, false);
    char *y = em_json_dumps(*(const cJSON *const *)b, EM_JSON_DEFAULT, false);
    int c = strcmp(x ? x : "", y ? y : "");
    free(x);
    free(y);
    return c;
}

/* PodBackend._additional over the slot VIFs present: Config rows or (non-empty) State rows */
static cJSON *additional(const em_ap_scope *s, const cJSON *tables, bool states)
{
    cJSON *list = cJSON_CreateArray();
    const cJSON *r;
    cJSON_ArrayForEach(r, em_table(tables, "Wifi_VIF_Config"))
    {
        const char *name = ovs_str(r, "if_name");
        bool slot = false;
        for (size_t i = 0; name && i < s->profile->nslots; i++)
            slot = slot || !strcmp(name, s->profile->slots[i].if_name);
        if (!slot)
            continue;
        const cJSON *row = r;
        if (states) {
            row = NULL;
            const cJSON *st;
            size_t n = 0;
            cJSON_ArrayForEach(st, em_table(tables, "Wifi_VIF_State"))
            {
                const char *ref = ovs_str(st, "vif_config"), *sname = ovs_str(st, "if_name");
                if (ref && !strcmp(ref, r->string) && sname && !strcmp(sname, name)) {
                    row = st;
                    n++;
                }
            }
            if (n != 1)
                continue;
        }
        const char *multi_ap = ovs_str(row, "multi_ap"), *mode = ovs_str(row, "mode");
        const char *key = em_row_wpa2_psk(row) ? em_row_sole_map_value(row, "wpa_psks") : NULL;
        bool usable = mode && !strcmp(mode, "ap") && ovs_true(row, "enabled");
        cJSON *entry = cJSON_CreateArray();
        cJSON_AddItemToArray(entry, cJSON_CreateString(multi_ap && !strcmp(multi_ap, "backhaul_bss") ? "backhaul" : "fronthaul"));
        cJSON_AddItemToArray(entry, em_row_string(row, "ssid"));
        char fp[65];
        if (key && usable && em_vault_fingerprint_text(s->vault, key, fp))
            cJSON_AddItemToArray(entry, cJSON_CreateString(fp));
        else
            cJSON_AddItemToArray(entry, cJSON_CreateNull());
        cJSON_AddItemToArray(list, entry);
    }
    return sorted(list, compare_dumps);
}

/* slot VIFs are unambiguous and on the bound radio (PodBackend._extras) */
static bool extras_ok(const em_ap_scope *s, const cJSON *tables, const binding *b)
{
    for (size_t i = 0; i < s->profile->nslots; i++) {
        size_t n = 0;
        const cJSON *r;
        cJSON_ArrayForEach(r, em_table(tables, "Wifi_VIF_Config"))
        {
            const char *name = ovs_str(r, "if_name");
            if (name && !strcmp(name, s->profile->slots[i].if_name)) {
                if (++n > 1 || !em_row_has_uuid(b->radio, "vif_configs", r->string))
                    return false;
            }
        }
    }
    return true;
}

static bool snapshot(void *ctx, em_snapshot *out)
{
    em_ap_scope *s = ctx;
    memset(out, 0, sizeof(*out));
    const cJSON *tables = em_ovsdb_tables(s->ovs);
    bool raw_ready = em_ovsdb_ready(s->ovs);
    binding b;
    if (!raw_ready || bind(s, tables, &b) != EM_OK || (s->multi_bss && !extras_ok(s, tables, &b))) {
        if (!s->has_last)
            return false;
        /* the last snapshot, no longer ready or fresh */
        out->config = cJSON_Duplicate(s->last.config, true);
        out->observed = cJSON_Duplicate(s->last.observed, true);
        cJSON_ReplaceItemInObjectCaseSensitive(out->observed, "fresh", cJSON_CreateFalse());
        out->generation = s->last.generation;
        memcpy(out->schema_fingerprint, s->last.schema_fingerprint, 65);
        return true;
    }
    cJSON *configured = values(s, b.config), *observed = values(s, b.state);
    if (s->multi_bss) {
        cJSON_AddItemToObject(configured, "additional", additional(s, tables, false));
        cJSON_AddItemToObject(observed, "additional", additional(s, tables, true));
    }
    bool fresh = b.state != NULL;
    out->ready = fresh || (s->identity && !b.vif_uuid);
    out->generation = em_ovsdb_generation(s->ovs);
    EM_FORMAT_FIXED(out->schema_fingerprint, sizeof(out->schema_fingerprint), "%s", em_ovsdb_schema_fingerprint(s->ovs));
    out->config = configured;
    out->observed = em_observation(s->pod_id, "bss-1", observed, MODE, out->generation, fresh, PROVENANCE,
                                   em_ovsdb_revision(s->ovs));
    em_snapshot_clear(&s->last);
    s->last.config = cJSON_Duplicate(out->config, true);
    s->last.observed = cJSON_Duplicate(out->observed, true);
    s->last.generation = out->generation;
    memcpy(s->last.schema_fingerprint, out->schema_fingerprint, 65);
    s->has_last = true;
    return true;
}

/* PodBackend._assign: a slot for each additional BSS, by role in slot order */
static em_reason assign(const em_ap_scope *s, const cJSON *intent, cJSON *slots)
{
    bool used[8] = {0};
    const cJSON *b;
    cJSON_ArrayForEach(b, cJSON_GetObjectItemCaseSensitive(intent, "additional"))
    {
        size_t i = 0;
        while (i < s->profile->nslots && (used[i] || strcmp(s->profile->slots[i].role, str(b, "role"))))
            i++;
        if (i == s->profile->nslots)
            return EM_UNSUPPORTED_OPERATION; /* "more BSSes of a role than the radio maps" */
        used[i] = true;
        cJSON_AddStringToObject(slots, s->profile->slots[i].if_name, str(b, "role"));
    }
    return EM_OK;
}

static cJSON *plan(void *ctx, const cJSON *intent, em_reason *why)
{
    em_ap_scope *s = ctx;
    const cJSON *extra = cJSON_GetObjectItemCaseSensitive(intent, "additional");
    if (cJSON_GetArraySize(extra) && !s->multi_bss) {
        *why = EM_UNSUPPORTED_OPERATION; /* "this radio maps one BSS" */
        return NULL;
    }
    cJSON *t = target(ctx, intent, why);
    if (!t)
        return NULL;
    cJSON_Delete(t);
    if (strcmp(str(intent, "pod_id"), s->pod_id) || strcmp(str(intent, "radio_id"), "radio-1") ||
        strcmp(str(intent, "bss_id"), "bss-1")) {
        *why = EM_UNSUPPORTED_OPERATION; /* "request exceeds the bound VIF" */
        return NULL;
    }
    cJSON *slots = cJSON_CreateObject();
    if ((*why = s->multi_bss ? assign(s, intent, slots) : EM_OK) != EM_OK) {
        cJSON_Delete(slots);
        return NULL;
    }
    const cJSON *tables = em_ovsdb_tables(s->ovs);
    binding b;
    if (!em_ovsdb_ready(s->ovs) || (*why = bind(s, tables, &b)) != EM_OK || !s->identity) {
        *why = EM_NOT_READY;
        cJSON_Delete(slots);
        return NULL;
    }
    if (s->multi_bss && !extras_ok(s, tables, &b)) {
        *why = EM_NOT_READY; /* "slot VIF ambiguous or on another radio" */
        cJSON_Delete(slots);
        return NULL;
    }
    cJSON *p = cJSON_CreateObject();
    cJSON_AddStringToObject(p, "mapping", s->profile->id);
    cJSON_AddStringToObject(p, "radio_id", "radio-1");
    cJSON_AddStringToObject(p, "bss_id", "bss-1");
    cJSON_AddStringToObject(p, "ssid", str(intent, "ssid"));
    cJSON_AddStringToObject(p, "secret_ref", str(intent, "secret_ref"));
    cJSON_AddBoolToObject(p, "shared_radio_actuation", !b.vif_uuid || s->multi_bss);
    cJSON_AddItemToObject(p, "additional_slots", slots);
    if (!b.vif_uuid) {
        const char *fields[] = {"Wifi_VIF_Config", "Wifi_Radio_Config.vif_configs/channel", "Inet"};
        cJSON_AddStringToObject(p, "action", "create");
        cJSON_AddItemToObject(p, "fields", em_ovs_strings(fields, 3));
        cJSON_AddNumberToObject(p, "channel", s->profile->channel);
        cJSON_AddStringToObject(p, "guard", "pod serial, radio references, bound VIF absent");
        return p;
    }
    if (!b.state) {
        cJSON_Delete(p);
        *why = EM_NOT_READY; /* "bound VIF State unavailable" */
        return NULL;
    }
    const char *mode = ovs_str(b.config, "mode");
    if (!mode || strcmp(mode, "ap") || !ovs_true(b.config, "enabled") || !em_row_wpa2_psk(b.config)) {
        cJSON_Delete(p);
        *why = EM_UNSUPPORTED_OPERATION; /* "current VIF/security representation unqualified" */
        return NULL;
    }
    const char *fields[] = {"ssid", "wpa_psks"};
    cJSON_AddStringToObject(p, "action", "update");
    cJSON_AddItemToObject(p, "fields", em_ovs_strings(fields, 2));
    cJSON_AddStringToObject(p, "guard", "pod serial, radio/VIF references and VIF security fields");
    return p;
}

typedef struct {
    const em_vault *vault;
    char *keys[8];
    size_t n;
} resolver;

static const char *resolve(void *ctx, const char *ref)
{
    resolver *r = ctx;
    em_reason why;
    char *key = r->n < 8 ? em_vault_resolve(r->vault, ref, &why) : NULL;
    if (key)
        r->keys[r->n++] = key;
    return key;
}

static void submit(void *ctx, const cJSON *intent, const cJSON *attempt, em_submit_out *out)
{
    em_ap_scope *s = ctx;
    memset(out, 0, sizeof(*out));
    int generation = (int)cJSON_GetNumberValue(cJSON_GetObjectItemCaseSensitive(attempt, "session_generation"));
    if (!em_ovsdb_ready(s->ovs) || em_ovsdb_generation(s->ovs) != generation) {
        strcpy(out->status, "rejected");
        out->reason = EM_NOT_READY;
        return;
    }
    binding b;
    bool create = bind(s, em_ovsdb_tables(s->ovs), &b) == EM_OK && !b.vif_uuid;
    em_ap_intent in = {.ssid = str(intent, "ssid"), .secret_ref = str(intent, "secret_ref")};
    const cJSON *extra = cJSON_GetObjectItemCaseSensitive(intent, "additional"), *x;
    in.has_additional = extra && !cJSON_IsNull(extra);
    cJSON_ArrayForEach(x, extra)
    {
        if (in.nadditional == 8)
            break;
        in.additional[in.nadditional].role = str(x, "role");
        in.additional[in.nadditional].ssid = str(x, "ssid");
        in.additional[in.nadditional].secret_ref = str(x, "secret_ref");
        in.nadditional++;
    }
    em_ovs_session session = {em_ovsdb_tables(s->ovs), generation, s->transact, s->transact_ctx};
    resolver r = {s->vault, {0}, 0};
    em_submit_result result;
    em_reason e = em_ap_submit(s->profile, s->serial, s->multi_bss, &session, &in, resolve, &r, &result);
    for (size_t i = 0; i < r.n; i++) {
        memset(r.keys[i], 0, strlen(r.keys[i]));
        free(r.keys[i]);
    }
    if (e != EM_OK) { /* the plan failed at submission: the reference raises, the engine records unknown */
        strcpy(out->status, "unknown");
        out->reason = EM_OUTCOME_UNKNOWN;
        return;
    }
    EM_FORMAT_FIXED(out->status, sizeof(out->status), "%s", result.status);
    out->reason = result.reason;
    if (!strcmp(result.status, "committed")) {
        out->evidence = cJSON_CreateObject();
        cJSON_AddStringToObject(out->evidence, "attribution", "reply");
        cJSON_AddTrueToObject(out->evidence, "transaction_validated");
        cJSON_AddStringToObject(out->evidence, "transaction_id", str(attempt, "transaction_id"));
        cJSON_AddNumberToObject(out->evidence, "session_generation", generation);
        cJSON_AddStringToObject(out->evidence, "profile", s->profile->id);
        cJSON_AddStringToObject(out->evidence, "action", create ? "create" : "update");
        cJSON_AddNumberToObject(out->evidence, "additional_bss_count", (double)in.nadditional);
    }
}

em_backend em_ap_backend(void) { return (em_backend){MODE, target, snapshot, plan, submit}; }

bool em_ap_context(em_ap_scope *s, int *generation, char schema_fingerprint[65], char token[65])
{
    binding b;
    if (!em_ovsdb_ready(s->ovs) || bind(s, em_ovsdb_tables(s->ovs), &b) != EM_OK || !s->identity)
        return false; /* "bound radio State unavailable" */
    *generation = em_ovsdb_generation(s->ovs);
    EM_FORMAT_FIXED(schema_fingerprint, 65, "%s", em_ovsdb_schema_fingerprint(s->ovs)); /* SHA-256 hex */
    cJSON *list = cJSON_CreateArray();
    cJSON_AddItemToArray(list, cJSON_CreateString(s->profile->id));
    cJSON_AddItemToArray(list, cJSON_CreateString(s->node_uuid));
    cJSON_AddItemToArray(list, cJSON_CreateString(s->radio_uuid));
    const cJSON *state;
    const char *mac = NULL;
    cJSON_ArrayForEach(state, em_table(em_ovsdb_tables(s->ovs), "Wifi_Radio_State"))
    {
        const char *ref = ovs_str(state, "radio_config");
        if (ref && !strcmp(ref, s->radio_uuid))
            mac = ovs_str(state, "mac"); /* as the pod writes it */
    }
    cJSON_AddItemToArray(list, cJSON_CreateString(mac ? mac : s->radio_mac));
    char *text = em_json_dumps(list, EM_JSON_DEFAULT, true);
    cJSON_Delete(list);
    em_sha256_hex(text, strlen(text), token);
    free(text);
    return true;
}
