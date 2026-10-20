# SPDX-License-Identifier: Apache-2.0
"""EMOSA's log lines start with their time, ISO 8601 in UTC to the millisecond, as the C
programs' (c/tests/log_rdk.c the_line_time)."""

import logging
import re

import pytest

from emosa import logs

pytestmark = pytest.mark.unit


def test_a_lines_time_is_iso_8601_utc_to_the_millisecond():
    assert logs.stamp(1792524191.123456) == "2026-10-20T19:23:11.123Z"
    assert logs.stamp(0.007) == "1970-01-01T00:00:00.007Z"


def test_a_line_is_its_time_level_component_and_message():
    record = logging.LogRecord("emosa.forward", logging.INFO, __file__, 1, "port %d", (6640,), None)
    record.created = 1792524191.5
    line = logs.UtcFormatter(logs.FORMAT).format(record)
    assert line == "2026-10-20T19:23:11.500Z INFO emosa.forward: port 6640"
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z ", line)
