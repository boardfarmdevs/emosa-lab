"""Owned lab MQTT transport and measured OpenSync-format station publisher.

Unix socket permissions provide isolation here, not physical pod authentication.
This transport is deliberately unavailable to the hardware service.
"""

import time
from pathlib import Path

import paho.mqtt.client as mqtt

from emosa.telemetry.stations import Report

NODE_ID = "owned-wsc-component"
TOPIC = "emosa/owned/sole-radio/clients"


def encode_stations(observed, ssid):
    if observed.get("complete") is not True:
        raise ValueError("station read is incomplete")
    report = Report(nodeID=NODE_ID)
    radio = report.clients.add(band=0, channel=6, timestamp_ms=observed["timestamp_ms"])
    for station in observed["clients"]:
        radio.client_list.add(
            mac_address=station["mac"],
            ssid=ssid,
            connected=True,
            connect_offset_ms=station["connected_seconds"] * 1000,
        )
    return report.SerializeToString()


class LabMqtt:
    """Polled on the caller's loop; no unbounded queue or background callbacks."""

    def __init__(self, directory, source=None):
        import json

        from emosa.simulation.native_onboarding import OWNER, RADIO_ROOT

        directory = Path(directory).resolve(strict=True)
        if (
            directory.parent != RADIO_ROOT
            or json.loads((directory / "native-owner.json").read_text()) != OWNER
        ):
            raise ValueError("expected owned native experiment")
        self.source = source
        self.path = str(directory / "mqtt.sock")
        self.ready = False
        self.next_connect = 0
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, transport="unix")
        self.client.max_queued_messages_set(1)
        self.client.on_connect = self._connected
        self.client.on_disconnect = self._disconnected
        self.client.on_message = self._message

    def _connected(self, client, _userdata, _flags, reason, _properties):
        self.ready = not reason.is_failure
        if self.ready and self.source:
            client.subscribe(TOPIC, qos=0)

    def _disconnected(self, *_):
        self.ready = False
        if self.source:
            self.source.disconnect()

    def _message(self, _client, _userdata, message):
        if self.source:
            self.source.receive(message.topic, message.payload, retained=message.retain)

    def poll(self):
        now = time.monotonic()
        if not self.client.is_connected() and now >= self.next_connect:
            self.next_connect = now + 1
            try:
                self.client.connect(self.path, keepalive=10)
            except OSError:
                self._disconnected()
                return
        result = self.client.loop(timeout=0)
        if result not in (mqtt.MQTT_ERR_SUCCESS, mqtt.MQTT_ERR_AGAIN) and self.ready:
            self._disconnected()

    def publish(self, observed, ssid):
        self.poll()
        if not self.ready:
            return False
        result = self.client.publish(TOPIC, encode_stations(observed, ssid), qos=0, retain=False)
        self.poll()
        return result.rc == mqtt.MQTT_ERR_SUCCESS

    def close(self):
        self.client.disconnect()
        self._disconnected()
