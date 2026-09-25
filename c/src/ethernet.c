/* The agent's 1905 packet endpoint. Mirrors emosa.wire.ethernet. */
#include "ethernet.h"

#include <arpa/inet.h>
#include <errno.h>
#include <linux/if_packet.h>
#include <net/ethernet.h>
#include <net/if.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "cmdu.h"

em_reason em_ethernet_open(em_ethernet *e, const char *interface, const uint8_t local[6])
{
    e->fd = -1;
    memcpy(e->local, local, 6);
    unsigned index = if_nametoindex(interface);
    if (!index || (local[0] & 1))
        return EM_INVALID_INPUT;
    int fd = socket(AF_PACKET, SOCK_RAW | SOCK_NONBLOCK | SOCK_CLOEXEC, htons(EM_ETHERTYPE));
    if (fd < 0)
        return EM_NOT_READY;
    struct sockaddr_ll sa = {.sll_family = AF_PACKET, .sll_protocol = htons(EM_ETHERTYPE),
                             .sll_ifindex = (int)index};
    struct sockaddr_ll bound;
    socklen_t blen = sizeof(bound);
    if (bind(fd, (struct sockaddr *)&sa, sizeof(sa)) ||
        getsockname(fd, (struct sockaddr *)&bound, &blen) || bound.sll_hatype != 1 ||
        bound.sll_halen != 6 || memcmp(bound.sll_addr, local, 6)) {
        close(fd);
        return EM_INVALID_INPUT; /* must be Ethernet with the declared local MAC */
    }
    struct packet_mreq m = {.mr_ifindex = (int)index, .mr_type = PACKET_MR_MULTICAST, .mr_alen = 6};
    memcpy(m.mr_address, EM_MULTICAST, 6);
    if (setsockopt(fd, SOL_PACKET, PACKET_ADD_MEMBERSHIP, &m, sizeof(m))) {
        close(fd);
        return EM_NOT_READY;
    }
    e->fd = fd;
    return EM_OK;
}

void em_ethernet_close(em_ethernet *e)
{
    if (e->fd >= 0)
        close(e->fd);
    e->fd = -1;
}

size_t em_ethernet_receive(em_ethernet *e, uint8_t *frame, size_t cap)
{
    for (;;) {
        struct sockaddr_ll from;
        socklen_t flen = sizeof(from);
        ssize_t n = recvfrom(e->fd, frame, cap, 0, (struct sockaddr *)&from, &flen);
        if (n < 0)
            return 0;
        if (from.sll_pkttype == PACKET_OUTGOING || n < 22)
            continue;
        if (memcmp(frame, EM_MULTICAST, 6) && memcmp(frame, e->local, 6))
            continue;
        return (size_t)n;
    }
}

em_reason em_ethernet_send(em_ethernet *e, const uint8_t *frame, size_t len)
{
    if (len < 22 || memcmp(frame + 6, e->local, 6) || frame[14] != 0)
        return EM_INVALID_INPUT;
    ssize_t n = send(e->fd, frame, len, 0);
    return n == (ssize_t)len ? EM_OK : EM_OUTCOME_UNKNOWN;
}
