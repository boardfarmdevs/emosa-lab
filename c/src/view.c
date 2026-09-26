/* The pod as EasyMesh sees it (spec §3.3). Mirrors emosa.opensync.easymesh_view and the
 * capability and topology builders of emosa.wire.reports. */
#include "view.h"

#include <stdlib.h>
#include <string.h>

#include "ovs.h"

static bool copy_str(char *dst, size_t cap, const char *src)
{
    size_t n = src ? strlen(src) : 0;
    if (n >= cap)
        return false;
    memcpy(dst, src ? src : "", n);
    dst[n] = 0;
    return true;
}

static int cmp_mac(const void *a, const void *b) { return memcmp(a, b, 6); }

static int cmp_bss(const void *a, const void *b)
{
    return strcmp(((const em_bss_view *)a)->if_name, ((const em_bss_view *)b)->if_name);
}

static int cmp_radio(const void *a, const void *b)
{
    return memcmp(((const em_radio_view *)a)->ruid, ((const em_radio_view *)b)->ruid, 6);
}

em_reason em_device_view_from_rows(const cJSON *tables, em_device_view *out)
{
    memset(out, 0, sizeof(*out));
    const cJSON *nodes = cJSON_GetObjectItemCaseSensitive(tables, "AWLAN_Node");
    const cJSON *node = cJSON_GetArraySize(nodes) == 1 ? nodes->child : NULL;
    const char *serial = node ? ovs_str(node, "serial_number") : NULL;
    if (!serial || !*serial)
        return EM_NOT_READY;
    const char *id = ovs_str(node, "id"), *model = ovs_str(node, "model"),
               *firmware = ovs_str(node, "firmware_version");
    if (!copy_str(out->serial, sizeof(out->serial), serial) ||
        !copy_str(out->node_id, sizeof(out->node_id), id) ||
        !copy_str(out->model, sizeof(out->model), model) ||
        !copy_str(out->firmware, sizeof(out->firmware), firmware))
        return EM_INVALID_INPUT;
    out->has_node_id = id, out->has_model = model, out->has_firmware = firmware;
    const cJSON *vifs = cJSON_GetObjectItemCaseSensitive(tables, "Wifi_VIF_State");
    const cJSON *clients = cJSON_GetObjectItemCaseSensitive(tables, "Wifi_Associated_Clients");
    const cJSON *radio;
    cJSON_ArrayForEach(radio, cJSON_GetObjectItemCaseSensitive(tables, "Wifi_Radio_State"))
    {
        const char *mac = ovs_str(radio, "mac");
        if (!mac || !*mac)
            continue;
        if (out->nradios == EM_MAX_RADIOS)
            return EM_INVALID_INPUT;
        em_radio_view *r = &out->radios[out->nradios++];
        if (!em_parse_mac(mac, r->ruid) || !copy_str(r->if_name, sizeof(r->if_name), ovs_str(radio, "if_name")) ||
            !copy_str(r->band, sizeof(r->band), ovs_str(radio, "freq_band")))
            return EM_INVALID_INPUT;
        long v;
        if ((r->has_channel = ovs_int(radio, "channel", &v)))
            r->channel = (int)v;
        if ((r->has_tx_power = ovs_int(radio, "tx_power", &v)))
            r->tx_power = (int)v;
        r->enabled = ovs_true(radio, "enabled");
        const char *uuids[64];
        size_t n = ovs_uuids(radio, "vif_states", uuids, 64);
        for (size_t i = 0; i < n; i++) {
            const cJSON *vif = cJSON_GetObjectItemCaseSensitive(vifs, uuids[i]);
            const char *vmac = vif ? ovs_str(vif, "mac") : NULL;
            if (!vif || !ovs_true(vif, "enabled") || !vmac || !*vmac)
                continue;
            const char *mode = ovs_str(vif, "mode"), *ssid = ovs_str(vif, "ssid");
            const char *multi_ap = ovs_str(vif, "multi_ap");
            if (mode && !strcmp(mode, "sta")) {
                if (r->nuplinks == EM_MAX_UPLINKS)
                    return EM_INVALID_INPUT;
                em_uplink_view *u = &r->uplinks[r->nuplinks++];
                const char *parent = ovs_str(vif, "parent");
                if (!copy_str(u->if_name, sizeof(u->if_name), ovs_str(vif, "if_name")) ||
                    !em_parse_mac(vmac, u->mac) || !copy_str(u->ssid, sizeof(u->ssid), ssid))
                    return EM_INVALID_INPUT;
                if (parent && *parent) {
                    if (!em_parse_mac(parent, u->parent))
                        return EM_INVALID_INPUT;
                    u->has_parent = true;
                }
                u->multi_ap = multi_ap && !strcmp(multi_ap, "backhaul_sta") && ovs_true(vif, "wds");
            } else if (mode && !strcmp(mode, "ap")) {
                if (r->nbss == EM_MAX_BSS)
                    return EM_INVALID_INPUT;
                em_bss_view *b = &r->bss[r->nbss++];
                if (!copy_str(b->if_name, sizeof(b->if_name), ovs_str(vif, "if_name")) ||
                    !em_parse_mac(vmac, b->bssid) || !copy_str(b->ssid, sizeof(b->ssid), ssid))
                    return EM_INVALID_INPUT;
                b->backhaul = multi_ap && !strcmp(multi_ap, "backhaul_bss");
                const char *assoc[EM_MAX_STATIONS];
                size_t na = ovs_uuids(vif, "associated_clients", assoc, EM_MAX_STATIONS);
                for (size_t k = 0; k < na; k++) {
                    const cJSON *c = cJSON_GetObjectItemCaseSensitive(clients, assoc[k]);
                    const char *state = c ? ovs_str(c, "state") : NULL, *cmac = c ? ovs_str(c, "mac") : NULL;
                    if (!state || strcmp(state, "active") || !cmac)
                        continue;
                    if (!em_parse_mac(cmac, b->stations[b->nstations++]))
                        return EM_INVALID_INPUT;
                }
                qsort(b->stations, b->nstations, 6, cmp_mac);
            }
        }
        qsort(r->bss, r->nbss, sizeof(em_bss_view), cmp_bss);
    }
    qsort(out->radios, out->nradios, sizeof(em_radio_view), cmp_radio);
    return EM_OK;
}

const em_radio_view *em_view_radio(const em_device_view *v, const uint8_t ruid[6])
{
    for (size_t i = 0; i < v->nradios; i++)
        if (!memcmp(v->radios[i].ruid, ruid, 6))
            return &v->radios[i];
    return NULL;
}

static void add_mac(cJSON *o, const char *key, const uint8_t mac[6])
{
    char *text = em_mac_str(mac);
    cJSON_AddStringToObject(o, key, text);
    free(text);
}

static void add_opt_str(cJSON *o, const char *key, bool has, const char *value)
{
    if (has)
        cJSON_AddStringToObject(o, key, value);
    else
        cJSON_AddNullToObject(o, key);
}

cJSON *em_device_view_json(const em_device_view *v)
{
    cJSON *d = cJSON_CreateObject(), *radios = cJSON_AddArrayToObject(d, "radios");
    cJSON_AddStringToObject(d, "serial", v->serial);
    add_opt_str(d, "node_id", v->has_node_id, v->node_id);
    add_opt_str(d, "model", v->has_model, v->model);
    add_opt_str(d, "firmware", v->has_firmware, v->firmware);
    for (size_t i = 0; i < v->nradios; i++) {
        const em_radio_view *r = &v->radios[i];
        cJSON *jr = cJSON_CreateObject();
        add_mac(jr, "ruid", r->ruid);
        cJSON_AddStringToObject(jr, "if_name", r->if_name);
        cJSON_AddStringToObject(jr, "band", r->band);
        if (r->has_channel)
            cJSON_AddNumberToObject(jr, "channel", r->channel);
        else
            cJSON_AddNullToObject(jr, "channel");
        if (r->has_tx_power)
            cJSON_AddNumberToObject(jr, "tx_power", r->tx_power);
        else
            cJSON_AddNullToObject(jr, "tx_power");
        cJSON_AddBoolToObject(jr, "enabled", r->enabled);
        cJSON *bsses = cJSON_AddArrayToObject(jr, "bsses");
        for (size_t k = 0; k < r->nbss; k++) {
            const em_bss_view *b = &r->bss[k];
            cJSON *jb = cJSON_CreateObject(), *st = cJSON_CreateArray();
            cJSON_AddStringToObject(jb, "if_name", b->if_name);
            add_mac(jb, "bssid", b->bssid);
            cJSON_AddStringToObject(jb, "ssid", b->ssid);
            cJSON_AddStringToObject(jb, "role", b->backhaul ? "backhaul" : "fronthaul");
            for (size_t s = 0; s < b->nstations; s++) {
                char *text = em_mac_str(b->stations[s]);
                cJSON_AddItemToArray(st, cJSON_CreateString(text));
                free(text);
            }
            cJSON_AddItemToObject(jb, "stations", st);
            cJSON_AddItemToArray(bsses, jb);
        }
        cJSON *ups = cJSON_AddArrayToObject(jr, "uplinks");
        for (size_t k = 0; k < r->nuplinks; k++) {
            const em_uplink_view *u = &r->uplinks[k];
            cJSON *ju = cJSON_CreateObject();
            cJSON_AddStringToObject(ju, "if_name", u->if_name);
            add_mac(ju, "mac", u->mac);
            cJSON_AddStringToObject(ju, "ssid", u->ssid);
            if (u->has_parent)
                add_mac(ju, "parent", u->parent);
            else
                cJSON_AddNullToObject(ju, "parent");
            cJSON_AddBoolToObject(ju, "multi_ap", u->multi_ap);
            cJSON_AddItemToArray(ups, ju);
        }
        cJSON_AddItemToArray(radios, jr);
    }
    return d;
}

/* -- payloads --------------------------------------------------------------------- */

void em_tlv_list_free(em_tlv_list *l)
{
    for (size_t i = 0; i < l->count; i++)
        free(l->tlvs[i].value);
    l->count = 0;
}

/* Append a TLV taking the buffer's bytes. */
static bool append(em_tlv_list *l, uint8_t kind, em_buf *value)
{
    if (l->count == 32 || value->len > EM_MAX_VALUE) {
        em_buf_free(value);
        return false;
    }
    if (!value->data && !em_buf_put(value, "", 0))
        return false;
    l->tlvs[l->count++] = (em_tlv){kind, (uint16_t)value->len, value->data ? value->data : calloc(1, 1)};
    memset(value, 0, sizeof(*value));
    return true;
}

static bool append_bytes(em_tlv_list *l, uint8_t kind, const void *data, size_t len)
{
    em_buf b = {0};
    return em_buf_put(&b, data, len) && append(l, kind, &b);
}

em_reason em_capability_tlvs(const em_radio_view *r, int channel, uint8_t max_bss, int8_t max_eirp,
                             em_tlv_list *out)
{
    memset(out, 0, sizeof(*out));
    if (strcmp(r->band, "2.4G"))
        return EM_UNSUPPORTED_OPERATION; /* the only mapped band */
    if (!max_bss)
        return EM_INVALID_INPUT;
    uint8_t zero = 0, ruid_flags[7];
    memcpy(ruid_flags, r->ruid, 6);
    ruid_flags[6] = 0;
    em_buf basic = {0};
    bool ok = append_bytes(out, 0xA1, &zero, 1) && em_buf_put(&basic, r->ruid, 6) &&
              em_buf_u8(&basic, max_bss) && em_buf_u8(&basic, 1) && em_buf_u8(&basic, 81) &&
              em_buf_u8(&basic, (uint8_t)max_eirp) &&
              em_buf_u8(&basic, (uint8_t)(channel >= 1 && channel <= 13 ? 12 : 13));
    for (int c = 1; ok && c <= 13; c++)
        if (c != channel)
            ok = em_buf_u8(&basic, (uint8_t)c);
    static const uint8_t akm[6] = {0x00, 0x01, 0x00, 0x0f, 0xac, 0x02};
    static const uint8_t profile2[4] = {0, 0, 0, 0};
    static const uint8_t ciphers[5] = {0x01, 0x00, 0x0f, 0xac, 0x04};
    ok = ok && append(out, 0x85, &basic) && append_bytes(out, 0x86, ruid_flags, 7) &&
         append_bytes(out, 0xBE, ruid_flags, 7) && append_bytes(out, 0xCC, akm, 6) &&
         append_bytes(out, 0xB4, profile2, 4) && append_bytes(out, 0xED, ciphers, 5);
    em_buf_free(&basic);
    if (!ok) {
        em_tlv_list_free(out);
        return EM_NO_MEMORY;
    }
    return EM_OK;
}

static bool inventory_string(em_buf *b, const char *s, size_t max)
{
    size_t n = strlen(s) < max ? strlen(s) : max;
    return em_buf_u8(b, (uint8_t)n) && em_buf_put(b, s, n);
}

em_reason em_inventory_tlv(const em_device_view *d, const em_radio_view *r, const char *chipset,
                           em_tlv *out)
{
    em_buf b = {0};
    bool ok = inventory_string(&b, d->serial, 64) &&
              inventory_string(&b, d->has_firmware ? d->firmware : "unknown", 64) &&
              inventory_string(&b, "OpenSync pod via EMOSA", 64) && em_buf_u8(&b, 1) &&
              em_buf_put(&b, r->ruid, 6) && strlen(chipset) <= 64 && inventory_string(&b, chipset, 64);
    if (!ok) {
        em_buf_free(&b);
        return EM_INVALID_INPUT;
    }
    *out = (em_tlv){0xD4, (uint16_t)b.len, b.data};
    return EM_OK;
}

static long age_of(const em_station_age *ages, size_t n, const uint8_t mac[6], bool *found)
{
    for (size_t i = 0; i < n; i++)
        if (!memcmp(ages[i].mac, mac, 6)) {
            *found = true;
            return ages[i].seconds;
        }
    *found = false;
    return 0;
}

em_reason em_topology_tlvs(const uint8_t agent_al[6], const uint8_t controller_al[6],
                           const em_radio_view *r, int channel, const em_station_age *ages,
                           size_t nages, bool r1, const em_backhaul *uplink, em_tlv_list *out)
{
    size_t extra = uplink ? 1 : 0;
    memset(out, 0, sizeof(*out));
    em_buf dev = {0}, bridge = {0}, nb = {0}, op = {0}, conf = {0}, cl = {0};
    size_t clients = 0;
    /* Device Information: the agent's own port, then one AP interface per BSS */
    bool ok = em_buf_put(&dev, agent_al, 6) && em_buf_u8(&dev, (uint8_t)(1 + r->nbss + extra)) &&
              em_buf_put(&dev, agent_al, 6) && em_buf_u16(&dev, 0x0001) && em_buf_u8(&dev, 0);
    for (size_t i = 0; ok && i < r->nbss; i++) {
        uint8_t media[4] = {0x00, 0x00, (uint8_t)channel, 0x00};
        ok = em_buf_put(&dev, r->bss[i].bssid, 6) && em_buf_u16(&dev, 0x0103) &&
             em_buf_u8(&dev, 10) && em_buf_put(&dev, r->bss[i].bssid, 6) && em_buf_put(&dev, media, 4);
    }
    if (ok && uplink) {
        /* the backhaul STA: a non-AP STA interface on its parent BSSID */
        uint8_t media[4] = {0x40, 0x00, (uint8_t)uplink->channel, 0x00};
        ok = em_buf_put(&dev, uplink->station.mac, 6) &&
             em_buf_u16(&dev, strcmp(uplink->band, "5G") ? 0x0103 : 0x0104) && em_buf_u8(&dev, 10) &&
             em_buf_put(&dev, uplink->station.parent, 6) && em_buf_put(&dev, media, 4);
    }
    /* Bridging Capability: every interface in one tuple */
    ok = ok && em_buf_u8(&bridge, 1) && em_buf_u8(&bridge, (uint8_t)(1 + r->nbss + extra)) &&
         em_buf_put(&bridge, agent_al, 6);
    for (size_t i = 0; ok && i < r->nbss; i++)
        ok = em_buf_put(&bridge, r->bss[i].bssid, 6);
    if (ok && uplink)
        ok = em_buf_put(&bridge, uplink->station.mac, 6);
    /* 1905 Neighbor Device: the controller, on the agent's port */
    ok = ok && em_buf_put(&nb, agent_al, 6) && em_buf_put(&nb, controller_al, 6) && em_buf_u8(&nb, 0);
    ok = ok && em_buf_u8(&op, 1) && em_buf_put(&op, r->ruid, 6) && em_buf_u8(&op, (uint8_t)r->nbss);
    ok = ok && em_buf_u8(&conf, 1) && em_buf_put(&conf, r->ruid, 6) && em_buf_u8(&conf, (uint8_t)r->nbss);
    ok = ok && em_buf_u8(&cl, (uint8_t)r->nbss);
    for (size_t i = 0; ok && i < r->nbss; i++) {
        const em_bss_view *b = &r->bss[i];
        size_t slen = strlen(b->ssid);
        ok = slen <= 32 && em_buf_put(&op, b->bssid, 6) && em_buf_u8(&op, (uint8_t)slen) &&
             em_buf_put(&op, b->ssid, slen) && em_buf_put(&conf, b->bssid, 6) &&
             em_buf_u8(&conf, b->backhaul ? 0x80 : 0x40) && em_buf_u8(&conf, 0) &&
             em_buf_u8(&conf, (uint8_t)slen) && em_buf_put(&conf, b->ssid, slen) &&
             em_buf_put(&cl, b->bssid, 6) && em_buf_u16(&cl, (uint16_t)b->nstations);
        for (size_t s = 0; ok && s < b->nstations; s++) {
            bool found;
            long age = age_of(ages, nages, b->stations[s], &found);
            if (!found || age < 0)
                ok = false;
            else
                ok = em_buf_put(&cl, b->stations[s], 6) && em_buf_u16(&cl, (uint16_t)(age > 65535 ? 65535 : age));
            clients++;
        }
    }
    static const uint8_t services[2] = {1, 1}, profile = 1;
    ok = ok && append(out, 0x03, &dev) && append(out, 0x04, &bridge) && append(out, 0x07, &nb) &&
         append_bytes(out, 0x80, services, 2) && append(out, 0x83, &op) &&
         (r1 || append(out, 0xB7, &conf)) && (!clients || append(out, 0x84, &cl)) &&
         (r1 || append_bytes(out, 0xB3, &profile, 1));
    em_buf_free(&dev);
    em_buf_free(&bridge);
    em_buf_free(&nb);
    em_buf_free(&op);
    em_buf_free(&conf);
    em_buf_free(&cl);
    if (!ok) {
        em_tlv_list_free(out);
        return EM_INVALID_INPUT;
    }
    return EM_OK;
}

/* -- the uplink (spec §8.3) --------------------------------------------------------- */

bool em_view_backhaul(const em_device_view *v, const char *station, em_backhaul *out)
{
    for (size_t i = 0; i < v->nradios; i++) {
        const em_radio_view *r = &v->radios[i];
        for (size_t k = 0; k < r->nuplinks; k++) {
            const em_uplink_view *u = &r->uplinks[k];
            if (strcmp(u->if_name, station) || !u->multi_ap || !u->has_parent || !r->has_channel ||
                (strcmp(r->band, "2.4G") && strcmp(r->band, "5G")))
                continue;
            memcpy(out->ruid, r->ruid, 6);
            strcpy(out->band, r->band);
            out->channel = r->channel;
            out->station = *u;
            return true;
        }
    }
    return false;
}

cJSON *em_backhaul_json(const em_backhaul *b)
{
    cJSON *o = cJSON_CreateObject(), *s = cJSON_CreateObject();
    add_mac(o, "ruid", b->ruid);
    cJSON_AddStringToObject(o, "band", b->band);
    cJSON_AddNumberToObject(o, "channel", b->channel);
    cJSON_AddStringToObject(s, "if_name", b->station.if_name);
    add_mac(s, "mac", b->station.mac);
    cJSON_AddStringToObject(s, "ssid", b->station.ssid);
    add_mac(s, "parent", b->station.parent);
    cJSON_AddBoolToObject(s, "multi_ap", b->station.multi_ap);
    cJSON_AddItemToObject(o, "station", s);
    return o;
}

static void lower_copy(char *dst, size_t cap, const char *src)
{
    size_t i = 0;
    for (; src && src[i] && i + 1 < cap; i++)
        dst[i] = (char)(src[i] >= 'A' && src[i] <= 'F' ? src[i] + 32 : src[i]);
    dst[i] = 0;
}

void em_uplink_state_of(const cJSON *tables, const char *station, em_uplink_state *out)
{
    memset(out, 0, sizeof(*out));
    const cJSON *row, *used = NULL, *state = NULL;
    int nused = 0, nstates = 0;
    cJSON_ArrayForEach(row, cJSON_GetObjectItemCaseSensitive(tables, "Connection_Manager_Uplink"))
    {
        if (ovs_true(row, "is_used")) {
            nused++;
            used = row;
        }
    }
    cJSON_ArrayForEach(row, cJSON_GetObjectItemCaseSensitive(tables, "Wifi_VIF_State"))
    {
        const char *name = ovs_str(row, "if_name"), *mode = ovs_str(row, "mode");
        if (name && !strcmp(name, station) && mode && !strcmp(mode, "sta")) {
            nstates++;
            state = row;
        }
    }
    if (nstates != 1)
        state = NULL;
    const char *multi_ap = state ? ovs_str(state, "multi_ap") : NULL;
    bool wds_sta = state && ovs_true(state, "enabled") && multi_ap &&
                   !strcmp(multi_ap, "backhaul_sta") && ovs_true(state, "wds");
    const char *in_use = nused == 1 ? ovs_str(used, "if_name") : NULL;
    if (wds_sta && in_use && !strcmp(in_use, station))
        strcpy(out->kind, "multi-ap");
    else if (nused == 1 && ovs_str(used, "if_type"))
        copy_str(out->kind, sizeof(out->kind), ovs_str(used, "if_type"));
    copy_str(out->in_use, sizeof(out->in_use), in_use);
    copy_str(out->station, sizeof(out->station), station);
    if (state) {
        copy_str(out->ssid, sizeof(out->ssid), ovs_str(state, "ssid"));
        lower_copy(out->parent, sizeof(out->parent), ovs_str(state, "parent"));
        copy_str(out->mac, sizeof(out->mac), ovs_str(state, "mac"));
    }
}

static void add_or_null(cJSON *o, const char *key, const char *value)
{
    if (*value)
        cJSON_AddStringToObject(o, key, value);
    else
        cJSON_AddNullToObject(o, key);
}

cJSON *em_uplink_state_json(const em_uplink_state *u)
{
    cJSON *o = cJSON_CreateObject();
    add_or_null(o, "kind", u->kind);
    add_or_null(o, "in_use", u->in_use);
    cJSON_AddStringToObject(o, "station", u->station);
    add_or_null(o, "ssid", u->ssid);
    add_or_null(o, "parent", u->parent);
    add_or_null(o, "mac", u->mac);
    return o;
}
