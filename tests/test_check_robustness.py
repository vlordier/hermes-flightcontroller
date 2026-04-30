"""Tests for scripts/check_robustness.py."""

from __future__ import annotations

from scripts.check_robustness import (
    Footprint,
    Track,
    Via,
    check_connector_anchoring,
    check_crystal_network,
    check_current_density,
    check_decoupling_proximity,
    check_diff_pair_length_matching,
    check_impedance_controlled_traces,
    check_mechanical_vibration,
    check_power_ic_thermal_vias,
    check_power_track_robustness,
    check_test_point_coverage,
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
    footprints = [Footprint(ref="U2", name="Reg", x_mm=10.0, y_mm=10.0, th_pads=0, smd_pads=6)]
    vias = [Via(x_mm=10.5, y_mm=10.5)]
    bom = {"U2": "AP63203WU"}
    result = check_power_ic_thermal_vias(footprints, vias, bom)
    assert not result.passed


def test_crystal_network_passes_when_present() -> None:
    bom = {"Y1": "16Mhz", "C11": "10p", "C12": "10p"}
    fps = [Footprint(ref="Y1", name="Crystal", x_mm=0.0, y_mm=0.0, th_pads=0, smd_pads=4)]
    vias = [Via(x_mm=1.0, y_mm=1.0), Via(x_mm=2.0, y_mm=0.5)]
    results = check_crystal_network(bom, fps, vias)
    assert all(r.passed for r in results)


def test_connector_anchoring_passes_with_through_hole_connector() -> None:
    fps = [
        Footprint(ref="J1", name="Connector_USB", x_mm=5.0, y_mm=5.0, th_pads=4, smd_pads=4),
    ]
    result = check_connector_anchoring(fps, vias=[])
    assert result.passed


def test_connector_anchoring_fails_without_th_or_local_vias() -> None:
    fps = [
        Footprint(
            ref="J2", name="Connector_PinHeader", x_mm=10.0, y_mm=10.0, th_pads=0, smd_pads=6
        ),
    ]
    result = check_connector_anchoring(fps, vias=[])
    assert not result.passed


def test_mechanical_vibration_checks_mounting_and_margin() -> None:
    fps = [
        Footprint(
            ref="MH1",
            name="MountingHole:MountingHole_4.3mm",
            x_mm=2.0,
            y_mm=2.0,
            th_pads=1,
            smd_pads=0,
        ),
        Footprint(
            ref="MH2",
            name="MountingHole:MountingHole_4.3mm",
            x_mm=98.0,
            y_mm=2.0,
            th_pads=1,
            smd_pads=0,
        ),
        Footprint(
            ref="MH3",
            name="MountingHole:MountingHole_4.3mm",
            x_mm=2.0,
            y_mm=48.0,
            th_pads=1,
            smd_pads=0,
        ),
        Footprint(
            ref="MH4",
            name="MountingHole:MountingHole_4.3mm",
            x_mm=98.0,
            y_mm=48.0,
            th_pads=1,
            smd_pads=0,
        ),
        Footprint(
            ref="L1",
            name="Inductor_SMD:L_0805_2012Metric",
            x_mm=10.0,
            y_mm=10.0,
            th_pads=0,
            smd_pads=2,
        ),
    ]
    results = check_mechanical_vibration(fps, (0.0, 0.0, 100.0, 50.0))
    assert results[0].passed
    assert results[1].passed


# ── check_current_density ─────────────────────────────────────────────────────


def test_current_density_passes_when_wide_enough() -> None:
    tracks = [Track(width_mm=1.1, net_id=1), Track(width_mm=0.9, net_id=2)]
    net_map = {1: "VBUS", 2: "5V"}
    results = check_current_density(tracks, net_map)
    assert all(r.passed for r in results)


def test_current_density_fails_vbus_too_narrow() -> None:
    tracks = [Track(width_mm=0.5, net_id=1)]
    net_map = {1: "VBUS"}
    results = check_current_density(tracks, net_map)
    vbus_result = next(r for r in results if "VBUS" in r.name)
    assert not vbus_result.passed


def test_current_density_skips_absent_nets() -> None:
    # 3V3 net not on board → no result for it
    tracks = [Track(width_mm=1.2, net_id=1)]
    net_map = {1: "VBUS"}
    results = check_current_density(tracks, net_map)
    names = [r.name for r in results]
    assert not any("3V3" in n for n in names)


# ── check_decoupling_proximity ────────────────────────────────────────────────


def test_decoupling_proximity_passes_when_cap_nearby() -> None:
    fps = [
        Footprint(ref="U1", name="IC_STM32", x_mm=10.0, y_mm=10.0, th_pads=0, smd_pads=64),
        Footprint(ref="C1", name="C_0402", x_mm=12.0, y_mm=10.5, th_pads=0, smd_pads=2),
    ]
    bom = {"U1": "STM32F405", "C1": "100nF"}
    results = check_decoupling_proximity(fps, bom)
    assert all(r.passed for r in results)


def test_decoupling_proximity_fails_no_nearby_cap() -> None:
    fps = [
        Footprint(ref="U1", name="IC_STM32", x_mm=10.0, y_mm=10.0, th_pads=0, smd_pads=64),
        Footprint(ref="C1", name="C_0402", x_mm=50.0, y_mm=50.0, th_pads=0, smd_pads=2),
    ]
    bom = {"U1": "STM32F405", "C1": "100nF"}
    results = check_decoupling_proximity(fps, bom)
    u1_result = next(r for r in results if "U1" in r.name)
    assert not u1_result.passed


# ── check_impedance_controlled_traces ─────────────────────────────────────────


def test_impedance_passes_for_correctly_sized_usb_tracks() -> None:
    tracks = [Track(width_mm=0.17, net_id=1), Track(width_mm=0.17, net_id=2)]
    net_map = {1: "USB_DP", 2: "USB_DM"}
    results = check_impedance_controlled_traces(tracks, net_map)
    assert all(r.passed for r in results)


def test_impedance_fails_for_oversized_usb_tracks() -> None:
    tracks = [Track(width_mm=0.8, net_id=1), Track(width_mm=0.8, net_id=2)]
    net_map = {1: "D+", 2: "D-"}
    results = check_impedance_controlled_traces(tracks, net_map)
    assert any(not r.passed for r in results)


def test_impedance_warns_when_no_usb_nets() -> None:
    tracks = [Track(width_mm=0.25, net_id=1)]
    net_map = {1: "SPI_MOSI"}
    results = check_impedance_controlled_traces(tracks, net_map)
    assert len(results) == 1
    assert not results[0].passed


# ── check_diff_pair_length_matching ──────────────────────────────────────────


def test_diff_pair_length_matching_passes_within_skew() -> None:
    # D+ = 3-4-5 right triangle → 5 mm; D- = 5.1 mm → skew 0.1 mm < 2.5 mm
    tracks = [
        Track(width_mm=0.17, net_id=1, start=(0.0, 0.0), end=(3.0, 4.0)),  # len=5 mm
        Track(width_mm=0.17, net_id=2, start=(0.0, 0.0), end=(5.1, 0.0)),  # len=5.1 mm
    ]
    net_map = {1: "USB_DP", 2: "USB_DM"}
    results = check_diff_pair_length_matching(tracks, net_map)
    assert len(results) == 1
    assert results[0].passed


def test_diff_pair_length_matching_fails_on_large_skew() -> None:
    # D+ = 10 mm; D- = 20 mm → skew 10 mm > 2.5 mm
    tracks = [
        Track(width_mm=0.17, net_id=1, start=(0.0, 0.0), end=(10.0, 0.0)),
        Track(width_mm=0.17, net_id=2, start=(0.0, 0.0), end=(20.0, 0.0)),
    ]
    net_map = {1: "D+", 2: "D-"}
    results = check_diff_pair_length_matching(tracks, net_map)
    assert len(results) == 1
    assert not results[0].passed
    assert "skew" in results[0].detail.lower()


def test_diff_pair_length_matching_warns_when_nets_missing() -> None:
    tracks = [Track(width_mm=0.25, net_id=1)]
    net_map = {1: "SPI_MOSI"}
    results = check_diff_pair_length_matching(tracks, net_map)
    assert not results[0].passed


# ── check_test_point_coverage ─────────────────────────────────────────────────


def test_test_point_coverage_passes_with_enough_tps() -> None:
    fps = [
        Footprint(ref="TP1", name="TestPoint", x_mm=1.0, y_mm=1.0, th_pads=1, smd_pads=0),
        Footprint(ref="TP2", name="TestPoint", x_mm=2.0, y_mm=1.0, th_pads=1, smd_pads=0),
        Footprint(ref="U1", name="IC_STM32", x_mm=10.0, y_mm=10.0, th_pads=0, smd_pads=64),
    ]
    result = check_test_point_coverage(fps)
    assert result.passed


def test_test_point_coverage_fails_with_no_tps() -> None:
    fps = [
        Footprint(ref="U1", name="IC_STM32", x_mm=10.0, y_mm=10.0, th_pads=0, smd_pads=64),
        Footprint(ref="C1", name="C_0402", x_mm=12.0, y_mm=10.0, th_pads=0, smd_pads=2),
    ]
    result = check_test_point_coverage(fps)
    assert not result.passed
    assert "0" in result.detail
