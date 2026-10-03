/* Fuzz target: what the broker sends the agent (mqtt.c: MQTT 3.1.1 framing, CONNACK,
 * SUBACK, PUBLISH at any QoS, the topic). The input's first byte chooses the connection's
 * phase (awaiting the CONNACK, the SUBACK, or subscribed); the rest arrives as one read,
 * then again split in two, as a stream can. The payloads go to a stub: their parser is the
 * stats target's. */
#include "mqtt.h"

#include <string.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

static size_t delivered;

static void deliver(void *ctx, const char *topic, const uint8_t *payload, size_t len, bool retained)
{
    (void)ctx;
    (void)retained;
    delivered += strlen(topic) + len + (len ? payload[0] : 0);
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    static bool ready;
    if (!ready) {
        em_init();
        ready = true;
    }
    if (size < 1)
        return 0;
    int awaiting = data[0];
    for (int split = 0; split < 2; split++) {
        em_mqtt *m = em_mqtt_open("127.0.0.1", 1883, "emosa/stats/MVXPOD023F87E628DD", "fuzz", deliver, NULL);
        if (!m)
            return 0;
        size_t first = split ? (size - 1) / 2 : size - 1;
        if (em_mqtt_input(m, awaiting, data + 1, first, 1000.0))
            (void)em_mqtt_input(m, awaiting, data + 1 + first, size - 1 - first, 1000.5);
        em_mqtt_close(m);
    }
    return 0;
}
