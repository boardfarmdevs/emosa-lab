/* SPDX-License-Identifier: Apache-2.0 */
/* emosa-forward-c (spec §3.1) as a program: inert without "forward"; with it, a connection to
 * the advertise address (here 127.0.0.2) reaches what listens on 127.0.0.1 on the same port,
 * both ways, each end passed on; a port nothing listens on closes the connection.
 *
 *   emosa-forward-test FORWARDER SCHEMAS DIR */
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

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

static void pause_ms(long ms)
{
    struct timespec ts = {.tv_sec = ms / 1000, .tv_nsec = (ms % 1000) * 1000000L};
    while (nanosleep(&ts, &ts) && errno == EINTR) {
    }
}

static struct sockaddr_in at(const char *address, int port)
{
    struct sockaddr_in sa = {.sin_family = AF_INET, .sin_port = htons((uint16_t)port)};
    (void)inet_pton(AF_INET, address, &sa.sin_addr);
    return sa;
}

/* a port free on 127.0.0.1 and 127.0.0.2 now */
static int free_port(void)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in sa = at("127.0.0.1", 0);
    socklen_t len = sizeof(sa);
    if (fd < 0 || bind(fd, (struct sockaddr *)&sa, sizeof(sa)) || getsockname(fd, (struct sockaddr *)&sa, &len)) {
        if (fd >= 0)
            close(fd);
        return -1;
    }
    close(fd);
    return ntohs(sa.sin_port);
}

/* an echo server on 127.0.0.1:port for one connection, in a child */
static pid_t echo(int port)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0), one = 1;
    struct sockaddr_in sa = at("127.0.0.1", port);
    if (fd < 0 || setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)) ||
        bind(fd, (struct sockaddr *)&sa, sizeof(sa)) || listen(fd, 1)) {
        if (fd >= 0)
            close(fd);
        return -1;
    }
    pid_t pid = fork();
    if (pid) {
        close(fd);
        return pid;
    }
    int c = accept(fd, NULL, NULL);
    char buffer[256];
    ssize_t n;
    while (c >= 0 && (n = recv(c, buffer, sizeof(buffer), 0)) > 0)
        if (send(c, buffer, (size_t)n, MSG_NOSIGNAL) != n)
            break;
    if (c >= 0) {
        (void)shutdown(c, SHUT_WR);
        close(c);
    }
    _exit(0);
}

static pid_t forwarder(const char *program, const char *schemas, const char *config)
{
    pid_t pid = fork();
    if (pid)
        return pid;
    (void)setenv("EMOSA_SCHEMAS", schemas, 1);
    execl(program, program, config, (char *)NULL);
    _exit(127);
}

static int dial(const char *address, int port)
{
    for (int attempt = 0; attempt < 50; attempt++) { /* 5 s for the forwarder to listen */
        int fd = socket(AF_INET, SOCK_STREAM, 0);
        struct sockaddr_in sa = at(address, port);
        if (fd >= 0 && !connect(fd, (struct sockaddr *)&sa, sizeof(sa)))
            return fd;
        if (fd >= 0)
            close(fd);
        pause_ms(100);
    }
    return -1;
}

static void write_config(const char *path, int front, int first, int last, bool forward)
{
    FILE *f = fopen(path, "w");
    if (!f)
        return;
    (void)fprintf(f,
                  "{\"listen\": \"ptcp:%d:127.0.0.1\", \"advertise\": \"127.0.0.2\", %s"
                  "\"ports\": [%d, %d], \"controller_al\": \"00:60:2f:da:68:d4\", "
                  "\"state_root\": \"/nonexistent\", \"config_dir\": \"/nonexistent\"}\n",
                  front, forward ? "\"forward\": true, " : "", first, last);
    (void)fclose(f);
}

int main(int argc, char **argv)
{
    if (argc != 4) {
        (void)fprintf(stderr, "usage: emosa-forward-test FORWARDER SCHEMAS DIR\n");
        return 2;
    }
    char config[512];
    (void)snprintf(config, sizeof(config), "%s/forward-fleet.json", argv[3]);
    int front = free_port(), agent = free_port(), silent = agent + 1;
    CHECK(front > 0 && agent > 0, "no free ports");

    /* without "forward": inert, at once */
    write_config(config, front, agent, silent, false);
    pid_t pid = forwarder(argv[1], argv[2], config);
    int status = -1;
    CHECK(waitpid(pid, &status, 0) == pid && WIFEXITED(status) && WEXITSTATUS(status) == 0,
          "without forward: exits 0, nothing forwarded (status %d)", status);

    /* with it: the agent port both ways, each end passed on */
    write_config(config, front, agent, silent, true);
    pid_t server = echo(agent);
    CHECK(server > 0, "echo server");
    pid = forwarder(argv[1], argv[2], config);
    int fd = dial("127.0.0.2", agent);
    CHECK(fd >= 0, "the agent port reached on the advertise address");
    const char message[] = "{\"method\":\"echo\",\"params\":[],\"id\":\"forwarded\"}";
    char reply[sizeof(message)] = "";
    size_t got = 0;
    if (fd >= 0) {
        CHECK(send(fd, message, sizeof(message) - 1, MSG_NOSIGNAL) == (ssize_t)sizeof(message) - 1, "sent");
        while (got < sizeof(message) - 1) {
            ssize_t n = recv(fd, reply + got, sizeof(message) - 1 - got, 0);
            if (n <= 0)
                break;
            got += (size_t)n;
        }
        CHECK(got == sizeof(message) - 1 && !memcmp(reply, message, got), "the same bytes back (%zu)", got);
        (void)shutdown(fd, SHUT_WR); /* the client's end, passed on: the server ends too */
        char rest;
        CHECK(recv(fd, &rest, 1, 0) == 0, "the server's end passed back");
        close(fd);
    }
    CHECK(server > 0 && waitpid(server, &status, 0) == server, "the echo server saw the end");

    /* a port nothing listens on behind: the connection is closed */
    fd = dial("127.0.0.2", silent);
    CHECK(fd >= 0, "the silent port accepts");
    if (fd >= 0) {
        char byte;
        ssize_t n = recv(fd, &byte, 1, 0);
        CHECK(n <= 0, "closed when nothing listens on 127.0.0.1 (%zd)", n);
        close(fd);
    }
    (void)kill(pid, SIGTERM);
    CHECK(waitpid(pid, &status, 0) == pid && WIFEXITED(status) && WEXITSTATUS(status) == 0,
          "stops on SIGTERM with 0 (status %d)", status);
    (void)unlink(config);
    (void)printf("%s: %d checks, %d failed\n", failures ? "FAIL" : "PASS", checks, failures);
    return failures ? 1 : 0;
}
