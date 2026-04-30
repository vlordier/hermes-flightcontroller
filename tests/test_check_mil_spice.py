"""Tests for scripts/check_mil_spice.py."""

from __future__ import annotations

from scripts.check_mil_spice import _check_load_step, _parse_meas_load_step


def test_parse_meas_load_step_extracts_expected_keys() -> None:
    text = """
ls_dip_5v = 1.20e-1
ls_recover_5v = 2.50e-4
ls_dip_3v3 = 5.00e-2
some_other_metric = 7.0
"""
    parsed = _parse_meas_load_step(text)
    assert parsed["ls_dip_5v"] == 0.12
    assert parsed["ls_recover_5v"] == 2.5e-4
    assert parsed["ls_dip_3v3"] == 0.05
    assert "some_other_metric" not in parsed


def test_check_load_step_passes_for_good_droop_and_recovery() -> None:
    load_step = {
        "ls_dip_5v": 0.10,  # 100 mV < 250 mV target
        "ls_recover_5v": 2.0e-4,  # 200 us < 400 us target
        "ls_dip_3v3": 0.08,  # 80 mV < 160 mV target
        "ls_recover_3v3": 2.0e-4,  # 200 us < 300 us target
        "ls_dip_1v8": 0.06,  # 60 mV < 120 mV target
        "ls_recover_1v8": 1.5e-4,  # 150 us < 250 us target
    }

    results = _check_load_step(load_step)
    assert results
    assert all(r.passed for r in results)


def test_check_load_step_fails_for_excessive_droop() -> None:
    load_step = {"ls_dip_5v": 0.35}  # 350 mV > 250 mV target
    results = _check_load_step(load_step)
    assert len(results) == 1
    assert not results[0].passed
    assert "droop" in results[0].name.lower()


def test_check_load_step_fails_for_slow_recovery() -> None:
    load_step = {"ls_recover_3v3": 5.0e-4}  # 500 us > 300 us target
    results = _check_load_step(load_step)
    assert len(results) == 1
    assert not results[0].passed
    assert "recovery" in results[0].name.lower()
