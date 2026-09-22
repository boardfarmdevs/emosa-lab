"""Audited subset of IEEE 802.11-2024 Table E-4 (printed pp. 5658–5660).

These channel sets are not regulatory permission or hardware capability. No
bandwidth is inferred here (in particular, class 81's spacing column is 25 MHz).
Additional classes require source review before the mapper can accept them.
"""

GLOBAL_OPERATING_CLASSES = {
    81: ("2.4G", tuple(range(1, 14))),
    82: ("2.4G", (14,)),
    83: ("2.4G", tuple(range(1, 10))),
    84: ("2.4G", tuple(range(5, 14))),
    115: ("5G", (36, 40, 44, 48)),
    116: ("5G", (36, 44)),
    117: ("5G", (40, 48)),
    128: ("5G", (42, 58, 106, 122, 138, 155, 171)),
}

# Table E-4 uses center-channel numbers for this 80 MHz class. These cannot
# validate Wifi_Radio_State.channel, which is the synthetic primary channel.
CENTER_CHANNEL_CLASSES = frozenset({128})
