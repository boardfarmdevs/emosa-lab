/* SPDX-License-Identifier: Apache-2.0 */
/* The other agents of the fleet, as their statuses give them (spec §8.5; emosa.agent.peers).
 *
 * A pod on its EasyMesh backhaul may have another pod's backhaul BSS as its upstream. The agents
 * then describe that hop to the controller: the child names its parent's agent as its 1905
 * neighbor on the backhaul, the parent names the child on its backhaul BSS, and both answer a
 * Link Metric Query for the pair from the parent's measurement of the child's station. No 1905
 * frame tells an agent about another pod, so each reads the others' statuses in the fleet's run
 * root (read-only), at most once a second, leaving out an agent whose process is gone. */
#ifndef EM_PEERS_H
#define EM_PEERS_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <cjson/cJSON.h>

#define EM_PEERS_MAX 32
#define EM_PEER_BACKHAULS 8

typedef struct {
    char pod_id[64];
    uint8_t al[6];                               /* its agent's AL MAC */
    uint8_t backhaul[EM_PEER_BACKHAULS][6];       /* its backhaul BSSes' BSSIDs */
    size_t nbackhaul;
    bool has_station;                            /* on its EasyMesh backhaul: */
    uint8_t station[6], parent[6];               /* its station, and that station's parent */
    cJSON *measured;                             /* its pod's stations' measurements, owned */
} em_peer;

typedef struct {
    char root[512], pod_id[64];
    bool read;
    double read_at;
    em_peer peers[EM_PEERS_MAX];
    size_t n;
    bool (*alive)(long pid); /* NULL: kill(pid, 0) */
} em_peers;

/* The directory of the agents beside run_dir (its parent directory), this pod left out. */
void em_peers_init(em_peers *d, const char *run_dir, const char *pod_id);
/* The peers, read again when the last read is a second old (now: monotonic seconds). */
size_t em_peers_read(em_peers *d, double now);
/* The peer one of whose backhaul BSSes is parent, or NULL. */
const em_peer *em_peers_upstream(const em_peers *d, const uint8_t parent[6]);
/* True when an upstream target would put a loop into br-home: one of the own BSSes, or a BSS of a
 * pod whose upstream chain reaches this pod (own_al) or runs in a circle. */
bool em_peers_loops(const em_peers *d, const uint8_t own_al[6], const uint8_t (*own)[6], size_t nown,
                    const uint8_t target[6]);
void em_peers_free(em_peers *d);

#endif
