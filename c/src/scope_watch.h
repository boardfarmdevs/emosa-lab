/* The probe watch, as emosa.opensync.probe_watch.WatchBackend and
 * emosa.agent.probe_watch.ProbeWatch: the stations the controller asks to be measured
 * get a watch row on the pod (client steering off, marked {"emosa": "watch"}) so that
 * owm reports their probe requests. At most 32 watched, dropped after 600 s unasked,
 * written at most every 10 s, one guarded write at a time, its own journal. */
#ifndef EMOSA_SCOPE_WATCH_H
#define EMOSA_SCOPE_WATCH_H

#include "engine.h"
#include "ovsdb.h"

#define EM_WATCH_ASKED 256

typedef struct {
    em_ovsdb *ovs;
    const char *serial, *pod_id, *run_id, *if_name, *band;
    cJSON *(*transact)(void *ctx, const cJSON *operations);
    void *transact_ctx;
    double (*monotonic)(void);
    em_journal *journal;
    em_engine engine;
    char instance[17];
    em_snapshot last;
    bool has_last;
    size_t nasked;
    struct {
        char mac[18];
        double at;
    } asked[EM_WATCH_ASKED];
    double last_request;
    bool has_last_request;
    char waiting[96];
    bool has_waiting;
} em_watch_scope;

em_reason em_watch_open(em_watch_scope *w, const char *state_dir, const em_journal_schemas *schemas,
                        const em_vault *vault);
void em_watch_close(em_watch_scope *w);
/* ProbeWatch.ask: the stations queried on the pod's own channel. */
void em_watch_ask(em_watch_scope *w, const uint8_t (*stations)[6], size_t n);
void em_watch_tick(em_watch_scope *w);
cJSON *em_watch_status(em_watch_scope *w);

/* The scope's backend (the engine's), for vectors and tests. */
em_backend em_watch_backend(void);

#endif
