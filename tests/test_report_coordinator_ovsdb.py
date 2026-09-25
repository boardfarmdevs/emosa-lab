import asyncio

import pytest

from emosa.errors import EmosaError
from emosa.opensync.mapping import check_results
from emosa_lab.simulation.coordinator import run
from emosa_lab.simulation.report_source import COLUMNS, DatabaseReportFixture

pytestmark = pytest.mark.ovsdb


def test_real_connecting_database_state_changes_withdrawal_and_reconnect(tmp_path):
    result = asyncio.run(run(tmp_path / "coordinator"))
    assert result["passed"] and result["monitor_read_only"]
    assert result["pod_initiated_connection"] and result["adapter_config_writes"] == 0
    assert len(result["stages"]) == 8
    assert result["stages"]["config_only_keeps_observed_ssid"]["response_frames"] > 0
    assert not result["stages"]["unknown_client_age_withdraws_report"]["response_frames"]
    assert not result["stages"]["database_disconnect_withdraws_report"]["response_frames"]


def test_report_monitor_cannot_write_and_incompatible_security_withdraws_source():
    async def scenario():
        fixture = DatabaseReportFixture()
        try:
            await fixture.start()
            assert not {"wpa_psks", "security", "dpp_netaccesskey_hex"} & {
                c for cols in COLUMNS.values() for c in cols
            }
            with pytest.raises(EmosaError):
                await fixture.session.transact(
                    [
                        {
                            "op": "update",
                            "table": "Wifi_VIF_Config",
                            "where": [],
                            "row": {"ssid": "forbidden"},
                        }
                    ]
                )
            check_results(
                await fixture.admin.transact(
                    [
                        {
                            "op": "update",
                            "table": "Wifi_VIF_State",
                            "where": [],
                            "row": {"wpa_key_mgmt": ["set", ["sae"]]},
                        }
                    ]
                ),
                [1],
            )
            assert not await fixture.refresh()
            assert fixture.source.current() is None
            # Optional channel cleared in a valid database row is an unknown
            # fact, not a Python comparison error or a presumed channel 6.
            check_results(
                await fixture.admin.transact(
                    [
                        {
                            "op": "update",
                            "table": "Wifi_VIF_State",
                            "where": [],
                            "row": {"wpa_key_mgmt": ["set", ["wpa2-psk"]]},
                        },
                        {
                            "op": "update",
                            "table": "Wifi_Radio_State",
                            "where": [],
                            "row": {"channel": ["set", []]},
                        },
                    ]
                ),
                [1, 1],
            )
            assert not await fixture.refresh()
            assert fixture.source.current() is None
        finally:
            await fixture.close()

    asyncio.run(scenario())
