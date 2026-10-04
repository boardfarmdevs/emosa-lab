/* SPDX-License-Identifier: Apache-2.0 */
/* The accepted channel policy, one durable record per agent, as the reference's
 * emosa.wire.channel.ChannelPolicyStore (state_dir/channel-policy.sqlite): a receipt,
 * kept before the Channel Selection Response is sent, never read back as evidence that
 * the radio applied it. The agent's first session after it starts resets it
 * ("reboot_default", spec §8.2). */
#ifndef EMOSA_CHANNEL_STORE_H
#define EMOSA_CHANNEL_STORE_H

#include <cjson/cJSON.h>

#include "common.h"

typedef struct em_channel_store em_channel_store;

em_channel_store *em_channel_store_open(const char *path);
void em_channel_store_close(em_channel_store *s);
/* false when the record exceeds its 8 KiB budget or cannot be written */
bool em_channel_store_save(em_channel_store *s, const cJSON *record);
/* the record, or NULL; the caller frees it */
cJSON *em_channel_store_read(em_channel_store *s);

#endif
