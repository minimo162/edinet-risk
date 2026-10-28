from datetime import datetime

import pytest

from edinet_risk.net import edinet_api


@pytest.fixture(autouse=True)
def _outside_doclist_quiet_hours(request, monkeypatch):
    """Offline tests must not depend on the wall clock: pin it to noon JST (documents.json is not
    called 00:00-01:00, plan §6-4). Live tests keep the real clock."""
    if request.node.get_closest_marker("live") is None:
        monkeypatch.setattr(edinet_api, "_now_jst", lambda: datetime(2026, 10, 28, 12, 0, tzinfo=edinet_api.JST))
