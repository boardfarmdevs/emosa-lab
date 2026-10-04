/* SPDX-License-Identifier: Apache-2.0 */
/* The accepted channel policy's durable record (emosa.wire.channel.ChannelPolicyStore). */
#include "channel_store.h"

#include <sqlite3.h>
#include <stdlib.h>
#include <string.h>

#include "canon.h"

struct em_channel_store {
    sqlite3 *db;
};

em_channel_store *em_channel_store_open(const char *path)
{
    em_channel_store *s = em_calloc(1, sizeof(*s));
    if (sqlite3_open(path, &s->db) != SQLITE_OK ||
        sqlite3_exec(s->db,
                     "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;"
                     "CREATE TABLE IF NOT EXISTS channel_policy (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)",
                     NULL, NULL, NULL) != SQLITE_OK) {
        em_channel_store_close(s);
        return NULL;
    }
    sqlite3_busy_timeout(s->db, 2000);
    return s;
}

void em_channel_store_close(em_channel_store *s)
{
    if (!s)
        return;
    if (s->db)
        sqlite3_close(s->db);
    free(s);
}

bool em_channel_store_save(em_channel_store *s, const cJSON *record)
{
    char *text = em_json_dumps(record, EM_JSON_DEFAULT, true);
    if (!text || strlen(text) > 8192) {
        free(text);
        return false; /* "channel policy exceeds durable record budget" */
    }
    sqlite3_stmt *st;
    bool ok = false;
    if (sqlite3_prepare_v2(s->db,
                           "INSERT INTO channel_policy VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                           -1, &st, NULL) == SQLITE_OK) {
        sqlite3_bind_text(st, 1, text, -1, SQLITE_TRANSIENT);
        ok = sqlite3_step(st) == SQLITE_DONE;
        sqlite3_finalize(st);
    }
    free(text);
    return ok;
}

cJSON *em_channel_store_read(em_channel_store *s)
{
    sqlite3_stmt *st;
    cJSON *v = NULL;
    if (sqlite3_prepare_v2(s->db, "SELECT value FROM channel_policy WHERE id=1", -1, &st, NULL) == SQLITE_OK) {
        if (sqlite3_step(st) == SQLITE_ROW)
            v = cJSON_Parse((const char *)sqlite3_column_text(st, 0));
        sqlite3_finalize(st);
    }
    return v;
}
