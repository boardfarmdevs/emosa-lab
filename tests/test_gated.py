import pytest


@pytest.mark.wire
def test_wire_acceptance_requires_P0():
    pytest.fail(
        "MISSING_PREREQUISITE P0: exact normative matrix, independent vectors "
        "and wire endpoint missing; no wire acceptance executed"
    )


@pytest.mark.hardware
def test_hardware_acceptance_requires_M0():
    pytest.fail(
        "MISSING_PREREQUISITE M0: actual target/schema/trust/ownership/recovery/client "
        "qualification absent; no hardware accessed"
    )


@pytest.mark.external
def test_external_acceptance_requires_X1():
    pytest.fail(
        "MISSING_PREREQUISITE X1: actual EMOSA exchanges and peer recovery unqualified; "
        "the independent controller baseline does not establish interoperability"
    )
