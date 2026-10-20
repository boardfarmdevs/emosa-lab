/* SPDX-License-Identifier: Apache-2.0 */
/* EMOSA C: the programs' log (log.h). */
#include "log.h"

#include "common.h"

#include <ctype.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#ifdef EMOSA_RDK_LOGGER
#include <rdk_logger.h>
#ifndef EMOSA_RDK_LOG_DIR
#define EMOSA_RDK_LOG_DIR "/rdklogs/logs/"
#endif
#ifndef EMOSA_RDK_LOG_MAXSIZE
#define EMOSA_RDK_LOG_MAXSIZE 262144L /* bytes per file */
#endif
#ifndef EMOSA_RDK_LOG_MAXCOUNT
#define EMOSA_RDK_LOG_MAXCOUNT 2L /* files kept */
#endif
#define EMOSA_RDK_MODULE "LOG.RDK.EMOSA"
#endif

static const char *const NAMES[] = {"ERROR", "WARNING", "INFO", "DEBUG"};

static struct {
    bool rdk;           /* the messages go through RDK's logger */
    char instance[64];  /* the agent's pod, shown with each RDK message */
} state;

static bool plain(const char *s, size_t max)
{
    size_t n = s ? strlen(s) : 0;
    if (!n || n > max || s[0] == '.')
        return false;
    for (size_t i = 0; i < n; i++)
        if (!isalnum((unsigned char)s[i]) && s[i] != '.' && s[i] != '_' && s[i] != '-')
            return false;
    return true;
}

void em_log_file_name(const char *program, const char *instance, char out[32])
{
    /* "EMOSA_" + instance + ".txt" in 31 characters: an instance of at most 21 */
    if (plain(instance, 21)) {
        EM_FORMAT_FIXED(out, 32, "EMOSA_%s.txt", instance); /* checked: at most 31 */
        return;
    }
    char name[13] = "";
    size_t n = program ? strlen(program) : 0;
    bool letters = n && n < sizeof(name);
    for (size_t i = 0; letters && i < n; i++)
        letters = isalpha((unsigned char)program[i]) != 0;
    if (letters) {
        for (size_t i = 0; i < n; i++)
            name[i] = (char)(i ? tolower((unsigned char)program[i]) : toupper((unsigned char)program[i]));
        name[n] = 0;
    }
    EM_FORMAT_FIXED(out, 32, "EMOSA%sLog.txt", name); /* at most 5 + 12 + 7 characters */
}

void em_log_open(const char *program, const char *instance)
{
    if (!em_copy(state.instance, sizeof(state.instance), plain(instance, sizeof(state.instance) - 1) ? instance : ""))
        state.instance[0] = 0;
#ifdef EMOSA_RDK_LOGGER
    rdk_logger_ext_config_t config;
    memset(&config, 0, sizeof(config));
    em_log_file_name(program, instance, config.fileName);
    if (!em_copy(config.logdir, sizeof(config.logdir), EMOSA_RDK_LOG_DIR))
        return; /* a directory too long for the logger: stderr */
    config.maxSize = EMOSA_RDK_LOG_MAXSIZE;
    config.maxCount = EMOSA_RDK_LOG_MAXCOUNT;
    state.rdk = rdk_logger_ext_init(&config) == RDK_SUCCESS;
    if (!state.rdk)
        (void)fprintf(stderr, "WARNING emosa: RDK logger not initialised, logging to stderr\n");
#else
    (void)program;
#endif
}

void em_log_stamp(const struct timespec *at, char out[EM_LOG_STAMP])
{
    struct tm tm;
    time_t seconds = at->tv_sec;
    if (!gmtime_r(&seconds, &tm) || strftime(out, 20, "%Y-%m-%dT%H:%M:%S", &tm) != 19) {
        memcpy(out, "0000-00-00T00:00:00.000Z", EM_LOG_STAMP);
        return;
    }
    long ms = at->tv_nsec / 1000000;
    out[19] = '.';
    out[20] = (char)('0' + (ms / 100) % 10);
    out[21] = (char)('0' + (ms / 10) % 10);
    out[22] = (char)('0' + ms % 10);
    out[23] = 'Z';
    out[24] = '\0';
}

void em_vlog(em_log_level level, const char *component, const char *fmt, va_list ap)
{
    if ((unsigned)level > EM_LOG_DEBUG)
        level = EM_LOG_ERROR;
    /* every line starts with its time (logs.py): ISO 8601, UTC, milliseconds */
    struct timespec at = {0};
    char when[EM_LOG_STAMP];
    (void)clock_gettime(CLOCK_REALTIME, &at);
    em_log_stamp(&at, when);
#ifdef EMOSA_RDK_LOGGER
    if (state.rdk) {
        static const rdk_LogLevel LEVELS[] = {RDK_LOG_ERROR, RDK_LOG_WARN, RDK_LOG_INFO, RDK_LOG_DEBUG};
        char text[2048];
        (void)vsnprintf(text, sizeof(text), fmt, ap); /* a message: cut when longer */
        if (state.instance[0])
            RDK_LOG(LEVELS[level], EMOSA_RDK_MODULE, "%s %s %s[%s]: %s\n", when, NAMES[level], component,
                    state.instance, text);
        else
            RDK_LOG(LEVELS[level], EMOSA_RDK_MODULE, "%s %s %s: %s\n", when, NAMES[level], component, text);
        return;
    }
#endif
    (void)fprintf(stderr, "%s %s %s: ", when, NAMES[level], component);
    (void)vfprintf(stderr, fmt, ap);
    (void)fputc('\n', stderr);
}

void em_log(em_log_level level, const char *component, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    em_vlog(level, component, fmt, ap);
    va_end(ap);
}
