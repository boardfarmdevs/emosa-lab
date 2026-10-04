/* SPDX-License-Identifier: Apache-2.0 */
/* The pod's statistics topic on the local broker (emosa.agent.telemetry.MqttSubscriber):
 * a minimal MQTT 3.1.1 subscriber, clean session, QoS 0, keepalive 30 s, reconnecting
 * after 1 s doubling to 30 s. Never blocks; the agent polls its descriptor. */
#ifndef EMOSA_MQTT_H
#define EMOSA_MQTT_H

#include "common.h"

typedef void (*em_mqtt_deliver)(void *ctx, const char *topic, const uint8_t *payload, size_t len, bool retained);

typedef struct em_mqtt em_mqtt;

em_mqtt *em_mqtt_open(const char *host, int port, const char *topic, const char *client_id,
                      em_mqtt_deliver deliver, void *ctx);
void em_mqtt_close(em_mqtt *m);
/* The socket to poll (-1 while waiting to reconnect) and the events wanted. */
int em_mqtt_fd(const em_mqtt *m, short *events);
/* Connect, read, answer, keep alive; now is monotonic seconds. */
void em_mqtt_pump(em_mqtt *m, double now);
/* Subscribed on a live connection. */
bool em_mqtt_connected(const em_mqtt *m);

/* For the fuzz target and tests: bytes as if received from the broker while awaiting
 * its CONNACK (0), its SUBACK (1) or subscribed (2); false where the agent would drop
 * the connection. */
bool em_mqtt_input(em_mqtt *m, int awaiting, const uint8_t *data, size_t len, double now);

#endif
