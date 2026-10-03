/* The GRE termination point (spec §8.2), as emosa.gtp: the gateway end of every pod's
 * OpenSync GRE. It owns .1 of a link-local underlay, serves DHCP there with dnsmasq
 * (whose lease events run it), and keeps one gretap per leased pod, bridged into the
 * gateway's LAN. The links are changed through iproute2 (em_ip), which the vectors
 * replace with a recording. */
#ifndef EMOSA_GTP_H
#define EMOSA_GTP_H

#include <cjson/cJSON.h>

#include "common.h"

#define EM_GRETAP_OVERHEAD 38 /* outer IPv4 20 + GRE 4 + inner Ethernet 14 */

/* `ip ARGS...`: its standard output (the caller frees it), or NULL when it failed and
 * check was set (a failure without check gives its output, usually empty). */
typedef struct {
    char *(*run)(void *ctx, const char *const *args, size_t n, bool check);
    void *ctx;
} em_ip;

/* The rules JSON Schema cannot state: a link-local underlay, the GTP its first host,
 * the DHCP range inside it above .1, room for the gretap's 38 bytes. False with the
 * reference's words in why. */
bool em_gtp_check(const cJSON *config, char *why, size_t size);

/* gtp<third>_<fourth> octet (mv3's pgd<b3>_<b4>): at most 15 characters. */
bool em_gtp_tunnel_name(const char *ip, char out[16]);

/* dnsmasq's configuration for config (caller frees). */
char *em_gtp_dnsmasq(const cJSON *config);
/* The lease hook dnsmasq runs: exe lease CONFIG_PATH "$@" (caller frees). */
char *em_gtp_hook(const char *exe, const char *config_path);

typedef struct {
    em_ip ip;
    const cJSON *config;
    char underlay[16], bridge[16], local[16];
    uint32_t network, mask; /* the underlay subnet, host order */
    int tunnel_mtu;
    char state_dir[512];
} em_gtp;

/* config: schema-valid. False (why) when its addresses do not parse. */
bool em_gtp_init(em_gtp *g, const cJSON *config, em_ip ip, char *why, size_t size);

/* Each command's result: the tunnels, {name: {"remote", "mac"}} (caller owns it), or
 * NULL with the refusal in why. */
cJSON *em_gtp_setup(em_gtp *g, const char *config_path, const char *exe, char *why, size_t size);
cJSON *em_gtp_lease(em_gtp *g, const char *action, const char *mac, const char *ip, char *why, size_t size);
cJSON *em_gtp_reconcile(em_gtp *g, char *why, size_t size);
cJSON *em_gtp_list(em_gtp *g, char *why, size_t size);

#endif
