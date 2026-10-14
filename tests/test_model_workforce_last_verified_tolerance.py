"""last_verified must not be in the future, with a one-day timezone allowance (UTC)."""

import datetime

import pytest
import yaml

from nexus_runtime.planning.services import model_workforce_policy as mwp
from nexus_runtime.planning.services.model_workforce_policy import (
    WorkforcePolicyLoader,
    WorkforcePolicyValidationError,
)

FIXED_TODAY = datetime.date(2026, 10, 14)


def _load_with(tmp_path, monkeypatch, last_verified):
    monkeypatch.setattr(mwp, "_utc_today", lambda: FIXED_TODAY)
    data = yaml.safe_load(mwp.WorkforcePolicyLoader().policy_path.read_text(encoding="utf-8"))
    data["last_verified"] = last_verified
    path = tmp_path / "model_workforce.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return WorkforcePolicyLoader(path).load()


def test_today_utc_accepted(tmp_path, monkeypatch):
    assert _load_with(tmp_path, monkeypatch, "2026-10-14").last_verified == "2026-10-14"


def test_tomorrow_utc_accepted_as_timezone_allowance(tmp_path, monkeypatch):
    assert _load_with(tmp_path, monkeypatch, "2026-10-15").last_verified == "2026-10-15"


@pytest.mark.parametrize("value", ["2026-10-16", "2027-01-01"])
def test_two_or_more_days_ahead_rejected(tmp_path, monkeypatch, value):
    with pytest.raises(WorkforcePolicyValidationError, match="in the future"):
        _load_with(tmp_path, monkeypatch, value)


@pytest.mark.parametrize("value", ["2026-10-13", "2020-01-01"])
def test_past_dates_still_accepted(tmp_path, monkeypatch, value):
    assert _load_with(tmp_path, monkeypatch, value).last_verified == value
