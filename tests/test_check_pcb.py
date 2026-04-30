from scripts.check_pcb import PcbParser, check_power_zone_fills, check_stackup


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
