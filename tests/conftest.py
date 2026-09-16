import pytest


def pytest_collection_modifyitems(config, items):
    # Deliberately selected gated suites execute and fail with an explicit reason.
    # Ordinary CI excludes them; no hardware network is ever auto-discovered.
    selected = config.option.markexpr
    if not any(name in selected for name in ("wire", "hardware", "external")):
        for item in items:
            if any(name in item.keywords for name in ("wire", "hardware", "external")):
                item.add_marker(pytest.mark.skip(reason="select gated suite explicitly"))
