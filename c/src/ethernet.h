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
void em_ethernet_close(em_ethernet *e);
/* One frame for this agent (unicast to it, or 1905 multicast); 0 when none is waiting. */
size_t em_ethernet_receive(em_ethernet *e, uint8_t *frame, size_t cap);
em_reason em_ethernet_send(em_ethernet *e, const uint8_t *frame, size_t len);

#endif
