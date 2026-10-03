/* emosa-gtp-c: the GRE termination point (spec §8.2) in C, as emosa.gtp's main.
 *
 *   emosa-gtp-c setup CONFIG                       addresses, bridge, dnsmasq configuration, reconcile
 *   emosa-gtp-c lease CONFIG ACTION MAC IP [HOST]  dnsmasq --dhcp-script: add, old, del
 *   emosa-gtp-c reconcile CONFIG                   tunnels = current leases
 *   emosa-gtp-c list CONFIG
 *
 * Each prints the tunnels as JSON; a refusal is printed to stderr with exit code 1. */
#include <cjson/cJSON.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "canon.h"
#include "gtp.h"
#include "jschema.h"
#include "proc.h"

#define CONFIG_MAX (1024 * 1024)

/* iproute2, as the reference's Links._run: output captured, a checked failure refused */
static char *run_ip(void *ctx, const char *const *args, size_t n, bool check)
{
    (void)ctx;
    const char **argv = em_calloc(n + 2, sizeof(*argv));
    argv[0] = "ip";
    memcpy(argv + 1, args, n * sizeof(*args));
    char *out = NULL, *err = NULL;
    int status = -1;
    bool ran = em_run(argv, &out, &err, &status);
    free(argv);
    if (!ran) {
        (void)fprintf(stderr, "WARNING emosa.gtp: ip could not be started\n");
        return check ? NULL : em_strdup("");
    }
    if (check && status != 0) { /* its own words on stderr: the reason */
        (void)fprintf(stderr, "WARNING emosa.gtp: ip %s: %s", args[0], *err ? err : "failed\n");
        free(out);
        out = NULL;
    }
    free(err);
    return out;
}

static cJSON *load(const char *path)
{
    char *text = em_read_file(path, CONFIG_MAX, NULL);
    cJSON *config = text ? cJSON_Parse(text) : NULL;
    free(text);
    if (!config) {
        (void)fprintf(stderr, "%s: not a JSON GTP configuration\n", path);
        return NULL;
    }
    em_schema *schema = em_schema_load(em_schema_directory(), "gtp-config");
    char why[256] = "";
    bool valid = schema && em_schema_valid(schema, config, why, sizeof(why));
    em_schema_free(schema);
    if (!valid) {
        (void)fprintf(stderr, "%s: invalid GTP configuration (%s)\n", path, schema ? why : "no schema");
    } else if (!em_gtp_check(config, why, sizeof(why))) {
        (void)fprintf(stderr, "%s: %s\n", path, why);
        valid = false;
    }
    if (!valid) {
        cJSON_Delete(config);
        return NULL;
    }
    return config;
}

static int usage(void)
{
    (void)fprintf(stderr, "usage: emosa-gtp-c setup|lease|reconcile|list CONFIG [ACTION MAC IP [HOST]]\n");
    return 2;
}

int main(int argc, char **argv)
{
    em_init();
    if (argc < 3)
        return usage();
    const char *command = argv[1];
    if (strcmp(command, "setup") && strcmp(command, "lease") && strcmp(command, "reconcile") &&
        strcmp(command, "list"))
        return usage();
    if (!strcmp(command, "lease")) {
        if (argc < 6) {
            (void)fprintf(stderr, "lease needs ACTION MAC IP\n");
            return 2;
        }
        if (strcmp(argv[3], "add") && strcmp(argv[3], "old") && strcmp(argv[3], "del"))
            return 0; /* dnsmasq also reports tftp and init events */
    } else if (argc != 3) {
        return usage();
    }
    cJSON *config = load(argv[2]);
    if (!config)
        return 1;
    em_gtp g;
    char why[512] = "";
    cJSON *result = NULL;
    if (em_gtp_init(&g, config, (em_ip){run_ip, NULL}, why, sizeof(why))) {
        if (!strcmp(command, "setup")) {
            char config_path[PATH_MAX], exe[PATH_MAX];
            ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
            if (n <= 0 || !realpath(argv[2], config_path))
                (void)em_format(why, sizeof(why), "cannot name this program or %s for dnsmasq's hook", argv[2]);
            else {
                exe[n] = 0;
                result = em_gtp_setup(&g, config_path, exe, why, sizeof(why));
            }
        } else if (!strcmp(command, "lease")) {
            result = em_gtp_lease(&g, argv[3], argv[4], argv[5], why, sizeof(why));
        } else if (!strcmp(command, "reconcile")) {
            result = em_gtp_reconcile(&g, why, sizeof(why));
        } else {
            result = em_gtp_list(&g, why, sizeof(why));
        }
    }
    int rc = 0;
    if (result) {
        char *text = em_json_dumps(result, EM_JSON_INDENT2, true);
        if (text)
            (void)printf("%s\n", text);
        free(text);
    } else {
        (void)fprintf(stderr, "emosa-gtp: %s\n", why);
        rc = 1;
    }
    cJSON_Delete(result);
    cJSON_Delete(config);
    return rc;
}
