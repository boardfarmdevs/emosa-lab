import asyncio

import pytest

from emosa.simulation.discovery import run

pytestmark = pytest.mark.ovsdb


def test_real_database_requires_fresh_discovery_after_reconnect(tmp_path):
    result = asyncio.run(run(tmp_path / "discovery"))
    assert result["passed"] and result["monitor_read_only"]
    assert result["pod_initiated_connection"] and result["adapter_config_writes"] == 0
    assert len(result["stages"]) == 10
    assert result["session"]["state"] == "discovered_read_only"
    assert result["search_mids"] == [1, 2, 3]
    for stage in (
        "before_discovery",
        "incompatible_query",
        "disconnected",
        "reconnected_before_discovery",
    ):
        assert result["stages"][stage]["response_frames"] == 0
