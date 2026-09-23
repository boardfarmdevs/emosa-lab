"""Selected BBF TR-181 2.17.0 representations for internal report publishers.

This bridge requires already qualified DataElements values, not raw kernel or
OpenSync counters. BBF is the stated source; equivalence to the WFA DEr3 package
is still pending. It opens no connection and enables no runtime report source.
See doc/protocol/bbf-data-elements.md for the exact namespaces and limitations.
"""

import struct
from collections.abc import Mapping

from emosa.errors import EmosaError, Reason
from emosa.wire.ap_metrics import APExtendedMetrics, RadioMetrics, StationLinkMetrics
from emosa.wire.cmdu import Tlv, invalid, uint

BSS_COUNTERS = (
    "UnicastBytesSent",
    "UnicastBytesReceived",
    "MulticastBytesSent",
    "MulticastBytesReceived",
    "BroadcastBytesSent",
    "BroadcastBytesReceived",
)


def parameter(values, name, bits, *, statistics=False):
    if not isinstance(values, Mapping):
        invalid("BBF parameter mapping required")
    if name not in values or values[name] is None:
        raise EmosaError(Reason.NOT_READY, f"BBF {name} unavailable")
    value = uint(values[name], bits, f"BBF {name}")
    if statistics and value == (1 << bits) - 1:
        # StatsCounter64/32 reserve maxval; ordinary unsigned types do not.
        raise EmosaError(Reason.NOT_READY, f"BBF {name} statistics unavailable")
    return value


def fits_wire(value, name):
    if value > 0xFFFFFFFF:
        # Tables 84/85 do not state Table 58's explicit rollover rule. Do not
        # silently truncate a wider BBF counter or borrow that rule here.
        raise EmosaError(Reason.NOT_READY, f"BBF {name} exceeds reviewed wire range")
    return value


def collection_interval_tlv(milliseconds):
    """Table 82 / Device.CollectionInterval, an unsigned 32-bit millisecond value.

    This is the fastest actual measurement cadence, not the controller's report
    period or a configured polling loop. BBF supplies no positive lower bound;
    zero is representable but is not a claim about any live publisher.
    """
    return Tlv(0xC5, struct.pack("!I", uint(milliseconds, 32, "collection interval")))


def radio_metrics(ruid, values):
    """Radio.*: encoded ANPI plus three already scaled 0..255 utilizations.

    Noise is not signed dBm; there is no dBm, percent or hwsim survey conversion.
    The publisher must qualify the primary channel and averaging interval.
    """
    result = RadioMetrics(
        ruid,
        *(
            parameter(values, name, 8)
            for name in ("Noise", "Transmit", "ReceiveSelf", "ReceiveOther")
        ),
    )
    result.tlv()
    return result


def ap_extended_metrics(bssid, values, *, byte_units):
    """BSS.* StatsCounter64 bytes to Table 84's six counters in declared units.

    Missing and sentinel values withhold the result. Divide before checking the
    wire width, discarding sub-unit remainders by flooring. Counter epoch, traffic classes
    and the selected non-MLD scope still require publisher qualification.
    """
    if type(byte_units) is not int or byte_units not in (0, 1, 2):
        invalid("explicit advertised byte counter units required")
    scale = (1, 1024, 1024 * 1024)[byte_units]
    counts = tuple(
        fits_wire(parameter(values, name, 64, statistics=True) // scale, name)
        for name in BSS_COUNTERS
    )
    result = APExtendedMetrics(bssid, counts, byte_units)
    result.tlv()
    return result


def station_link_metrics(values, *, earliest_measurement, downlink_mac_mbps, uplink_mac_mbps, rcpi):
    """STA.* last rates in kbps and accumulated RX/TX duration in milliseconds.

    BBF duration is unsignedLong; Table 85 has four octets. Reject overflow until
    its mapping is resolved. Durations are not percentages or report-period
    deltas. The base MAC-throughput estimates and RCPI remain separately qualified
    inputs: last PHY rates cannot establish achievable MAC throughput, and BBF's
    SignalStrength description/units are inconsistent in this edition.
    """
    return StationLinkMetrics(
        earliest_measurement,
        downlink_mac_mbps,
        uplink_mac_mbps,
        rcpi,
        parameter(values, "LastDataDownlinkRate", 32),
        parameter(values, "LastDataUplinkRate", 32),
        fits_wire(parameter(values, "UtilizationReceive", 64), "UtilizationReceive"),
        fits_wire(parameter(values, "UtilizationTransmit", 64), "UtilizationTransmit"),
    )
