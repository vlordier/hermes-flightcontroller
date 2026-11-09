"""Tests for PCB integrity checks (net naming, via annular rings, silkscreen refs).

These checks were consolidated into check_pcb.py from the former
check_pcb_integrity.py module.
"""

from __future__ import annotations

from scripts.check_pcb import (
    CheckResult,
    PcbParser,
    check_footprint_silkscreen,
    check_net_names,
    check_vias,
)


def _vias_from_pcb(content: str) -> list[dict[str, float]]:
    return PcbParser(content).get_vias()


def _nets_from_pcb(content: str) -> list[str]:
    return PcbParser(content).get_nets()


def _footprints_from_pcb(content: str) -> list[tuple[str, bool]]:
    return PcbParser(content).get_footprint_refs()


# ── Annular ring checks ────────────────────────────────────────────────────────


def test_annular_ring_violation() -> None:
    """Via with drill 0.3 and size 0.5 → annular ring 0.10 mm < 0.125 mm threshold."""
    content = """
(kicad_pcb (version 20211014) (generator pcbnew)
  (net 0 "")
  (net 1 "GND")
  (via (at 100 100) (size 0.5) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
)
"""
    vias = _vias_from_pcb(content)
    results = check_vias(vias)
    annular_result = next(r for r in results if r.name == "Via annular rings")
    assert isinstance(annular_result, CheckResult)
    assert annular_result.passed is False
    assert "annular ring" in annular_result.detail.lower()


def test_annular_ring_pass() -> None:
    """Via with drill 0.3 and size 0.8 → annular ring 0.25 mm >= threshold."""
    content = """
(kicad_pcb (version 20211014) (generator pcbnew)
  (net 0 "")
  (net 1 "GND")
  (via (at 100 100) (size 0.8) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
)
"""
    vias = _vias_from_pcb(content)
    results = check_vias(vias)
    annular_result = next(r for r in results if r.name == "Via annular rings")
    assert annular_result.passed is True


# ── Net naming checks ──────────────────────────────────────────────────────────


def test_net_naming_violation_lowercase() -> None:
    """Net with lowercase characters (non-hierarchical) should fail."""
    content = """
(kicad_pcb (version 20211014) (generator pcbnew)
  (net 0 "")
  (net 1 "GND")
  (net 2 "vcc_dirty")
)
"""
    nets = _nets_from_pcb(content)
    result = check_net_names(nets)
    assert result.passed is False
    assert "lowercase" in result.detail


def test_net_naming_pass() -> None:
    """All-uppercase nets should pass."""
    content = """
(kicad_pcb (version 20211014) (generator pcbnew)
  (net 0 "")
  (net 1 "GND")
  (net 2 "VCC")
  (net 3 "5V")
)
"""
    nets = _nets_from_pcb(content)
    result = check_net_names(nets)
    assert result.passed is True


def test_net_naming_hierarchical_allowed() -> None:
    """Hierarchical nets (starting with /) are exempt from the uppercase rule."""
    content = """
(kicad_pcb (version 20211014) (generator pcbnew)
  (net 0 "")
  (net 1 "GND")
  (net 2 "/mcu/uart_tx")
)
"""
    nets = _nets_from_pcb(content)
    result = check_net_names(nets)
    assert result.passed is True


# ── Silkscreen / reference designator checks ──────────────────────────────────


def test_footprint_silkscreen_pass() -> None:
    """Footprint with a reference property should pass."""
    footprints: list[tuple[str, bool]] = [("Package_SMD:R_0603", True)]
    result = check_footprint_silkscreen(footprints)
    assert result.passed is True


def test_footprint_silkscreen_missing() -> None:
    """Footprint without a reference property should fail."""
    footprints: list[tuple[str, bool]] = [("Package_SMD:C_0402", False)]
    result = check_footprint_silkscreen(footprints)
    assert result.passed is False
    assert "missing" in result.detail.lower()
