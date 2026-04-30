"""Tests for MIL checklist helpers that parse SPICE margin outputs."""

from __future__ import annotations

from pathlib import Path

from scripts.mil_checklist import _parse_spice_margin_items, _spice_status


def test_parse_spice_margin_items_extracts_case_lines(tmp_path: Path) -> None:
    report = tmp_path / "spice_check_report.txt"
    report.write_text(
        "\n".join(
            [
                "[MIL-SPICE:sim_output_low] Results:",
                "  PASS  5 V rail DC (v(5v)): 5.01 V [MIL limit: 4.75-5.25 V, headroom 120 mV, target >= 50 mV]",
                "  FAIL  3.3 V rail startup rise: 15.0 ms [target <= 10.0 ms]",
                "[MIL-SPICE:sim_output_high] Results:",
                "  PASS  5V load-step droop: 120.0 mV [target <= 250 mV]",
            ]
        ),
        encoding="utf-8",
    )

    items = _parse_spice_margin_items(tmp_path)
    assert len(items) == 3
    assert items[0].section == "SPICE Margin / sim_output_low"
    assert items[1].status == "FAIL"
    assert "startup rise" in items[1].requirement
    assert items[2].section == "SPICE Margin / sim_output_high"


def test_spice_status_uses_report_summary(tmp_path: Path) -> None:
    (tmp_path / "sim_output_nominal.txt").write_text("dummy", encoding="utf-8")
    (tmp_path / "spice_check_report.txt").write_text(
        "\n".join(
            [
                "[MIL-SPICE:sim_output_nominal] Results:",
                "  PASS  5 V rail DC (v(5v)): 5.00 V [MIL limit: 4.75-5.25 V]",
                "[MIL-SPICE:sim_output_nominal] All 1 checks passed.",
                "[MIL-SPICE] Checked 1 file(s), 1 total checks.",
            ]
        ),
        encoding="utf-8",
    )

    status, evidence = _spice_status(tmp_path)
    assert status == "PASS"
    assert "1 file(s), 1 checks" in evidence


def test_spice_status_reports_failures_from_checker_report(tmp_path: Path) -> None:
    (tmp_path / "sim_output_a.txt").write_text("dummy", encoding="utf-8")
    (tmp_path / "spice_check_report.txt").write_text(
        "\n".join(
            [
                "[MIL-SPICE:sim_output_a] Results:",
                "  FAIL  3.3 V rail ripple pk-pk: 35.0 mV [target <= 24.0 mV]",
                "[MIL-SPICE:sim_output_a] 1 violation(s) detected.",
                "[MIL-SPICE] Checked 1 file(s), 3 total checks.",
            ]
        ),
        encoding="utf-8",
    )

    status, evidence = _spice_status(tmp_path)
    assert status == "FAIL"
    assert "1 SPICE check failure" in evidence
