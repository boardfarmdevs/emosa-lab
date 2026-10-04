/* SPDX-License-Identifier: Apache-2.0 */
/* Fuzz target: what the pod sends on its management connection (ovsdb.c: the JSON-RPC
 * stream, the dispatch of each message, monitor updates folded into the rows), then the
 * agent's view of the rows that results. The input is the byte stream, delivered in chunks
 * whose sizes come from its first byte (as recv would cut it), to a session without a
 * socket: an echo's answer has nowhere to go and is dropped. */
#include "ovsdb.h"
#include "view.h"

#include <stdlib.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    static bool ready;
    if (!ready) {
        em_init();
        ready = true;
    }
    if (size < 1)
        return 0;
    size_t chunk = 1 + (size_t)data[0] * 16; /* 1 .. 4081 bytes at a time */
    data++;
    size--;
    cJSON *empty = cJSON_CreateObject();
    em_ovsdb *s = em_ovsdb_fixed(empty, 1, "fuzz"); /* monitor 0: updates [0, {...}] apply */
    cJSON_Delete(empty);
    bool ok = true;
    for (size_t at = 0; ok && at < size; at += chunk)
        ok = em_ovsdb_input(s, (const char *)data + at, size - at < chunk ? size - at : chunk);
    em_device_view *view = em_malloc(sizeof(*view));
    if (em_device_view_from_rows(em_ovsdb_tables(s), view) == EM_OK)
        cJSON_Delete(em_device_view_json(view));
    free(view);
    em_ovsdb_close(s);
    return 0;
}
