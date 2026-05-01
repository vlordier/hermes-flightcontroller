from __future__ import annotations

from typing import Any

from scripts.check_pcb import (
    PcbParser,
    check_power_net_islands,
    check_power_zone_fills,
    check_stackup,
)


def test_stackup_detection() -> None:
    # Minimal 4-layer stackup S-expression
    pcb_text = '(kicad_pcb (version 20211014) (layers (0 "F.Cu" signal) (1 "In1.Cu" power) (2 "In2.Cu" mixed) (31 "B.Cu" signal)))'
    parser = PcbParser(pcb_text)
    layers = parser.get_layers()

    result = check_stackup(layers)
    assert result.passed is True
    assert "F.Cu / In1.Cu / In2.Cu / B.Cu" in result.detail


def test_track_width_parsing() -> None:
    # One valid track (0.3mm), one violation (0.1mm)
    pcb_text = """(kicad_pcb (version 20211014)
      (segment (start 0 0) (end 1 1) (width 0.3) (layer "F.Cu") (net 1))
      (segment (start 2 2) (end 3 3) (width 0.1) (layer "B.Cu") (net 2))
    )"""
    parser = PcbParser(pcb_text)
    tracks = parser.get_tracks()

    assert len(tracks) == 2
    assert tracks[0]["width"] == 0.3
    assert tracks[1]["width"] == 0.1
    assert tracks[1]["layer"] == "B.Cu"


def test_has_edge_cuts() -> None:
    pcb_text = '(kicad_pcb (gr_line (start 0 0) (end 10 0) (layer "Edge.Cuts") (width 0.1)))'
    parser = PcbParser(pcb_text)
    assert parser.has_edge_cuts() is True

    pcb_text_no_edge = '(kicad_pcb (gr_line (start 0 0) (end 10 0) (layer "F.SilkS") (width 0.1)))'
    parser_no_edge = PcbParser(pcb_text_no_edge)
    assert parser_no_edge.has_edge_cuts() is False


def test_power_zone_fills_passes_when_all_present() -> None:
    zones = [
        {"net_name": "5V", "layer": "F.Cu"},
        {"net_name": "3V3", "layer": "In2.Cu"},
        {"net_name": "1V8", "layer": "B.Cu"},
        {"net_name": "GND", "layer": "In1.Cu"},
    ]
    result = check_power_zone_fills(zones)
    assert result.passed


def test_power_zone_fills_fails_when_missing() -> None:
    zones = [
        {"net_name": "GND", "layer": "In1.Cu"},
        {"net_name": "5V", "layer": "F.Cu"},
    ]
    result = check_power_zone_fills(zones)
    assert not result.passed
    assert "3V3" in result.detail or "1V8" in result.detail


# ── Power net island detection ────────────────────────────────────────────────


def test_power_net_islands_connected_graph_passes() -> None:
    """Three segments forming a chain should be one connected component → PASS."""
    # 5V net: A-B, B-C  → fully connected
    segments = [
        {"start": (0.0, 0.0), "end": (1.0, 0.0), "net": 1, "width": 0.8, "layer": "F.Cu"},
        {"start": (1.0, 0.0), "end": (2.0, 0.0), "net": 1, "width": 0.8, "layer": "F.Cu"},
        {"start": (2.0, 0.0), "end": (3.0, 0.0), "net": 1, "width": 0.8, "layer": "F.Cu"},
    ]
    net_map = {1: "5V"}
    results = check_power_net_islands(segments, net_map)
    fv_results = [r for r in results if "5V" in r.name]
    assert fv_results, "Expected at least one result for 5V net"
    assert fv_results[0].passed, fv_results[0].detail


def test_power_net_islands_disconnected_fails() -> None:
    """Two isolated segment groups for the same net should produce a FAIL."""
    # Segment A at (0,0)-(1,0); segment B at (5,5)-(6,5) — no shared endpoint
    segments = [
        {"start": (0.0, 0.0), "end": (1.0, 0.0), "net": 2, "width": 0.8, "layer": "F.Cu"},
        {"start": (5.0, 5.0), "end": (6.0, 5.0), "net": 2, "width": 0.8, "layer": "F.Cu"},
    ]
    net_map = {2: "3V3"}
    results = check_power_net_islands(segments, net_map)
    v33_results = [r for r in results if "3V3" in r.name]
    assert v33_results, "Expected a result for 3V3 net"
    assert not v33_results[0].passed, "Disconnected segments should fail"
    assert "island" in v33_results[0].detail.lower()


def test_power_net_islands_no_segments_skipped() -> None:
    """If a critical net has no segments on the board it should be silently skipped."""
    segments: list[dict[str, Any]] = []
    net_map = {1: "SOME_SIGNAL"}  # None of the critical nets match
    results = check_power_net_islands(segments, net_map)
    # Should return one informational FAIL indicating no critical nets found
    assert any("no critical" in r.detail.lower() for r in results)
