"""Tests for scripts/check_manufacturing.py."""

from __future__ import annotations

from scripts.check_manufacturing import (
    check_bom_vs_positions,
    check_bottom_side,
    check_lcsc_coverage,
    check_positions_vs_bom,
    check_unique_lcsc,
)

# ── check_bom_vs_positions ────────────────────────────────────────────────────


def test_bom_vs_positions_all_present() -> None:
    bom = {"C1": "C123", "R1": "C456", "U1": "C789"}
    pos = {"C1": "top", "R1": "top", "U1": "top"}
    result = check_bom_vs_positions(bom, pos)
    assert result.passed
    assert "3" in result.detail


def test_bom_vs_positions_missing() -> None:
    bom = {"C1": "C123", "C2": "C456"}
    pos = {"C1": "top"}
    result = check_bom_vs_positions(bom, pos)
    assert not result.passed
    assert "C2" in result.detail


# ── check_positions_vs_bom ────────────────────────────────────────────────────


def test_positions_vs_bom_all_in_bom() -> None:
    bom = {"C1": "C123", "C2": "C456"}
    pos = {"C1": "top", "C2": "top"}
    result = check_positions_vs_bom(bom, pos)
    assert result.passed


def test_positions_vs_bom_orphan() -> None:
    bom = {"C1": "C123"}
    pos = {"C1": "top", "C2": "top"}  # C2 not in BOM
    result = check_positions_vs_bom(bom, pos)
    assert not result.passed
    assert "C2" in result.detail


# ── check_lcsc_coverage ───────────────────────────────────────────────────────


def test_lcsc_coverage_all_present() -> None:
    bom = {"C1": "C12345", "R1": "C67890"}
    result = check_lcsc_coverage(bom)
    assert result.passed
    assert "2" in result.detail


def test_lcsc_coverage_missing() -> None:
    bom = {"C1": "C12345", "R1": ""}
    result = check_lcsc_coverage(bom)
    assert not result.passed
    assert "R1" in result.detail


def test_lcsc_coverage_nan_treated_as_missing() -> None:
    bom = {"C1": "nan", "C2": "C12345"}
    result = check_lcsc_coverage(bom)
    assert not result.passed
    assert "C1" in result.detail


# ── check_bottom_side ─────────────────────────────────────────────────────────


def test_bottom_side_all_top() -> None:
    pos = {"C1": "top", "R1": "top", "U1": "top"}
    result = check_bottom_side(pos)
    assert result.passed
    assert "single" in result.detail.lower() or "top" in result.detail.lower()


def test_bottom_side_has_bottom_parts() -> None:
    pos = {"C1": "top", "C2": "bottom"}
    result = check_bottom_side(pos)
    assert result.passed  # informational — not a failure
    assert "C2" in result.detail
    assert "double" in result.detail.lower() or "bottom" in result.detail.lower()


# ── check_unique_lcsc ─────────────────────────────────────────────────────────


def test_unique_lcsc_no_cross_type_duplicates() -> None:
    # Same LCSC across same type prefix is fine (e.g. multiple caps same value)
    bom = {"C1": "C12345", "C2": "C12345", "R1": "C99999"}
    result = check_unique_lcsc(bom)
    assert result.passed


def test_unique_lcsc_cross_type_duplicate_flagged() -> None:
    # Same LCSC used for a capacitor and a resistor — likely a BOM error
    bom = {"C1": "C12345", "R1": "C12345"}
    result = check_unique_lcsc(bom)
    assert not result.passed
    assert "C12345" in result.detail
