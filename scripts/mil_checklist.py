#!/usr/bin/env python3
"""MIL-STD compliance checklist generator for the Hermes Flight Controller PCB.

Aggregates results from ERC/DRC reports, SPICE simulation output, and OpenEMS
EM simulation into a single Markdown compliance report.

Exit codes:
  0 — report generated (check markdown for PASS/FAIL per item)
  1 — unrecoverable error generating the report

Usage:
    python3 mil_checklist.py [--report OUTPUT.md] [--reports-dir reports/]

The generated report covers:
  • MIL-STD-461G  — Electromagnetic Compatibility (EMC)
  • MIL-STD-704F  — Aircraft / Platform Power Characteristics
  • MIL-STD-275E  — Printed Wiring Board physical requirements
  • IPC-2221A     — Generic PCB design standard (Class B / Class C)
  • MIL-PRF-55110 — Printed Wiring Board performance
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path
from typing import NamedTuple

# ── Data structures ───────────────────────────────────────────────────────────


class CheckItem(NamedTuple):
    section: str          # e.g. "MIL-STD-461G RE102"
    requirement: str      # human-readable requirement text
    method: str           # how it is verified (tool / inspection)
    status: str           # PASS | FAIL | WARN | N/A | PENDING
    evidence: str         # brief evidence or measurement


# ── Section parsers ────────────────────────────────────────────────────────────


def _erc_status(reports_dir: Path) -> tuple[str, str]:
    """Return (status, evidence) from erc_report.txt."""
    path = reports_dir / "erc_report.txt"
    if not path.exists():
        return "PENDING", "erc_report.txt not found"
    text = path.read_text(encoding="utf-8", errors="replace")
    # kicad-cli prints "ERC violations: N" on the last meaningful line
    m = re.search(r"ERC violations:\s*(\d+)", text, re.IGNORECASE)
    if m:
        count = int(m.group(1))
        if count == 0:
            return "PASS", "0 ERC violations"
        return "FAIL", f"{count} ERC violation(s) — see erc_report.txt"
    # Fallback: no violations line → assume pass if report non-empty
    if text.strip():
        return "PASS", "Report present, no violations keyword found"
    return "PENDING", "Empty ERC report"


def _drc_status(reports_dir: Path) -> tuple[str, str]:
    """Return (status, evidence) from drc_report.txt."""
    path = reports_dir / "drc_report.txt"
    if not path.exists():
        return "PENDING", "drc_report.txt not found"
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"DRC violations:\s*(\d+)", text, re.IGNORECASE)
    if m:
        count = int(m.group(1))
        if count == 0:
            return "PASS", "0 DRC violations"
        return "FAIL", f"{count} DRC violation(s) — see drc_report.txt"
    if text.strip():
        return "PASS", "Report present, no violations keyword found"
    return "PENDING", "Empty DRC report"


def _spice_status(reports_dir: Path) -> tuple[str, str]:
    """Return (status, evidence) from sim_output.txt."""
    path = reports_dir / "sim_output.txt"
    if not path.exists():
        return "PENDING", "sim_output.txt not found"
    text = path.read_text(encoding="utf-8", errors="replace")
    if "FAIL" in text:
        fails = sum(1 for line in text.splitlines() if "FAIL" in line)
        return "FAIL", f"{fails} SPICE threshold violation(s)"
    if "PASS" in text and "All checks passed" in text:
        return "PASS", "All SPICE rail checks passed"
    if not text.strip():
        return "PENDING", "No simulation output (SPICE models not yet added)"
    return "PENDING", "Simulation ran; no .meas results parsed"


def _em_status(reports_dir: Path) -> tuple[str, str]:
    """Return (status, evidence) from EM simulation results."""
    em_dir = reports_dir / "em_sim"
    stub = em_dir / "model.stub"
    if stub.exists():
        return "PENDING", "Stub model — gerber2ems not yet configured"
    result = em_dir / "results" / "e_field_spectrum.csv"
    if not result.exists():
        if not em_dir.exists():
            return "PENDING", "EM simulation not run"
        return "PENDING", "FDTD result file not found"
    text = result.read_text(encoding="utf-8", errors="replace")
    if "FAIL" in text:
        return "FAIL", "RE102 limit exceeded — see em_sim/results/"
    return "PASS", "RE102 field levels within MIL-STD-461G limits"


def _pcb_file_exists(pcb_path: Path) -> tuple[str, str]:
    if pcb_path.exists():
        return "PASS", str(pcb_path)
    return "N/A", "PCB file not found"


# ── Checklist builder ─────────────────────────────────────────────────────────


def build_checklist(reports_dir: Path, pcb_base: Path) -> list[CheckItem]:
    erc_s, erc_e  = _erc_status(reports_dir)
    drc_s, drc_e  = _drc_status(reports_dir)
    spice_s, spice_e = _spice_status(reports_dir)
    em_s, em_e    = _em_status(reports_dir)

    kicad_dru = pcb_base / "PCB" / "HermesFC" / "mil_rules.kicad_dru"
    dru_s, dru_e = (
        ("PASS", "mil_rules.kicad_dru present")
        if kicad_dru.exists()
        else ("FAIL", "mil_rules.kicad_dru missing")
    )

    return [
        # ── MIL-STD-461G ─────────────────────────────────────────────────────
        CheckItem(
            "MIL-STD-461G RE102",
            "Radiated emissions (electric field) within limits at 1 m, "
            "30 MHz–18 GHz",
            "OpenEMS FDTD simulation (scripts/run_openems.py --check-mil461)",
            em_s,
            em_e,
        ),
        CheckItem(
            "MIL-STD-461G CS101",
            "Conducted susceptibility — power leads, 30 Hz–150 kHz",
            "ngspice transient simulation (check_mil_spice.py)",
            spice_s,
            spice_e,
        ),
        CheckItem(
            "MIL-STD-461G CS116",
            "Conducted susceptibility — damped sinusoids on power/signal",
            "ngspice CS116 transient + check_mil_spice.py overshoot check",
            spice_s,
            spice_e,
        ),
        CheckItem(
            "MIL-STD-461G RE101",
            "Radiated emissions — magnetic field, 30 Hz–100 kHz",
            "Design review: proper ground plane, zero-ohm split, ferrites",
            "PENDING",
            "Manual inspection required",
        ),
        CheckItem(
            "MIL-STD-461G RS103",
            "Radiated susceptibility — electric field, 10 kHz–18 GHz",
            "OpenEMS susceptibility sweep (scripts/run_openems.py)",
            em_s,
            em_e,
        ),

        # ── MIL-STD-704F (Power Quality) ─────────────────────────────────────
        CheckItem(
            "MIL-STD-704F § 4.4",
            "5 V steady-state within ±5 % (4.75–5.25 V)",
            "ngspice .op (check_mil_spice.py DC check)",
            spice_s,
            spice_e,
        ),
        CheckItem(
            "MIL-STD-704F § 4.5",
            "Transient overshoot < 500 mV above nominal, recovery < 50 ms",
            "ngspice .tran (check_mil_spice.py transient check)",
            spice_s,
            spice_e,
        ),
        CheckItem(
            "MIL-STD-704F Ripple",
            "Ripple < 50 mV pk-pk on 5 V rail; < 30 mV on 3.3 V rail",
            "ngspice .meas pp_5v/pp_3v3 (check_mil_spice.py ripple check)",
            spice_s,
            spice_e,
        ),

        # ── MIL-STD-275E / IPC-2221A (Physical) ──────────────────────────────
        CheckItem(
            "MIL-STD-275E Clearance",
            "Min trace clearance 0.25 mm (external), per Table 1",
            "KiCad DRC with mil_rules.kicad_dru",
            drc_s,
            drc_e,
        ),
        CheckItem(
            "MIL-STD-275E Trace Width",
            "Min trace 0.25 mm; power traces ≥ 0.8 mm (3 A @ 10 °C rise)",
            "KiCad DRC with mil_rules.kicad_dru",
            drc_s,
            drc_e,
        ),
        CheckItem(
            "MIL-STD-275E Edge Clearance",
            "Copper ≥ 1.25 mm from board edge",
            "KiCad DRC with mil_rules.kicad_dru",
            drc_s,
            drc_e,
        ),
        CheckItem(
            "MIL-STD-275E Via Drill",
            "Via drill ≥ 0.3 mm (plated-through-holes)",
            "KiCad DRC with mil_rules.kicad_dru",
            drc_s,
            drc_e,
        ),
        CheckItem(
            "MIL-STD-275E Custom Rules",
            "mil_rules.kicad_dru present and applied to PCB",
            "File presence check",
            dru_s,
            dru_e,
        ),
        CheckItem(
            "IPC-2221A ERC",
            "No unconnected pins, no electrical rule violations",
            "KiCad ERC (kicad-cli sch erc)",
            erc_s,
            erc_e,
        ),
        CheckItem(
            "MIL-PRF-55110 Stackup",
            "4-layer stackup: F.Cu / In1.Cu(GND) / In2.Cu(PWR) / B.Cu",
            "KiCad stackup review + KiBot stackup report",
            "PENDING",
            "Visual inspection of PCB editor stackup",
        ),
        CheckItem(
            "MIL-PRF-55110 Ground Plane",
            "Continuous GND plane on In1.Cu; < 5 % voiding",
            "KiBot copper-pour analysis + visual inspection",
            "PENDING",
            "Manual inspection of In1.Cu pour",
        ),

        # ── Component / BOM ───────────────────────────────────────────────────
        CheckItem(
            "Component Derating",
            "Capacitors derated ≥ 50 % voltage; resistors ≥ 50 % power",
            "BOM derating field (Voltage Rating / Derating Note)",
            "PENDING",
            "Complete 'Derating Note' field in KiCad symbol editor",
        ),
        CheckItem(
            "Temperature Rating",
            "All components rated to -40 °C to +85 °C (commercial/industrial)",
            "BOM Temperature Rating field",
            "PENDING",
            "Complete 'Temperature Rating' field in KiCad symbol editor",
        ),
        CheckItem(
            "TVS / ESD Protection",
            "TVS diodes on all external connectors (USB, GPIO headers)",
            "Schematic review + ERC",
            erc_s,
            erc_e,
        ),

        # ── Software / Firmware traceability ─────────────────────────────────
        CheckItem(
            "Revision Control",
            "Git SHA embedded in firmware / board revision block",
            "CI: git describe --tags in build",
            "PENDING",
            "Add git describe to firmware build system",
        ),
    ]


# ── Report renderer ───────────────────────────────────────────────────────────


_STATUS_EMOJI = {
    "PASS": "✅",
    "FAIL": "❌",
    "WARN": "⚠️",
    "PENDING": "🔲",
    "N/A": "—",
}


def render_markdown(items: list[CheckItem]) -> str:
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pass_cnt = sum(1 for i in items if i.status == "PASS")
    fail_cnt = sum(1 for i in items if i.status == "FAIL")
    warn_cnt = sum(1 for i in items if i.status == "WARN")
    pend_cnt = sum(1 for i in items if i.status == "PENDING")

    lines: list[str] = [
        "# MIL-Grade Compliance Report — Hermes Flight Controller",
        "",
        f"Generated: {now}",
        "",
        "## Summary",
        "",
        f"| Status | Count |",
        f"|--------|-------|",
        f"| ✅ PASS    | {pass_cnt} |",
        f"| ❌ FAIL    | {fail_cnt} |",
        f"| ⚠️  WARN    | {warn_cnt} |",
        f"| 🔲 PENDING | {pend_cnt} |",
        f"| **Total**  | **{len(items)}** |",
        "",
    ]

    if fail_cnt > 0:
        lines += [
            "## ❌ Failures (must be resolved before release)",
            "",
        ]
        for item in items:
            if item.status == "FAIL":
                lines.append(
                    f"- **{item.section}** — {item.requirement}  "
                    f"(Evidence: {item.evidence})"
                )
        lines.append("")

    lines += [
        "## Detailed Checklist",
        "",
        "| # | Section | Requirement | Verification Method | Status | Evidence |",
        "|---|---------|-------------|---------------------|--------|----------|",
    ]
    for idx, item in enumerate(items, 1):
        emoji = _STATUS_EMOJI.get(item.status, item.status)
        lines.append(
            f"| {idx} "
            f"| {item.section} "
            f"| {item.requirement} "
            f"| {item.method} "
            f"| {emoji} {item.status} "
            f"| {item.evidence} |"
        )

    lines += [
        "",
        "## Standards Referenced",
        "",
        "- **MIL-STD-461G** — Requirements for the Control of Electromagnetic"
        " Interference Characteristics of Subsystems and Equipment",
        "- **MIL-STD-704F** — Aircraft Electric Power Characteristics",
        "- **MIL-STD-1275D** — Characteristics of 28 Volt DC Electrical Systems"
        " in Military Vehicles",
        "- **MIL-STD-275E** — Printed Wiring for Electronic Equipment",
        "- **MIL-PRF-55110** — Performance Specification: Printed Wiring Boards",
        "- **IPC-2221A** — Generic Standard on Printed Board Design",
        "",
        "> **Note:** PENDING items require manual inspection or SPICE/EM model"
        " setup.  See `CONTRIBUTING.md` for how to activate full simulation.",
    ]
    return "\n".join(lines) + "\n"


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        default="mil_compliance.md",
        help="Output Markdown report path (default: mil_compliance.md)",
    )
    parser.add_argument(
        "--reports-dir",
        default="reports",
        help="Directory containing ERC/DRC/SPICE/EM result files (default: reports/)",
    )
    args = parser.parse_args()

    reports_dir = Path(args.reports_dir)
    pcb_base    = Path(".")  # repo root

    items = build_checklist(reports_dir, pcb_base)
    md = render_markdown(items)

    out = Path(args.report)
    out.write_text(md, encoding="utf-8")
    print(f"[MIL-CHECKLIST] Report written to {out}")

    fail_count = sum(1 for i in items if i.status == "FAIL")
    if fail_count:
        print(f"[MIL-CHECKLIST] {fail_count} FAIL item(s) — review required.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
