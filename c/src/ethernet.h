/* SPDX-License-Identifier: Apache-2.0 */
/* The agent's 1905 packet endpoint (spec §3.3 I3), as emosa.wire.ethernet: one Linux
 * packet socket on the agent's own interface, EtherType 0x893A, 1905 multicast joined. */
#ifndef EMOSA_ETHERNET_H
#define EMOSA_ETHERNET_H

#include "common.h"

typedef struct {
    int fd;
    uint8_t local[6];
} em_ethernet;

em_reason em_ethernet_open(em_ethernet *e, const char *interface, const uint8_t local[6]);
/* The same, the interface in network namespace netns (a name under /run/netns, or a path such
 * as /proc/PID/ns/net; NULL or empty: this one): the socket is opened there and the calling
 * thread returns to its own namespace. On a gateway whose EasyMesh controller is local, the
 * agents' interfaces, each with its agent's AL MAC, live in a namespace of their own: RDK's
 * controller takes an agent whose AL MAC is the MAC of one of its own interfaces for its
 * co-located agent (rdk-1004, 5 October 2026). */
em_reason em_ethernet_open_in(em_ethernet *e, const char *netns, const char *interface, const uint8_t local[6]);
void em_ethernet_close(em_ethernet *e);
/* One frame for this agent (unicast to it, or 1905 multicast); 0 when none is waiting. */
size_t em_ethernet_receive(em_ethernet *e, uint8_t *frame, size_t cap);
em_reason em_ethernet_send(em_ethernet *e, const uint8_t *frame, size_t len);

#endif
