/* SPDX-License-Identifier: Apache-2.0 */
/* Other programs, without a shell (see proc.h). */
#include "proc.h"

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <spawn.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

/* posix_spawnp's argv is char *const *: the strings are not written (POSIX) */
static char *const *spawn_argv(const char *const *argv)
{
    union {
        const char *const *in;
        char *const *out;
    } cast = {.in = argv};
    return cast.out;
}

static bool start(const char *const *argv, int out_fd, int err_fd, pid_t *pid)
{
    posix_spawn_file_actions_t actions;
    if (posix_spawn_file_actions_init(&actions))
        return false;
    bool ok = !posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0);
    if (ok && out_fd >= 0)
        ok = !posix_spawn_file_actions_adddup2(&actions, out_fd, 1);
    if (ok && err_fd >= 0)
        ok = !posix_spawn_file_actions_adddup2(&actions, err_fd, 2);
    ok = ok && !posix_spawnp(pid, argv[0], &actions, NULL, spawn_argv(argv), environ);
    posix_spawn_file_actions_destroy(&actions);
    return ok;
}

bool em_spawn(const char *const *argv, pid_t *pid) { return start(argv, -1, -1, pid); }

static int exit_code(int raw) { return WIFEXITED(raw) ? WEXITSTATUS(raw) : -1; }

bool em_reaped(pid_t pid, int *status)
{
    int raw;
    pid_t got = waitpid(pid, &raw, WNOHANG);
    if (got == 0)
        return false;
    *status = got == pid ? exit_code(raw) : -1; /* an error: the child is gone */
    return true;
}

/* everything until EOF on both pipes into the two buffers */
static void drain(int fds[2], em_buf bufs[2])
{
    bool open[2] = {true, true};
    while (open[0] || open[1]) {
        struct pollfd p[2];
        nfds_t n = 0;
        int which[2];
        for (int i = 0; i < 2; i++)
            if (open[i]) {
                p[n] = (struct pollfd){fds[i], POLLIN, 0};
                which[n++] = i;
            }
        if (poll(p, n, -1) < 0) {
            if (errno == EINTR)
                continue;
            return;
        }
        for (nfds_t k = 0; k < n; k++) {
            if (!p[k].revents)
                continue;
            char chunk[4096];
            ssize_t r = read(p[k].fd, chunk, sizeof(chunk));
            if (r > 0)
                (void)em_buf_put(&bufs[which[k]], chunk, (size_t)r);
            else if (r == 0 || errno != EINTR)
                open[which[k]] = false;
        }
    }
}

/* a pipe whose ends a child does not inherit (its dup2'd copies it does) */
static bool private_pipe(int fds[2])
{
    if (pipe(fds))
        return false;
    for (int i = 0; i < 2; i++)
        if (fcntl(fds[i], F_SETFD, FD_CLOEXEC) == -1)
            return false;
    return true;
}

bool em_run(const char *const *argv, char **out, char **err, int *status)
{
    int pipes[2][2] = {{-1, -1}, {-1, -1}};
    if (!private_pipe(pipes[0]) || !private_pipe(pipes[1])) {
        for (int i = 0; i < 2; i++)
            for (int j = 0; j < 2; j++)
                if (pipes[i][j] >= 0)
                    close(pipes[i][j]);
        return false;
    }
    pid_t pid;
    bool started = start(argv, pipes[0][1], pipes[1][1], &pid);
    close(pipes[0][1]);
    close(pipes[1][1]);
    em_buf bufs[2] = {{0}, {0}};
    if (started) {
        int fds[2] = {pipes[0][0], pipes[1][0]};
        drain(fds, bufs);
    }
    close(pipes[0][0]);
    close(pipes[1][0]);
    if (started) {
        int raw = 0;
        pid_t got;
        do
            got = waitpid(pid, &raw, 0);
        while (got < 0 && errno == EINTR);
        *status = got == pid ? exit_code(raw) : -1;
    }
    char **dest[2] = {out, err};
    for (int i = 0; i < 2; i++) {
        if (started && dest[i]) {
            (void)em_buf_u8(&bufs[i], 0);
            *dest[i] = bufs[i].data ? (char *)bufs[i].data : em_strdup("");
        } else {
            em_buf_free(&bufs[i]);
        }
    }
    return started;
}

bool em_mkdirs(const char *path, mode_t mode)
{
    size_t n = strlen(path);
    if (!n)
        return false;
    char *copy = em_strdup(path);
    bool ok = true;
    for (size_t i = 1; ok && i <= n; i++) {
        if (copy[i] != '/' && copy[i] != 0)
            continue;
        char saved = copy[i];
        copy[i] = 0;
        if (mkdir(copy, saved ? 0755 : mode) && errno != EEXIST)
            ok = false;
        copy[i] = saved;
    }
    struct stat st;
    ok = ok && !stat(path, &st) && S_ISDIR(st.st_mode);
    free(copy);
    return ok;
}
