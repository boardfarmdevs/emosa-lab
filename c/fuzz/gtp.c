/* SPDX-License-Identifier: Apache-2.0 */
/* Fuzz target: what reaches the GTP from outside (gtp.c): a lease event's arguments
 * (dnsmasq passes on what a DHCP client sent), dnsmasq's lease file, and iproute2's
 * output. The input is three parts split by NUL bytes: the output every ip call gives,
 * the lease file, and the event's ACTION MAC IP separated by spaces. Each part may be
 * empty or missing. The commands run against that output and no real link. */
#include "gtp.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

static const char *ip_output;

static char *fake_ip(void *ctx, const char *const *args, size_t n, bool check)
{
    (void)ctx;
    (void)args;
    (void)n;
    (void)check;
    return em_strdup(ip_output);
}

/* the next part of the input (NUL-terminated copies, the caller frees) */
static char *part(const uint8_t *data, size_t size, size_t *at)
{
    size_t start = *at, end = start;
    while (end < size && data[end])
        end++;
    char *out = em_malloc(end - start + 1);
    memcpy(out, data + start, end - start);
    out[end - start] = 0;
    *at = end < size ? end + 1 : end;
    return out;
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    static char state[64];
    static cJSON *config;
    if (!config) {
        em_init();
        if (!em_copy(state, sizeof(state), "/tmp/emosa-fuzz-gtp-XXXXXX") || !mkdtemp(state))
            abort();
        config = cJSON_Parse("{\"underlay\": {\"interface\": \"podbh\", \"address\": \"169.254.2.1/25\","
                             " \"mtu\": 1600, \"dhcp_range\": [\"169.254.2.10\", \"169.254.2.126\"],"
                             " \"lease_time\": \"1h\"}, \"lan\": {\"bridge\": \"br-gtp\", \"ports\": [\"eth1\"]},"
                             " \"tunnel_mtu\": 1562}");
        cJSON_AddStringToObject(config, "state_dir", state);
    }
    size_t at = 0;
    char *output = part(data, size, &at), *leases = part(data, size, &at), *event = part(data, size, &at);
    ip_output = output;
    char path[128];
    EM_FORMAT_FIXED(path, sizeof(path), "%s/leases", state);
    (void)em_write_file(path, leases, false);
    em_gtp g;
    char why[512];
    if (em_gtp_init(&g, config, (em_ip){fake_ip, NULL}, why, sizeof(why))) {
        cJSON_Delete(em_gtp_list(&g, why, sizeof(why)));
        cJSON_Delete(em_gtp_reconcile(&g, why, sizeof(why)));
        char *words[3] = {"", "", ""}, *save = NULL;
        char *word = strtok_r(event, " ", &save);
        for (int i = 0; i < 3 && word; i++, word = strtok_r(NULL, " ", &save))
            words[i] = word;
        cJSON_Delete(em_gtp_lease(&g, words[0], words[1], words[2], why, sizeof(why)));
    }
    char name[16];
    (void)em_gtp_tunnel_name(event, name);
    free(output);
    free(leases);
    free(event);
    return 0;
}
