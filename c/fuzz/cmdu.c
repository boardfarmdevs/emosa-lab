/* Fuzz target: IEEE 1905 frames from the LAN, which any host on it can send
 * (cmdu.c reassembly, autoconf.c Response parsing). The input is a sequence of
 * frames, each after its length (two bytes, big endian), fed to one reassembler
 * with the agent's limits (agent.c). */
#include "autoconf.h"
#include "cmdu.h"

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

static double fixed_clock(void *ctx)
{
    (void)ctx;
    return 1000.0;
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    static bool ready;
    if (!ready) {
        em_init();
        ready = true;
    }
    em_reassembler *r = em_reassembler_new(fixed_clock, NULL, 5.0, 8, 65536, 16384, 16);
    while (size >= 2) {
        size_t len = (size_t)data[0] << 8 | data[1];
        data += 2;
        size -= 2;
        if (len > size)
            len = size;
        em_message *m = NULL;
        if (em_reassembler_feed(r, data, len, "fuzz", &m) == EM_OK && m) {
            em_advertisement ad;
            em_parse_response(m, EM_SET_61, &ad);
            em_parse_response(m, EM_SET_R1, &ad);
            em_message_free(m);
        }
        data += len;
        size -= len;
    }
    em_reassembler_free(r);
    return 0;
}
