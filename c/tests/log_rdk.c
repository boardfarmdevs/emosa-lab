/* SPDX-License-Identifier: Apache-2.0 */
/* The log's RDK backend (log.c built with EMOSA_RDK_LOGGER) against a stub of rdk-logger
 * (tests/rdk/rdk_logger.h): which file each program logs to, the module, the levels, the
 * pod shown with each message, a long message cut, stderr when the logger refuses.
 *
 *   emosa-log-rdk */
#include <rdk_logger.h>
#include <stdio.h>
#include <string.h>

#include "common.h"
#include "log.h"

static int checks, failures;

#define CHECK(cond, ...)                                                                                           \
    do {                                                                                                           \
        checks++;                                                                                                  \
        if (!(cond)) {                                                                                             \
            failures++;                                                                                            \
            fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__);                                                   \
            fprintf(stderr, __VA_ARGS__);                                                                          \
            fputc('\n', stderr);                                                                                   \
        }                                                                                                          \
    } while (0)

/* -- the stub: what the logger was given ------------------------------------------------ */

static rdk_Error init_result = RDK_SUCCESS;
static int inits, messages;
static rdk_logger_ext_config_t last_config;
static rdk_LogLevel last_level;
static char last_module[64], last_text[8192];

rdk_Error rdk_logger_ext_init(const rdk_logger_ext_config_t *config)
{
    inits++;
    last_config = *config;
    return init_result;
}

void rdk_logger_msg_printf(rdk_LogLevel level, const char *module, const char *format, ...)
{
    messages++;
    last_level = level;
    (void)snprintf(last_module, sizeof(last_module), "%s", module);
    va_list ap;
    va_start(ap, format);
    (void)vsnprintf(last_text, sizeof(last_text), format, ap);
    va_end(ap);
}

/* -- the checks ------------------------------------------------------------------------- */

static void file_names(void)
{
    static const struct {
        const char *program, *instance, *expected;
    } cases[] = {
        {"agent", "MVXPOD023F87E628DD", "EMOSA_MVXPOD023F87E628DD.txt"},
        {"agent", "pod-1.a_b", "EMOSA_pod-1.a_b.txt"},
        {"agent", "123456789012345678901", "EMOSA_123456789012345678901.txt"}, /* 21: fits 31 */
        {"agent", "1234567890123456789012", "EMOSAAgentLog.txt"},              /* 22: too long */
        {"agent", "../etc", "EMOSAAgentLog.txt"},
        {"agent", "a/b", "EMOSAAgentLog.txt"},
        {"agent", ".hidden", "EMOSAAgentLog.txt"},
        {"agent", "a b", "EMOSAAgentLog.txt"},
        {"agent", "", "EMOSAAgentLog.txt"},
        {"fleet", NULL, "EMOSAFleetLog.txt"},
        {"gtp", NULL, "EMOSAGtpLog.txt"},
        {"GTP", NULL, "EMOSAGtpLog.txt"},
        {"fleet-2", NULL, "EMOSALog.txt"},
        {"abcdefghijklm", NULL, "EMOSALog.txt"},
        {NULL, NULL, "EMOSALog.txt"},
    };
    for (size_t i = 0; i < sizeof(cases) / sizeof(*cases); i++) {
        char name[32];
        em_log_file_name(cases[i].program, cases[i].instance, name);
        CHECK(!strcmp(name, cases[i].expected), "file name %s/%s: %s, not %s", cases[i].program ? cases[i].program : "-",
              cases[i].instance ? cases[i].instance : "-", name, cases[i].expected);
        CHECK(strlen(name) <= 31, "file name %s longer than rdk-logger's 31", name);
    }
}

static void messages_through_the_logger(void)
{
    em_log(EM_LOG_INFO, "emosa.agent", "before the log is opened: stderr");
    CHECK(messages == 0, "a message before em_log_open reached the logger");

    em_log_open("agent", "MVXPOD023F87E628DD");
    CHECK(inits == 1, "the logger initialised %d times", inits);
    CHECK(!strcmp(last_config.fileName, "EMOSA_MVXPOD023F87E628DD.txt"), "file %s", last_config.fileName);
    CHECK(!strcmp(last_config.logdir, "/rdklogs/logs/"), "directory %s", last_config.logdir);
    CHECK(last_config.maxSize == 4096 && last_config.maxCount == 3, "rolling %ld x %ld", last_config.maxSize,
          last_config.maxCount);

    em_log(EM_LOG_WARNING, "emosa.agent.uplink", "uplink held on option 2: %s", "no confirmation");
    CHECK(messages == 1, "%d messages", messages);
    CHECK(last_level == RDK_LOG_WARN, "level %d", (int)last_level);
    CHECK(!strcmp(last_module, "LOG.RDK.EMOSA"), "module %s", last_module);
    CHECK(!strcmp(last_text, "emosa.agent.uplink[MVXPOD023F87E628DD]: uplink held on option 2: no confirmation\n"),
          "text %s", last_text);

    static const struct {
        em_log_level level;
        rdk_LogLevel rdk;
    } levels[] = {{EM_LOG_ERROR, RDK_LOG_ERROR}, {EM_LOG_WARNING, RDK_LOG_WARN}, {EM_LOG_INFO, RDK_LOG_INFO},
                  {EM_LOG_DEBUG, RDK_LOG_DEBUG}, {(em_log_level)42, RDK_LOG_ERROR}};
    for (size_t i = 0; i < sizeof(levels) / sizeof(*levels); i++) {
        em_log(levels[i].level, "emosa.agent", "level");
        CHECK(last_level == levels[i].rdk, "level %d -> %d, not %d", (int)levels[i].level, (int)last_level,
              (int)levels[i].rdk);
    }

    /* a message longer than the line: cut, never past the buffer */
    char huge[5000];
    memset(huge, 'x', sizeof(huge) - 1);
    huge[sizeof(huge) - 1] = 0;
    em_log(EM_LOG_INFO, "emosa.agent", "%s", huge);
    size_t n = strlen(last_text);
    CHECK(n > 2000 && n < 2200, "a long message became %zu characters", n);

    /* a program without an instance: no brackets */
    em_log_open("fleet", NULL);
    CHECK(!strcmp(last_config.fileName, "EMOSAFleetLog.txt"), "file %s", last_config.fileName);
    em_log(EM_LOG_INFO, "emosa.fleet", "pod %s", "MVXPOD1");
    CHECK(!strcmp(last_text, "emosa.fleet: pod MVXPOD1\n"), "text %s", last_text);

    /* the logger refuses: stderr */
    init_result = 1;
    em_log_open("gtp", NULL);
    int before = messages;
    em_log(EM_LOG_WARNING, "emosa.gtp", "after a refused initialisation: stderr");
    CHECK(messages == before, "a message reached a logger that refused to start");
}

int main(void)
{
    em_init();
    file_names();
    messages_through_the_logger();
    printf("%d checks, %d failures\n", checks, failures);
    return failures != 0;
}
