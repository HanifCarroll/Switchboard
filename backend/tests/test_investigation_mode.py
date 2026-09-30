import pytest

from switchboard.investigation.mode import get_investigation_mode


def test_live_mode_is_the_safe_default(monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_INVESTIGATION_MODE", raising=False)
    monkeypatch.delenv("SWITCHBOARD_RUNTIME", raising=False)

    assert get_investigation_mode() == "live"


def test_fixture_mode_requires_local_runtime(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_INVESTIGATION_MODE", "fixture")
    monkeypatch.setenv("SWITCHBOARD_RUNTIME", "aws")

    with pytest.raises(ValueError, match="only in local development"):
        get_investigation_mode()
