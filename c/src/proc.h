/* SPDX-License-Identifier: Apache-2.0 */
/* Other programs, without a shell (CERT ENV33-C): the fleet's systemctl and the GTP's
 * iproute2. Arguments are passed as given, never interpreted. */
#ifndef EMOSA_PROC_H
#define EMOSA_PROC_H

#include <sys/types.h>

#include "common.h"

/* argv[0] found on PATH, started with the caller's environment and /dev/null as its
 * input; false when it could not be started. */
bool em_spawn(const char *const *argv, pid_t *pid);

/* The child's exit: true once it has ended (*status its exit code, -1 when it was
 * killed), false while it runs. Never blocks. */
bool em_reaped(pid_t pid, int *status);

/* Runs argv to its end: its standard output and error (each NUL-terminated, the caller
 * frees them; NULL arguments discard them) and its exit code (-1 when killed). False
 * when it could not be started. */
bool em_run(const char *const *argv, char **out, char **err, int *status);

/* mkdir -p: the missing directories of path, the last with mode (0755 above it). */
bool em_mkdirs(const char *path, mode_t mode);

#endif
