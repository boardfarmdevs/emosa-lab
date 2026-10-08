/* SPDX-License-Identifier: Apache-2.0 */
#include "peers.h"

#include <dirent.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>

#include "common.h"

#define REFRESH 1.0 /* seconds between two reads of the run root */

static bool process_alive(long pid)
{
    return pid > 0 && (kill((pid_t)pid, 0) == 0 || errno == EPERM);
}

void em_peers_init(em_peers *d, const char *run_dir, const char *pod_id)
{
    memset(d, 0, sizeof(*d));
    EM_FORMAT_FIXED(d->root, sizeof(d->root), "%s", run_dir);
    char *slash = strrchr(d->root, '/');
    if (slash && slash != d->root)
        *slash = 0;
    else
        EM_FORMAT_FIXED(d->root, sizeof(d->root), "%s", ".");
    EM_FORMAT_FIXED(d->pod_id, sizeof(d->pod_id), "%s", pod_id);
}

static void clear(em_peers *d)
{
    for (size_t i = 0; i < d->n; i++)
        cJSON_Delete(d->peers[i].measured);
    d->n = 0;
}

void em_peers_free(em_peers *d) { clear(d); }

static const char *text(const cJSON *o, const char *key)
{
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(o, key));
}

/* the peer a status describes; false when it names no AL MAC */
static bool peer(const char *pod_id, const cJSON *status, em_peer *out)
{
    memset(out, 0, sizeof(*out));
    const char *al = text(status, "agent_al");
    if (!al || !em_parse_mac(al, out->al))
        return false;
    const char *named = text(status, "pod_id");
    EM_FORMAT_FIXED(out->pod_id, sizeof(out->pod_id), "%s", named ? named : pod_id);
    const cJSON *pod = cJSON_GetObjectItemCaseSensitive(status, "pod"), *b;
    cJSON_ArrayForEach(b, cJSON_GetObjectItemCaseSensitive(pod, "bsses"))
    {
        const char *role = text(b, "role"), *bssid = text(b, "bssid");
        if (role && !strcmp(role, "backhaul") && bssid && out->nbackhaul < EM_PEER_BACKHAULS &&
            em_parse_mac(bssid, out->backhaul[out->nbackhaul]))
            out->nbackhaul++;
    }
    const cJSON *backhaul = cJSON_GetObjectItemCaseSensitive(pod, "backhaul");
    const char *station = text(backhaul, "mac"), *parent = text(backhaul, "parent");
    out->has_station = station && parent && em_parse_mac(station, out->station) && em_parse_mac(parent, out->parent);
    const cJSON *stations =
        cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(status, "telemetry"), "stations");
    out->measured = cJSON_IsObject(stations) ? cJSON_Duplicate(stations, true) : cJSON_CreateObject();
    return true;
}

static int by_name(const void *x, const void *y) { return strcmp(*(char *const *)x, *(char *const *)y); }

size_t em_peers_read(em_peers *d, double now)
{
    if (d->read && now - d->read_at < REFRESH)
        return d->n;
    clear(d);
    d->read = true;
    d->read_at = now;
    DIR *dir = opendir(d->root);
    if (!dir)
        return 0;
    /* the agents in name order, as the reference reads them */
    char *names[EM_PEERS_MAX * 2];
    size_t count = 0;
    struct dirent *e;
    while ((e = readdir(dir)) && count < EM_PEERS_MAX * 2) {
        if (e->d_name[0] == '.' || !strcmp(e->d_name, d->pod_id))
            continue;
        names[count] = strdup(e->d_name);
        if (names[count])
            count++;
    }
    closedir(dir);
    qsort(names, count, sizeof(names[0]), by_name);
    for (size_t i = 0; i < count; i++) {
        char path[1024];
        cJSON *status = NULL;
        if (d->n < EM_PEERS_MAX && em_format(path, sizeof(path), "%s/%s/status.json", d->root, names[i])) {
            char *body = em_read_file(path, 1 << 20, NULL);
            status = body ? cJSON_Parse(body) : NULL;
            free(body);
        }
        const cJSON *pid = cJSON_GetObjectItemCaseSensitive(status, "worker_pid");
        bool live = cJSON_IsNumber(pid) && (d->alive ? d->alive((long)pid->valuedouble)
                                                     : process_alive((long)pid->valuedouble));
        if (status && live && peer(names[i], status, &d->peers[d->n]))
            d->n++;
        cJSON_Delete(status);
        free(names[i]);
    }
    return d->n;
}

static bool among(const uint8_t (*set)[6], size_t n, const uint8_t mac[6])
{
    for (size_t i = 0; i < n; i++)
        if (!memcmp(set[i], mac, 6))
            return true;
    return false;
}

const em_peer *em_peers_upstream(const em_peers *d, const uint8_t parent[6])
{
    for (size_t i = 0; i < d->n; i++)
        if (among((const uint8_t(*)[6])d->peers[i].backhaul, d->peers[i].nbackhaul, parent))
            return &d->peers[i];
    return NULL;
}

bool em_peers_loops(const em_peers *d, const uint8_t own_al[6], const uint8_t (*own)[6], size_t nown,
                    const uint8_t target[6])
{
    uint8_t seen[EM_PEERS_MAX][6], bssid[6];
    size_t nseen = 0;
    memcpy(bssid, target, 6);
    for (;;) {
        if (among(own, nown, bssid))
            return true;
        const em_peer *owner = em_peers_upstream(d, bssid);
        if (!owner)
            return false;
        if (!memcmp(owner->al, own_al, 6) || among((const uint8_t(*)[6])seen, nseen, owner->al) ||
            nseen == EM_PEERS_MAX)
            return true;
        memcpy(seen[nseen++], owner->al, 6);
        if (!owner->has_station)
            return false;
        memcpy(bssid, owner->parent, 6);
    }
}
