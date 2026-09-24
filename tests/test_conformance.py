"""The reference implementation reproduces every conformance vector in spec/conformance."""

import pytest

from emosa_lab.conformance import VECTOR_SETS, check

pytestmark = pytest.mark.unit


def test_every_vector_set_is_reproduced():
    assert check() == [], "run: python -m emosa_lab.conformance generate (if the spec changed)"
    assert len(VECTOR_SETS) == 5
