"""Tests for scripts/check_mil_spice.py."""

from __future__ import annotations

from scripts.check_mil_spice import (
    _check_load_step,
    _check_pi_frequency,
    _check_startup_brownout,
    _parse_meas_load_step,
    _parse_meas_pi,
    _parse_meas_startup_brownout,
)


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


def test_parse_meas_startup_brownout_extracts_expected_keys() -> None:
    text = """
su_rise_5v = 8.0e-3
bo_dip_5v = 1.8e-1
bo_recover_5v = 4.5e-4
irrelevant = 1.0
"""
    parsed = _parse_meas_startup_brownout(text)
    assert parsed["su_rise_5v"] == 8.0e-3
    assert parsed["bo_dip_5v"] == 1.8e-1
    assert parsed["bo_recover_5v"] == 4.5e-4
    assert "irrelevant" not in parsed


def test_check_startup_brownout_passes_for_good_metrics() -> None:
    metrics = {
        "su_rise_5v": 8e-3,
        "su_rise_3v3": 7e-3,
        "su_rise_1v8": 6e-3,
        "bo_dip_5v": 0.20,
        "bo_dip_3v3": 0.10,
        "bo_dip_1v8": 0.08,
        "bo_recover_5v": 6e-4,
        "bo_recover_3v3": 5e-4,
        "bo_recover_1v8": 4e-4,
    }
    results = _check_startup_brownout(metrics)
    assert results
    assert all(r.passed for r in results)


def test_check_startup_brownout_fails_on_slow_startup() -> None:
    metrics = {"su_rise_5v": 20e-3}  # 20 ms > 12 ms target
    results = _check_startup_brownout(metrics)
    assert len(results) == 1
    assert not results[0].passed
    assert "startup" in results[0].name.lower()


def test_check_startup_brownout_fails_on_large_brownout_dip() -> None:
    metrics = {"bo_dip_3v3": 0.30}  # 300 mV > 220 mV target
    results = _check_startup_brownout(metrics)
    assert len(results) == 1
    assert not results[0].passed
    assert "brownout" in results[0].name.lower()


def test_parse_meas_pi_extracts_expected_keys() -> None:
    text = """
pi_zout_5v_lf = 9.0e-2
pi_psrr_5v_lf = 26.5
pi_zout_3v3_hf = 7.5e-2
noise_metric = 1.0
"""
    parsed = _parse_meas_pi(text)
    assert parsed["pi_zout_5v_lf"] == 9.0e-2
    assert parsed["pi_psrr_5v_lf"] == 26.5
    assert parsed["pi_zout_3v3_hf"] == 7.5e-2
    assert "noise_metric" not in parsed


def test_check_pi_frequency_passes_for_good_values() -> None:
    pi = {
        "pi_zout_5v_lf": 0.10,  # 100 mOhm <= 120 mOhm
        "pi_psrr_5v_lf": 24.0,  # >= 20 dB
        "pi_zout_3v3_hf": 0.06,  # 60 mOhm <= 80 mOhm
        "pi_psrr_3v3_hf": 28.0,  # >= 25 dB
    }
    results = _check_pi_frequency(pi)
    assert results
    assert all(r.passed for r in results)


def test_check_pi_frequency_flags_bad_values() -> None:
    pi = {
        "pi_zout_5v_lf": 0.20,  # 200 mOhm > 120 mOhm
        "pi_psrr_1v8_hf": 24.0,  # < 30 dB
    }
    results = _check_pi_frequency(pi)
    assert len(results) == 2
    assert any(not r.passed and "impedance" in r.name.lower() for r in results)
    assert any(not r.passed and "psrr" in r.name.lower() for r in results)
