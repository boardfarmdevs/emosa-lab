# SPDX-License-Identifier: Apache-2.0
"""EMOSA's log lines: each starts with its time, ISO 8601 in UTC to the millisecond
(``2026-10-20T19:23:11.123Z``), then its level, its component and the message, the same as the C
programs' (c/src/log.c), so that a line can be set beside the controller's and the pod's logs
(a pod's session closed with no time on the line could not be placed, 20 October)."""

import logging
from datetime import UTC, datetime

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def stamp(seconds):
    """The time of a line: ISO 8601, UTC, milliseconds, ``Z``."""
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds")[:-6] + "Z"


class UtcFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return stamp(record.created)


def configure(level=logging.INFO):
    """The process's log lines on stderr, in EMOSA's format."""
    handler = logging.StreamHandler()
    handler.setFormatter(UtcFormatter(FORMAT))
    logging.basicConfig(level=level, handlers=[handler])
