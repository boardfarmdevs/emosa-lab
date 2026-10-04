/* SPDX-License-Identifier: Apache-2.0 */
/* RFC 7047 JSON-RPC framing and messages (see jsonrpc.h). */
#include "jsonrpc.h"

#include <string.h>

bool em_json_next(const char *buf, size_t len, size_t *start, size_t *end)
{
    size_t i = 0;
    while (i < len && (buf[i] == ' ' || buf[i] == '\n' || buf[i] == '\r' || buf[i] == '\t'))
        i++;
    *start = i;
    size_t depth = 0;
    bool string = false, escape = false;
    for (; i < len; i++) {
        char c = buf[i];
        if (string) {
            if (escape)
                escape = false;
            else if (c == '\\')
                escape = true;
            else if (c == '"')
                string = false;
        } else if (c == '"') {
            string = true;
        } else if (c == '{' || c == '[') {
            depth++;
        } else if (c == '}' || c == ']') {
            /* a closing bracket before any opening one is not a message: it ends here,
             * and the caller's parser refuses it */
            if (depth <= 1) {
                *end = i + 1;
                return true;
            }
            depth--;
        }
    }
    return false;
}

cJSON *em_rpc_request(const char *method, cJSON *params, long id)
{
    cJSON *m = cJSON_CreateObject();
    cJSON_AddStringToObject(m, "method", method);
    cJSON_AddItemToObject(m, "params", params);
    cJSON_AddNumberToObject(m, "id", (double)id);
    return m;
}

cJSON *em_rpc_echo_reply(const cJSON *message)
{
    const cJSON *method = cJSON_GetObjectItemCaseSensitive(message, "method");
    const cJSON *id = cJSON_GetObjectItemCaseSensitive(message, "id");
    if (!cJSON_IsString(method) || strcmp(method->valuestring, "echo") || !id || cJSON_IsNull(id))
        return NULL;
    cJSON *reply = cJSON_CreateObject();
    const cJSON *params = cJSON_GetObjectItemCaseSensitive(message, "params");
    cJSON_AddItemToObject(reply, "result", params ? cJSON_Duplicate(params, 1) : cJSON_CreateArray());
    cJSON_AddNullToObject(reply, "error");
    cJSON_AddItemToObject(reply, "id", cJSON_Duplicate(id, 1));
    return reply;
}

bool em_rpc_is_reply(const cJSON *message, long id, const cJSON **result)
{
    const cJSON *got = cJSON_GetObjectItemCaseSensitive(message, "id");
    if (cJSON_GetObjectItemCaseSensitive(message, "method") || !cJSON_IsNumber(got) ||
        got->valuedouble != (double)id)
        return false;
    const cJSON *error = cJSON_GetObjectItemCaseSensitive(message, "error");
    *result = error && !cJSON_IsNull(error) ? NULL : cJSON_GetObjectItemCaseSensitive(message, "result");
    return true;
}
