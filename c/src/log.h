/* SPDX-License-Identifier: Apache-2.0 */
/* EMOSA C: the programs' log (QUALITY.md §2: secrets never reach it).
 *
 * Every message has a level and a component (the reference's logger name, e.g.
 * "emosa.agent.uplink"). By default a message is one line on stderr, "TIME LEVEL component:
 * text" (TIME ISO 8601 in UTC to the millisecond, 2026-10-20T19:23:11.123Z, as the
 * reference's emosa.logs), which the service manager keeps (the labs read the units'
 * journals). Built with EMOSA_RDK_LOGGER, the messages go through RDK's logger instead
 * (module LOG.RDK.EMOSA, its levels from debug.ini), each program into its own rolling file in
 * the RDK log directory: EMOSA<Program>Log.txt, an agent's EMOSA_<pod>.txt; each line again
 * "TIME LEVEL component: text". */
#ifndef EMOSA_LOG_H
#define EMOSA_LOG_H

#include <stdarg.h>
#include <time.h>

#define EM_LOG_STAMP 25 /* "2026-10-20T19:23:11.123Z" and its NUL */

/* A line's time: ISO 8601, UTC, milliseconds, Z. */
void em_log_stamp(const struct timespec *at, char out[EM_LOG_STAMP]);

typedef enum {
    EM_LOG_ERROR,
    EM_LOG_WARNING,
    EM_LOG_INFO,
    EM_LOG_DEBUG,
} em_log_level;

/* Once, at the program's start: `program` names its log ("agent", "fleet", "gtp"), and
 * `instance` the agent's pod (or NULL). A message logged before goes to stderr. */
void em_log_open(const char *program, const char *instance);

void em_log(em_log_level level, const char *component, const char *fmt, ...) __attribute__((format(printf, 3, 4)));
void em_vlog(em_log_level level, const char *component, const char *fmt, va_list ap) __attribute__((format(printf, 3, 0)));

/* The file name of a program's RDK log (at most 31 characters), as em_log_open picks it:
 * EMOSA_<instance>.txt when the instance is a short plain name, else
 * EMOSA<Program>Log.txt. Exposed for the tests. */
void em_log_file_name(const char *program, const char *instance, char out[32]);

#endif
