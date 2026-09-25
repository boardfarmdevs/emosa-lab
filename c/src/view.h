/* The pod as EasyMesh sees it (spec §3.3), as emosa.opensync.easymesh_view. */
#ifndef EMOSA_VIEW_H
#define EMOSA_VIEW_H

#include <cjson/cJSON.h>

#include "cmdu.h"

#define EM_MAX_RADIOS 8
#define EM_MAX_BSS 16
#define EM_MAX_STATIONS 64
#define EM_MAX_UPLINKS 4

typedef struct {
    char if_name[33];
    uint8_t bssid[6];
    char ssid[65];
    bool backhaul; /* role: multi_ap = backhaul_bss */
    size_t nstations;
    uint8_t stations[EM_MAX_STATIONS][6]; /* sorted */
} em_bss_view;

typedef struct {
    char if_name[33];
    uint8_t mac[6];
    char ssid[65];
    bool has_parent;
    uint8_t parent[6];
    bool multi_ap;
} em_uplink_view;

typedef struct {
    uint8_t ruid[6];
    char if_name[33];
    char band[8];
    bool has_channel, has_tx_power, enabled;
    int channel, tx_power;
    size_t nbss, nuplinks;
    em_bss_view bss[EM_MAX_BSS];       /* sorted by if_name */
    em_uplink_view uplinks[EM_MAX_UPLINKS];
} em_radio_view;

typedef struct {
    char serial[65];
    char node_id[65], model[65], firmware[65];
    bool has_node_id, has_model, has_firmware;
    size_t nradios;
    em_radio_view radios[EM_MAX_RADIOS]; /* sorted by RUID */
} em_device_view;

/* From raw RFC 7047 tables ({table: {uuid: row}}). */
em_reason em_device_view_from_rows(const cJSON *tables, em_device_view *out);
const em_radio_view *em_view_radio(const em_device_view *v, const uint8_t ruid[6]);
cJSON *em_device_view_json(const em_device_view *v);

/* An ordered TLV list (owned values). */
typedef struct {
    size_t count;
    em_tlv tlvs[32];
} em_tlv_list;

void em_tlv_list_free(em_tlv_list *l);

/* AP Capability Report contents for one radio on one channel (spec §3.3). */
em_reason em_capability_tlvs(const em_radio_view *r, int channel, uint8_t max_bss, int8_t max_eirp,
                             em_tlv_list *out);
/* The Device Inventory TLV. */
em_reason em_inventory_tlv(const em_device_view *d, const em_radio_view *r, const char *chipset,
                           em_tlv *out);

typedef struct {
    uint8_t mac[6];
    long seconds;
} em_station_age;

/* The pod's EasyMesh backhaul (data plane option 1): its station on which radio. */
typedef struct {
    uint8_t ruid[6];
    char band[8];
    int channel;
    em_uplink_view station;
} em_backhaul;

/* The backhaul through `station`, when it is a connected Multi-AP backhaul STA. */
bool em_view_backhaul(const em_device_view *v, const char *station, em_backhaul *out);
cJSON *em_backhaul_json(const em_backhaul *b);

/* Topology Response TLVs for the radio's BSSes (6.1 or r1); uplink may be NULL. */
em_reason em_topology_tlvs(const uint8_t agent_al[6], const uint8_t controller_al[6],
                           const em_radio_view *r, int channel, const em_station_age *ages,
                           size_t nages, bool r1, const em_backhaul *uplink, em_tlv_list *out);

/* How the pod reaches its gateway, from cm's and owm's State (spec §8.3). */
typedef struct {
    char kind[16]; /* "multi-ap", cm's if_type ("gre", "eth", ...), or "" for none */
    char in_use[33], station[33], ssid[65], parent[18], mac[18]; /* "" for none */
} em_uplink_state;

void em_uplink_state_of(const cJSON *tables, const char *station, em_uplink_state *out);
cJSON *em_uplink_state_json(const em_uplink_state *u);

#endif
