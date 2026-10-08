/* SPDX-License-Identifier: Apache-2.0 */
/* The durable operation journal, as emosa.store.Store: <directory>/journal.db (SQLite,
 * WAL, synchronous FULL) with the same tables and JSON records, and <directory>/
 * writer.lock held while open. Either implementation reads the other's journal: an
 * agent swapped between Python and C keeps its operations and credentials.
 *
 * Records are cJSON objects with exactly the reference's Operation fields; they are
 * validated against schemas/operation.schema.json (and event, wsc-receipt) when the
 * schemas are loaded. */
#ifndef EMOSA_JOURNAL_H
#define EMOSA_JOURNAL_H

#include <cjson/cJSON.h>

#include "common.h"
#include "jschema.h"

typedef struct em_journal em_journal;

/* The schemas every journal validates against (NULL: not validated). */
typedef struct {
    em_schema *operation, *event, *receipt;
} em_journal_schemas;

/* why: EM_BUSY when another writer holds the directory, EM_SCHEMA_MISMATCH for another
 * journal version, EM_NOT_READY when SQLite fails. */
em_journal *em_journal_open(const char *directory, const em_journal_schemas *schemas, em_reason *why);
void em_journal_close(em_journal *j);
const char *em_journal_directory(const em_journal *j);
const char *em_journal_process_id(const em_journal *j);

/* The journal's operations, oldest first, as stored: kept parsed after the first read
 * and updated by this process's writes (it is the journal's only writer). Borrowed: valid
 * until the next write; never modified by the caller. */
const cJSON *em_journal_operations_view(em_journal *j);
/* The most recent operation, borrowed as above; NULL when there is none. */
const cJSON *em_journal_latest_view(em_journal *j);

/* Owned results (cJSON_Delete); NULL when absent. */
cJSON *em_journal_get(em_journal *j, const char *operation_id);
cJSON *em_journal_lookup(em_journal *j, const char *source, const char *pod, const char *key);
cJSON *em_journal_operations(em_journal *j, const char *run_id); /* array, oldest first */
/* an operation whose window created rows in the pod not yet released (spec §6, finding 23) */
bool em_journal_holds_rows(const cJSON *op);
cJSON *em_journal_latest(em_journal *j);
size_t em_journal_count(em_journal *j);
cJSON *em_journal_wsc_receipt(em_journal *j, const char *operation_id);
cJSON *em_journal_events(em_journal *j, const char *run_id, long after, int limit);
cJSON *em_journal_ownership(em_journal *j, const char *pod);

/* Writes: the record and an event in one transaction. */
em_reason em_journal_add(em_journal *j, const cJSON *op, const cJSON *wsc_receipt);
em_reason em_journal_save(em_journal *j, const cJSON *op, const cJSON *payload);
em_reason em_journal_event(em_journal *j, const char *run_id, const char *phase, const cJSON *payload,
                           const char *pod_id);
void em_journal_conflict(em_journal *j, const char *pod, const cJSON *evidence);
void em_journal_release(em_journal *j, const char *pod);

/* emosa.secrets.redact: sensitive keys' values replaced, recursively (a new value). */
cJSON *em_redact(const cJSON *value);

#endif
