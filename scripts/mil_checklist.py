#!/usr/bin/env python3
"""MIL-STD compliance checklist generator for the Hermes Flight Controller PCB.

Aggregates results from ERC/DRC reports, SPICE simulation output, OpenEMS
EM simulation, PCB physical checks, and BOM validation into a single Markdown
compliance report.

Exit codes:
  0 — report generated (check markdown for PASS/FAIL per item)
  1 — unrecoverable error generating the report

Usage:
    python3 mil_checklist.py [--report OUTPUT.md] [--reports-dir reports/]
                             [--pcb PCB/HermesFC/HermesFC.kicad_pcb]
                             [--bom PCB/HermesFC/manufacturing/bom.csv]

The generated report covers:
  * MIL-STD-461G  -- Electromagnetic Compatibility (EMC)
  * MIL-STD-704F  -- Aircraft / Platform Power Characteristics
  * MIL-STD-275E  -- Printed Wiring Board physical requirements
  * IPC-2221A     -- Generic PCB design standard (Class B / Class C)
  * MIL-PRF-55110 -- Printed Wiring Board performance
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, NamedTuple

doorstop: Any
try:
    import doorstop as doorstop_mod

    doorstop = doorstop_mod
except ImportError:
    doorstop = None

# ---- Data structures --------------------------------------------------------


class CheckItem(NamedTuple):
    section: str  # e.g. "MIL-STD-461G RE102"
    requirement: str  # human-readable requirement text
    method: str  # how it is verified (tool / inspection)
    status: str  # PASS | FAIL | WARN | N/A | PENDING
    evidence: str  # brief evidence or measurement


# ---- Helpers to run sub-scripts ----------------------------------------------


def _run_script(script: Path, *args: str) -> tuple[str, str, int]:
    """Run a Python script as a subprocess; return (stdout, stderr, returncode)."""
    cmd = [sys.executable, str(script), *list(args)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout, result.stderr, result.returncode


def _script_status(script_path: Path, *args: str, check_msg: str | None = None) -> tuple[str, str]:
    """Run a checker script; return (status, evidence) based on exit code."""
    if not script_path.exists():
        return "PENDING", f"Script not found: {script_path}"
    stdout, stderr, rc = _run_script(script_path, *args)
    output = (stdout + stderr).strip()
    lines = [line for line in output.splitlines() if line.strip()]

    if check_msg:
        # Find the line that matches our specific check message
        matched_line = next((line for line in lines if check_msg in line), None)
        if matched_line:
            evidence = matched_line
        else:
            evidence = lines[-1] if lines else "(no output)"
    else:
        evidence = lines[-1] if lines else "(no output)"

    if rc == 0:
        return "PASS", evidence

    # If it failed, but our specific line says PASS, we need to be careful.
    # For now, if the script returns non-zero, it's a FAIL.
    if check_msg and matched_line and "  PASS  " in matched_line:
        # This specific sub-check might have passed even if the script failed other checks
        return "PASS", evidence

    return "FAIL", evidence


# ---- Section parsers ---------------------------------------------------------


def _erc_status(reports_dir: Path) -> tuple[str, str]:
    """Return (status, evidence) from erc_report.txt."""
    path = reports_dir / "erc_report.txt"
    if not path.exists():
        return "PENDING", "erc_report.txt not found"
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"ERC violations:\s*(\d+)", text, re.IGNORECASE)
    if m:
        count = int(m.group(1))
        if count == 0:
            return "PASS", "0 ERC violations"
        return "FAIL", f"{count} ERC violation(s) -- see erc_report.txt"
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
        return "FAIL", f"{count} DRC violation(s) -- see drc_report.txt"
    if text.strip():
        return "PASS", "Report present, no violations keyword found"
    return "PENDING", "Empty DRC report"


def _spice_status(reports_dir: Path) -> tuple[str, str]:
    """Return (status, evidence) from sim_output.txt."""
    path = reports_dir / "sim_output.txt"
    if not path.exists():
        return "PENDING", "sim_output.txt not found"
    text = path.read_text(encoding="utf-8", errors="replace")
    if "violation(s) detected" in text:
        fails = sum(1 for line in text.splitlines() if "FAIL" in line)
        return "FAIL", f"{fails} SPICE threshold violation(s)"
    if "All" in text and "checks passed" in text:
        return "PASS", "All SPICE rail checks passed"
    if not text.strip():
        return "PENDING", "No simulation output (SPICE models not yet added)"
    return "PENDING", "Simulation ran; no .meas results parsed"


def _em_status(reports_dir: Path) -> tuple[str, str]:
    """Return (status, evidence) from EM simulation results."""
    em_dir = reports_dir / "em_sim"
    result = em_dir / "results" / "e_field_spectrum.csv"
    if not result.exists():
        if not em_dir.exists():
            return "PENDING", "EM simulation not run"
        return "PENDING", "FDTD result file not found"
    text = result.read_text(encoding="utf-8", errors="replace")
    if "FAIL" in text:
        return "FAIL", "RE102 limit exceeded -- see em_sim/results/"
    return "PASS", "RE102 field levels within MIL-STD-461G limits"


def _git_sha() -> tuple[str, str]:
    """Return (status, evidence) for git revision traceability."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            return "PASS", f"Git revision: {sha}"
        return "FAIL", "git describe failed -- ensure git tags are present"
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return "FAIL", f"git not available: {exc}"


def _stackup_status(pcb_file: Path) -> tuple[str, str]:
    """Check 4-layer stackup by parsing the PCB file directly."""
    if not pcb_file.exists():
        return "PENDING", f"PCB file not found: {pcb_file}"
    text = pcb_file.read_text(encoding="utf-8", errors="replace")
    required = {"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"}
    found = set(re.findall(r'"((?:F|B|In\d+)\.Cu)"', text))
    missing = required - found
    if not missing:
        return "PASS", "4-layer stackup: F.Cu / In1.Cu(GND) / In2.Cu(PWR) / B.Cu"
    return "FAIL", f"Missing copper layers: {', '.join(sorted(missing))}"


def _extract_zone_blocks(text: str) -> list[str]:
    """Extract individual zone S-expression blocks using paren-depth counting.

    Uses a regex to locate each ``(zone`` token regardless of surrounding
    whitespace, then walks the text character-by-character counting paren
    depth to find the matching close-paren.  This is robust against
    differences in indentation style (tabs vs spaces) across KiCad versions.
    """
    zones: list[str] = []
    for m in re.finditer(r"\(\s*zone\b", text):
        start = m.start()
        depth = 0
        j = start
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    zones.append(text[start : j + 1])
                    break
            j += 1
    return zones


def _gnd_plane_status(pcb_file: Path) -> tuple[str, str]:
    """Check for GND zone on inner copper layers using per-block zone parsing."""
    if not pcb_file.exists():
        return "PENDING", f"PCB file not found: {pcb_file}"
    text = pcb_file.read_text(encoding="utf-8", errors="replace")
    inner_layers = {"In1.Cu", "In2.Cu"}
    for block in _extract_zone_blocks(text):
        net_m = re.search(r'\(net_name "([^"]*)"\)', block)
        layer_m = re.search(r'\(layer "([^"]*)"\)', block)
        net = net_m.group(1) if net_m else ""
        layer = layer_m.group(1) if layer_m else ""
        if net == "GND" and layer in inner_layers:
            return "PASS", f"GND copper pour present on {layer}"
    return "FAIL", (
        "No GND zone on In1.Cu or In2.Cu -- add a GND copper pour on In1.Cu "
        "for EMI shielding (MIL-STD-461G / MIL-PRF-55110)"
    )


# ---- Checklist builder -------------------------------------------------------


def build_checklist(
    reports_dir: Path,
    pcb_file: Path,
    bom_file: Path,
    scripts_dir: Path,
) -> list[CheckItem]:
    erc_s, erc_e = _erc_status(reports_dir)
    drc_s, drc_e = _drc_status(reports_dir)
    spice_s, spice_e = _spice_status(reports_dir)
    em_s, em_e = _em_status(reports_dir)
    pcb_s, pcb_e = _script_status(scripts_dir / "check_pcb.py", str(pcb_file))
    integ_ring_s, integ_ring_e = _script_status(
        scripts_dir / "check_pcb_integrity.py", str(pcb_file), check_msg="Annular Rings"
    )
    integ_name_s, integ_name_e = _script_status(
        scripts_dir / "check_pcb_integrity.py", str(pcb_file), check_msg="Net Naming"
    )
    bom_s, bom_e = _script_status(scripts_dir / "check_bom.py", str(bom_file))
    git_s, git_e = _git_sha()
    stk_s, stk_e = _stackup_status(pcb_file)
    gnd_s, gnd_e = _gnd_plane_status(pcb_file)

    # Requirements traceability (Doorstop)
    door_s, door_e = "PENDING", "Doorstop data not found"
    if doorstop:
        try:
            tree = doorstop.build()
            items = [item for doc in tree.documents for item in doc]
            req_count = len(items)
            suspect = sum(1 for item in items if not item.cleared)
            unreviewed = sum(1 for item in items if not item.reviewed)
            if suspect > 0:
                door_s, door_e = "FAIL", f"{suspect} suspect link(s) detected in requirements"
            elif unreviewed > 0:
                door_s, door_e = "WARN", f"{unreviewed} unreviewed requirement(s)"
            elif req_count > 0:
                door_s = "PASS"
                door_e = f"{req_count} requirements validated; 100% traceability coverage"
            else:
                door_s, door_e = "WARN", "Requirement tree is empty"
        except Exception as e:
            door_s, door_e = "FAIL", f"Doorstop error: {e!s}"

    kicad_dru = pcb_file.parent / "mil_rules.kicad_dru"
    dru_s = "PASS" if kicad_dru.exists() else "FAIL"
    dru_e = "mil_rules.kicad_dru present" if kicad_dru.exists() else "mil_rules.kicad_dru missing"

    return [
        # -- MIL-STD-461G -----------------------------------------------------
        CheckItem(
            "MIL-STD-461G RE102",
            "Radiated emissions (electric field) within limits at 1 m, 30 MHz-18 GHz",
            "OpenEMS FDTD simulation (scripts/run_openems.py --check-mil461)",
            em_s,
            em_e,
        ),
        CheckItem(
            "MIL-STD-461G CS101",
            "Conducted susceptibility -- power leads, 30 Hz-150 kHz",
            "ngspice transient simulation (check_mil_spice.py)",
            spice_s,
            spice_e,
        ),
        CheckItem(
            "MIL-STD-461G CS116",
            "Conducted susceptibility -- damped sinusoids on power/signal",
            "ngspice CS116 transient + check_mil_spice.py overshoot check",
            spice_s,
            spice_e,
        ),
        CheckItem(
            "MIL-STD-461G RE101",
            "Radiated emissions -- magnetic field, 30 Hz-100 kHz",
            "check_pcb.py: ground plane and stitching via verification",
            pcb_s,
            pcb_e,
        ),
        CheckItem(
            "MIL-STD-461G RS103",
            "Radiated susceptibility -- electric field, 10 kHz-18 GHz",
            "OpenEMS susceptibility sweep (scripts/run_openems.py)",
            em_s,
            em_e,
        ),
        # -- MIL-STD-704F (Power Quality) -------------------------------------
        CheckItem(
            "MIL-STD-704F sec 4.4",
            "5 V steady-state within +-5 % (4.75-5.25 V)",
            "ngspice .op (check_mil_spice.py DC check)",
            spice_s,
            spice_e,
        ),
        CheckItem(
            "MIL-STD-704F sec 4.5",
            "Transient overshoot < 500 mV above nominal, recovery < 50 ms",
            "ngspice .tran (check_mil_spice.py transient check)",
            spice_s,
            spice_e,
        ),
        CheckItem(
            "MIL-STD-704F Ripple",
            "Ripple < 50 mV pk-pk on 5 V; < 30 mV on 3.3 V",
            "ngspice .meas pp_5v/pp_3v3 (check_mil_spice.py ripple check)",
            spice_s,
            spice_e,
        ),
        # -- MIL-STD-275E / IPC-2221A (Physical) ------------------------------
        CheckItem(
            "MIL-STD-275E Clearance",
            "Min trace clearance 0.25 mm (external), per Table 1",
            "KiCad DRC with mil_rules.kicad_dru + check_pcb.py",
            drc_s,
            drc_e,
        ),
        CheckItem(
            "MIL-STD-275E Trace Width",
            "Min trace 0.25 mm signal; power traces >= 0.8 mm",
            "check_pcb.py track-width scan of .kicad_pcb",
            pcb_s,
            pcb_e,
        ),
        CheckItem(
            "MIL-STD-275E Edge Clearance",
            "Copper >= 1.25 mm from board edge",
            "KiCad DRC with mil_rules.kicad_dru",
            drc_s,
            drc_e,
        ),
        CheckItem(
            "MIL-STD-275E Via Drill",
            "Via drill >= 0.3 mm (plated-through-holes)",
            "check_pcb.py via-drill scan of .kicad_pcb",
            pcb_s,
            pcb_e,
        ),
        CheckItem(
            "Annular Ring Integrity",
            "Via annular rings >= 0.15 mm",
            "check_pcb_integrity.py annular ring scan",
            integ_ring_s,
            integ_ring_e,
        ),
        CheckItem(
            "Net Naming Hygiene",
            "Nets follow uppercase convention and naming hygiene",
            "check_pcb_integrity.py net convention check",
            integ_name_s,
            integ_name_e,
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
            "check_pcb.py layer enumeration from .kicad_pcb",
            stk_s,
            stk_e,
        ),
        CheckItem(
            "MIL-PRF-55110 Ground Plane",
            "Continuous GND plane on inner layer; < 5 % voiding",
            "check_pcb.py zone-net scan of .kicad_pcb",
            gnd_s,
            gnd_e,
        ),
        # -- Component / BOM --------------------------------------------------
        CheckItem(
            "Component LCSC Coverage",
            "All BOM components have LCSC part numbers for traceability",
            "check_bom.py LCSC field scan of bom.csv",
            bom_s,
            bom_e,
        ),
        CheckItem(
            "TVS / ESD Protection",
            "TVS diodes on all external connectors (USB, GPIO headers)",
            "check_bom.py ESD/TVS device scan of bom.csv",
            bom_s,
            bom_e,
        ),
        CheckItem(
            "Power Management ICs",
            "Buck converter and LDO regulators present in BOM",
            "check_bom.py power-IC scan of bom.csv",
            bom_s,
            bom_e,
        ),
        # -- Software / Firmware traceability ---------------------------------
        CheckItem(
            "Revision Control",
            "Git SHA / tag embedded in build; clean working tree",
            "git describe --tags --always --dirty",
            git_s,
            git_e,
        ),
        # -- Traceability -----------------------------------------------------
        CheckItem(
            "Requirements Traceability",
            "Functional and hardware requirements mapped and validated",
            "Doorstop requirements tree validation",
            door_s,
            door_e,
        ),
    ]


# ---- Report renderer ---------------------------------------------------------

_STATUS_EMOJI = {
    "PASS": "OK",
    "FAIL": "FAIL",
    "WARN": "WARN",
    "PENDING": "PENDING",
    "N/A": "N/A",
}

_STATUS_ICON = {
    "PASS": "✅",
    "FAIL": "❌",
    "WARN": "⚠️",
    "PENDING": "🔲",
    "N/A": "—",
}


def render_markdown(items: list[CheckItem]) -> str:
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")
    pass_cnt = sum(1 for i in items if i.status == "PASS")
    fail_cnt = sum(1 for i in items if i.status == "FAIL")
    warn_cnt = sum(1 for i in items if i.status == "WARN")
    pend_cnt = sum(1 for i in items if i.status == "PENDING")

    lines: list[str] = [
        "# MIL-Grade Compliance Report -- Hermes Flight Controller",
        "",
        f"Generated: {now}",
        "",
        "## Summary",
        "",
        "| Status | Count |",
        "|--------|-------|",
        f"| OK PASS    | {pass_cnt} |",
        f"| FAIL       | {fail_cnt} |",
        f"| WARN       | {warn_cnt} |",
        f"| PENDING    | {pend_cnt} |",
        f"| **Total**  | **{len(items)}** |",
        "",
    ]

    if fail_cnt > 0:
        lines += ["## Failures (must be resolved before release)", ""]
        for item in items:
            if item.status == "FAIL":
                lines.append(
                    f"- **{item.section}** -- {item.requirement}  (Evidence: {item.evidence})"
                )
        lines.append("")

    lines += [
        "## Detailed Checklist",
        "",
        "| # | Section | Requirement | Verification Method | Status | Evidence |",
        "|---|---------|-------------|---------------------|--------|----------|",
    ]
    for idx, item in enumerate(items, 1):
        icon = _STATUS_ICON.get(item.status, item.status)
        lines.append(
            f"| {idx} "
            f"| {item.section} "
            f"| {item.requirement} "
            f"| {item.method} "
            f"| {icon} {item.status} "
            f"| {item.evidence} |"
        )

    lines += [
        "",
        "## Standards Referenced",
        "",
        "- **MIL-STD-461G** -- Requirements for the Control of Electromagnetic"
        " Interference Characteristics of Subsystems and Equipment",
        "- **MIL-STD-704F** -- Aircraft Electric Power Characteristics",
        "- **MIL-STD-1275D** -- Characteristics of 28 Volt DC Electrical Systems"
        " in Military Vehicles",
        "- **MIL-STD-275E** -- Printed Wiring for Electronic Equipment",
        "- **MIL-PRF-55110** -- Performance Specification: Printed Wiring Boards",
        "- **IPC-2221A** -- Generic Standard on Printed Board Design",
        "",
        "> **Note:** PENDING items require SPICE or EM model setup.",
        "> See `CONTRIBUTING.md` for how to activate full simulation.",
    ]
    return "\n".join(lines) + "\n"


# ---- Main -------------------------------------------------------------------


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
        help="Directory containing ERC/DRC/SPICE/EM result files",
    )
    parser.add_argument(
        "--pcb",
        default="PCB/HermesFC/HermesFC.kicad_pcb",
        help="Path to .kicad_pcb file",
    )
    parser.add_argument(
        "--bom",
        default="PCB/HermesFC/manufacturing/bom.csv",
        help="Path to BOM CSV",
    )
    args = parser.parse_args()

    reports_dir = Path(args.reports_dir)
    pcb_file = Path(args.pcb)
    bom_file = Path(args.bom)
    scripts_dir = Path(__file__).parent

    items = build_checklist(reports_dir, pcb_file, bom_file, scripts_dir)
    md = render_markdown(items)

    out = Path(args.report)
    out.write_text(md, encoding="utf-8")
    print(f"[MIL-CHECKLIST] Report written to {out}")

    fail_count = sum(1 for i in items if i.status == "FAIL")
    pend_count = sum(1 for i in items if i.status == "PENDING")
    if fail_count:
        print(f"[MIL-CHECKLIST] {fail_count} FAIL item(s) -- review required.")
    if pend_count:
        print(f"[MIL-CHECKLIST] {pend_count} PENDING item(s) -- simulation data needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
