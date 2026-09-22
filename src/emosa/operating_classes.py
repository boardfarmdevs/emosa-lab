"""Audited subset of IEEE 802.11-2024 Table E-4 (printed pp. 5658–5659).

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
}
