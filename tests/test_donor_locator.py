import pytest

import _donor


def test_donor_root_comes_from_env(monkeypatch, tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    monkeypatch.setenv("NEXUS_RUNTIME_FROZEN_DONOR_ROOT", str(tmp_path))
    assert _donor.donor_root() == tmp_path
    assert (_donor.donor_root() / "marker.txt").exists()


def test_donor_root_skips_when_unprovisioned(monkeypatch, tmp_path):
    monkeypatch.delenv("NEXUS_RUNTIME_FROZEN_DONOR_ROOT", raising=False)
    monkeypatch.setattr(_donor, "DEFAULT_DONOR_ROOT", tmp_path / "absent")
    with pytest.raises(pytest.skip.Exception) as exc:
        _donor.require_donor_root()
    assert str(exc.value).startswith("GAP: frozen donor not provisioned")
