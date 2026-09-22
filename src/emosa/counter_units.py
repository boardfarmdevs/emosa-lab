"""EasyMesh 6.1 section 9.1's counter-unit choice with explicit peer facts.

No observed peer, negotiation, telemetry conversion or ODH transport is provided.
The caller must establish the peer profile and capability in the actual exchange.
"""

from emosa.errors import EmosaError, Reason


def select_counter_units(*, controller_profile, controller_kib_mib, preferred_scaled_unit):
    if type(controller_profile) is not int or controller_profile not in (1, 2, 3):
        raise EmosaError(Reason.MISSING_PREREQUISITE, "known controller profile required")
    if controller_kib_mib is not None and type(controller_kib_mib) is not bool:
        raise EmosaError(Reason.INVALID_INPUT, "invalid counter capability fact")
    if controller_profile == 1 and controller_kib_mib is None:
        raise EmosaError(Reason.MISSING_PREREQUISITE, "Profile-1 counter capability is unknown")
    if controller_profile == 1 and controller_kib_mib is False:
        return 0
    if type(preferred_scaled_unit) is not int or preferred_scaled_unit not in (1, 2):
        raise EmosaError(Reason.INVALID_INPUT, "explicit KiB or MiB choice required")
    return preferred_scaled_unit
