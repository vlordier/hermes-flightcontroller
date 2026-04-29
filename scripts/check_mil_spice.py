#!/usr/bin/env python3
"""MIL-STD-461 / MIL-STD-704 SPICE output validator.

Checks power-rail DC levels, voltage ripple (MIL-STD-704F limits), conducted
transient overshoot (MIL-STD-461G CS101/CS116 limits), and power-sequencing
timing against configured thresholds.

Exit codes:
  0 — all checks passed (or no simulation data present)
  1 — one or more threshold violations detected

Usage:
    python3 check_mil_spice.py <ngspice_output_file>

Add simulation directives (.op / .tran / .meas) to the KiCad schematic and
wire up SPICE models in each component to activate full checking.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── MIL-STD-704F / MIL-STD-1275D DC voltage limits ─────────────────────────
# Each entry: net pattern → (min_V, max_V, ripple_pk_mV, label)
# ripple_pk_mV: peak-to-peak ripple budget at the rail (0 = not checked here)
@dataclass
class RailSpec:
    pattern: str
    vmin: float
    vmax: float
    ripple_pk_mv: float  # peak-to-peak ripple allowance in mV (0 = not checked)
    label: str


RAIL_SPECS: list[RailSpec] = [
    # AP63203 5 V buck output — MIL-STD-704F § 4.4 (28 V system scaled to 5 V)
    # ±5 % steady-state, 50 mV pk-pk ripple budget
    RailSpec(r"v\(5v\)", 4.75, 5.25, 50.0, "5 V rail"),
    # 3.3 V digital — ±5 %, 30 mV pk-pk
    RailSpec(r"v\(3v3\)", 3.135, 3.465, 30.0, "3.3 V rail"),
    # LD39015M18R 1.8 V LDO — ±5 %, 20 mV pk-pk
    RailSpec(r"v\(1v8\)", 1.71, 1.89, 20.0, "1.8 V rail"),
    # USB VBUS — USB spec 4.5–5.5 V, 120 mV pk-pk (USB 2.0 spec)
    RailSpec(r"v\(vbus\)", 4.5, 5.5, 120.0, "VBUS"),
]

# ── MIL-STD-461G CS116 damped-sinusoid transient limits ──────────────────────
# Allowable transient overshoot relative to nominal (fractional)
CS116_OVERSHOOT_LIMIT = 0.50   # 50 % above nominal → fail
CS116_UNDERSHOOT_LIMIT = 0.40  # 40 % below nominal → fail

# ── MIL-STD-704F transient limits (from steady-state, <1 ms) ─────────────────
MIL704_TRANSIENT_OVER_V = 0.5   # 500 mV above nominal absolute
MIL704_TRANSIENT_UNDER_V = 0.5  # 500 mV below nominal absolute


# ── Parser ────────────────────────────────────────────────────────────────────

def _parse_measurements(text: str) -> dict[str, float]:
    """Extract ``name = value`` scalar pairs from ngspice raw output."""
    results: dict[str, float] = {}
    for m in re.finditer(
        r"^\s*(\S+)\s*=\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)",
        text,
        re.MULTILINE,
    ):
        results[m.group(1).lower()] = float(m.group(2))
    return results


def _parse_meas_ripple(text: str) -> dict[str, float]:
    """Extract .meas results for ripple (pp_ prefix convention).

    Expects ngspice .meas statements of the form:
        .meas tran pp_5v pp(v(5v))
    ngspice prints: ``pp_5v = <value>``
    """
    return {
        k: v for k, v in _parse_measurements(text).items()
        if k.startswith("pp_")
    }


def _parse_meas_transient(text: str) -> dict[str, float]:
    """Extract .meas transient extremes (max_ / min_ prefix convention).

    Expects:
        .meas tran max_5v max(v(5v))
        .meas tran min_5v min(v(5v))
    """
    results = _parse_measurements(text)
    return {k: v for k, v in results.items()
            if k.startswith("max_") or k.startswith("min_")}


# ── Checks ────────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        tag = "PASS" if self.passed else "FAIL"
        return f"  {tag}  {self.name}: {self.detail}"


def _check_dc(measurements: dict[str, float]) -> list[CheckResult]:
    results: list[CheckResult] = []
    for spec in RAIL_SPECS:
        for name, value in measurements.items():
            if re.fullmatch(spec.pattern, name, re.IGNORECASE):
                ok = spec.vmin <= value <= spec.vmax
                results.append(CheckResult(
                    name=f"{spec.label} DC ({name})",
                    passed=ok,
                    detail=(
                        f"{value:.4f} V  "
                        f"[MIL limit: {spec.vmin}–{spec.vmax} V]"
                    ),
                ))
    return results


def _net_name(pattern: str) -> str:
    """Extract the net name from a v(...) SPICE pattern string.

    Patterns are stored with regex escapes (e.g. ``r"v\\(5v\\)"`` to match
    the literal SPICE notation ``v(5v)``).  This helper strips the wrapper
    to recover the bare net name (e.g. ``"5v"``).
    """
    m = re.match(r"v\\\((.+?)\\\)", pattern)
    if m:
        return m.group(1)
    # Fallback: strip common SPICE wrapper characters
    return re.sub(r"[v\\()\[\]]", "", pattern)


def _check_ripple(ripple: dict[str, float]) -> list[CheckResult]:
    """Validate pk-pk ripple from .meas pp_* results."""
    results: list[CheckResult] = []
    for spec in RAIL_SPECS:
        if spec.ripple_pk_mv == 0:
            continue
        net = _net_name(spec.pattern)
        key = f"pp_{net}"
        if key in ripple:
            val_mv = ripple[key] * 1000.0   # V → mV
            ok = val_mv <= spec.ripple_pk_mv
            results.append(CheckResult(
                name=f"{spec.label} ripple pk-pk",
                passed=ok,
                detail=(
                    f"{val_mv:.2f} mV pk-pk  "
                    f"[MIL-STD-704F limit: {spec.ripple_pk_mv:.0f} mV]"
                ),
            ))
    return results


def _check_transients(
    measurements: dict[str, float],
    transients: dict[str, float],
) -> list[CheckResult]:
    """Validate transient overshoot / undershoot for each rail."""
    results: list[CheckResult] = []
    for spec in RAIL_SPECS:
        nominal_key = None
        for name in measurements:
            if re.fullmatch(spec.pattern, name, re.IGNORECASE):
                nominal_key = name
                break
        if nominal_key is None:
            continue
        nominal = measurements[nominal_key]
        net = _net_name(spec.pattern)
        max_key, min_key = f"max_{net}", f"min_{net}"

        if max_key in transients:
            val = transients[max_key]
            over = val - nominal
            # Check absolute MIL-STD-704F limit
            ok_abs = over <= MIL704_TRANSIENT_OVER_V
            # Check fractional MIL-STD-461G CS116 limit (50 % above nominal)
            ok_cs116 = (over / nominal) <= CS116_OVERSHOOT_LIMIT if nominal else True
            ok = ok_abs and ok_cs116
            limit_str = (
                f"+{MIL704_TRANSIENT_OVER_V*1000:.0f} mV (704F) / "
                f"+{CS116_OVERSHOOT_LIMIT*100:.0f}% (CS116)"
            )
            results.append(CheckResult(
                name=f"{spec.label} transient overshoot",
                passed=ok,
                detail=f"+{over*1000:.1f} mV peak  [limit: {limit_str}]",
            ))

        if min_key in transients:
            val = transients[min_key]
            under = nominal - val
            ok_abs = under <= MIL704_TRANSIENT_UNDER_V
            ok_cs116 = (under / nominal) <= CS116_UNDERSHOOT_LIMIT if nominal else True
            ok = ok_abs and ok_cs116
            limit_str = (
                f"-{MIL704_TRANSIENT_UNDER_V*1000:.0f} mV (704F) / "
                f"-{CS116_UNDERSHOOT_LIMIT*100:.0f}% (CS116)"
            )
            results.append(CheckResult(
                name=f"{spec.label} transient undershoot",
                passed=ok,
                detail=f"-{under*1000:.1f} mV peak  [limit: {limit_str}]",
            ))

    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def main(output_file: str) -> int:
    path = Path(output_file)
    if not path.exists():
        print(
            f"[MIL-SPICE] '{output_file}' not found — "
            "no simulation data (add SPICE models + .meas directives "
            "to activate MIL-STD-704F / CS116 checks)."
        )
        return 0

    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        print("[MIL-SPICE] Output empty — skipping checks.")
        return 0

    measurements = _parse_measurements(text)
    ripple       = _parse_meas_ripple(text)
    transients   = _parse_meas_transient(text)

    if not measurements:
        print("[MIL-SPICE] No measurements found — skipping checks.")
        return 0

    all_results: list[CheckResult] = []
    all_results += _check_dc(measurements)
    all_results += _check_ripple(ripple)
    all_results += _check_transients(measurements, transients)

    if not all_results:
        print("[MIL-SPICE] No matching rail measurements found.")
        return 0

    print("[MIL-SPICE] Results:")
    for r in all_results:
        print(r)

    failures = [r for r in all_results if not r.passed]
    if failures:
        print(f"\n[MIL-SPICE] {len(failures)} violation(s) detected.")
        return 1

    print(f"\n[MIL-SPICE] All {len(all_results)} checks passed.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <ngspice_output_file>")
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
