"""Tests for scripts/check_robustness.py."""

from __future__ import annotations

from scripts.check_robustness import (
    Footprint,
    Track,
    Via,
    check_crystal_network,
    check_mechanical_vibration,
    check_power_ic_thermal_vias,
    check_power_track_robustness,
)


def test_power_track_robustness_passes() -> None:
    tracks = [Track(width_mm=1.0, net_id=1), Track(width_mm=0.9, net_id=2)]
    net_map = {1: "5V", 2: "VBUS"}
    result = check_power_track_robustness(tracks, net_map)
    assert result.passed


def test_power_track_robustness_fails_when_too_narrow() -> None:
    tracks = [Track(width_mm=0.6, net_id=1)]
    net_map = {1: "VBUS"}
    result = check_power_track_robustness(tracks, net_map)
    assert not result.passed


def test_power_ic_thermal_vias_detects_insufficient_vias() -> None:
    footprints = [Footprint(ref="U2", name="Reg", x_mm=10.0, y_mm=10.0)]
    vias = [Via(x_mm=10.5, y_mm=10.5)]
    bom = {"U2": "AP63203WU"}
    result = check_power_ic_thermal_vias(footprints, vias, bom)
    assert not result.passed


def test_crystal_network_passes_when_present() -> None:
    bom = {"Y1": "16Mhz", "C11": "10p", "C12": "10p"}
    fps = [Footprint(ref="Y1", name="Crystal", x_mm=0.0, y_mm=0.0)]
    vias = [Via(x_mm=1.0, y_mm=1.0), Via(x_mm=2.0, y_mm=0.5)]
    results = check_crystal_network(bom, fps, vias)
    assert all(r.passed for r in results)


def test_mechanical_vibration_checks_mounting_and_margin() -> None:
    fps = [
        Footprint(ref="MH1", name="MountingHole:MountingHole_4.3mm", x_mm=2.0, y_mm=2.0),
        Footprint(ref="MH2", name="MountingHole:MountingHole_4.3mm", x_mm=98.0, y_mm=2.0),
        Footprint(ref="MH3", name="MountingHole:MountingHole_4.3mm", x_mm=2.0, y_mm=48.0),
        Footprint(ref="MH4", name="MountingHole:MountingHole_4.3mm", x_mm=98.0, y_mm=48.0),
        Footprint(ref="L1", name="Inductor_SMD:L_0805_2012Metric", x_mm=10.0, y_mm=10.0),
    ]
    results = check_mechanical_vibration(fps, (0.0, 0.0, 100.0, 50.0))
    assert results[0].passed
    assert results[1].passed
