#!/usr/bin/env python3
"""MIL-STD-461 / MIL-STD-704 SPICE output validator.

Checks power-rail DC levels, voltage ripple (MIL-STD-704F limits), conducted
transient overshoot (MIL-STD-461G CS101/CS116 limits), load-step dynamic
response, and startup/brownout recovery against configured thresholds.

Exit codes:
  0 — all checks passed (or no simulation data present)
  1 — one or more threshold violations detected

Usage:
    python3 check_mil_spice.py <ngspice_output_file> [<ngspice_output_file> ...]

Add simulation directives (.op / .tran / .meas) to the KiCad schematic and
wire up SPICE models in each component to activate full checking.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
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
CS116_OVERSHOOT_LIMIT = 0.50  # 50 % above nominal → fail
CS116_UNDERSHOOT_LIMIT = 0.40  # 40 % below nominal → fail

# ── MIL-STD-704F transient limits (from steady-state, <1 ms) ─────────────────
MIL704_TRANSIENT_OVER_V = 0.5  # 500 mV above nominal absolute
MIL704_TRANSIENT_UNDER_V = 0.5  # 500 mV below nominal absolute

# ── Robustness margin targets (pre-compliance headroom) ─────────────────────
# These are intentionally stricter than hard MIL pass/fail limits.
DC_MARGIN_FRACTION = 0.10  # require 10% distance from min/max rails
RIPPLE_MARGIN_FRACTION = 0.20  # require at least 20% ripple headroom
TRANSIENT_MARGIN_FRACTION = 0.20  # require at least 20% transient headroom

# ── Load-step robustness targets (design margin, not MIL hard limits) ───────
LOAD_STEP_DROOP_LIMIT_MV: dict[str, float] = {
    "5v": 250.0,
    "3v3": 160.0,
    "1v8": 120.0,
}
LOAD_STEP_RECOVERY_TARGET_US: dict[str, float] = {
    "5v": 400.0,
    "3v3": 300.0,
    "1v8": 250.0,
}

# ── Startup / brownout robustness targets ───────────────────────────────────
STARTUP_RISE_TARGET_MS: dict[str, float] = {
    "5v": 12.0,
    "3v3": 10.0,
    "1v8": 8.0,
}
BROWNOUT_DIP_LIMIT_MV: dict[str, float] = {
    "5v": 350.0,
    "3v3": 220.0,
    "1v8": 160.0,
}
BROWNOUT_RECOVERY_TARGET_US: dict[str, float] = {
    "5v": 1000.0,
    "3v3": 800.0,
    "1v8": 600.0,
}

# ── Frequency-domain PI robustness targets ──────────────────────────────────
# Impedance targets are in milliohms and represent practical rail goals.
PI_ZOUT_TARGET_MOHM: dict[str, float] = {
    "5v": 120.0,
    "3v3": 80.0,
    "1v8": 60.0,
}
# PSRR proxy targets in dB across frequency bands.
PI_PSRR_TARGET_DB: dict[str, float] = {
    "5v": 20.0,
    "3v3": 25.0,
    "1v8": 30.0,
}


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
    return {k: v for k, v in _parse_measurements(text).items() if k.startswith("pp_")}


def _parse_meas_transient(text: str) -> dict[str, float]:
    """Extract .meas transient extremes (max_ / min_ prefix convention).

    Expects:
        .meas tran max_5v max(v(5v))
        .meas tran min_5v min(v(5v))
    """
    results = _parse_measurements(text)
    return {k: v for k, v in results.items() if k.startswith("max_") or k.startswith("min_")}


def _parse_meas_load_step(text: str) -> dict[str, float]:
    """Extract load-step metrics from .meas outputs.

    Convention:
        - ls_dip_<rail>      : droop in volts during load step
        - ls_recover_<rail>  : recovery time in seconds
    """
    results = _parse_measurements(text)
    return {
        k: v for k, v in results.items() if k.startswith("ls_dip_") or k.startswith("ls_recover_")
    }


def _parse_meas_startup_brownout(text: str) -> dict[str, float]:
    """Extract startup and brownout metrics from .meas outputs.

    Convention:
        - su_rise_<rail>       : startup rise time in seconds
        - bo_dip_<rail>        : brownout droop in volts
        - bo_recover_<rail>    : brownout recovery time in seconds
    """
    results = _parse_measurements(text)
    return {
        k: v
        for k, v in results.items()
        if k.startswith("su_rise_") or k.startswith("bo_dip_") or k.startswith("bo_recover_")
    }


def _parse_meas_pi(text: str) -> dict[str, float]:
    """Extract PI frequency-domain metrics from .meas outputs.

    Convention:
        - pi_zout_<rail>_<band> : output impedance in ohms
        - pi_psrr_<rail>_<band> : PSRR proxy in dB
    """
    results = _parse_measurements(text)
    return {
        k: v for k, v in results.items() if k.startswith("pi_zout_") or k.startswith("pi_psrr_")
    }


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
                in_range = spec.vmin <= value <= spec.vmax
                range_width = spec.vmax - spec.vmin
                min_margin = range_width * DC_MARGIN_FRACTION
                distance_to_limit = min(value - spec.vmin, spec.vmax - value)
                robust = distance_to_limit >= min_margin
                ok = in_range and robust
                results.append(
                    CheckResult(
                        name=f"{spec.label} DC ({name})",
                        passed=ok,
                        detail=(
                            f"{value:.4f} V  [MIL limit: {spec.vmin}–{spec.vmax} V, "
                            f"headroom {distance_to_limit * 1000:.1f} mV, "
                            f"target >= {min_margin * 1000:.1f} mV]"
                        ),
                    )
                )
    return results


def _net_name(pattern: str) -> str:
    r"""Extract the net name from a v(...) SPICE pattern string.

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
            val_mv = ripple[key] * 1000.0  # V → mV
            margin_limit_mv = spec.ripple_pk_mv * (1.0 - RIPPLE_MARGIN_FRACTION)
            ok = val_mv <= margin_limit_mv
            results.append(
                CheckResult(
                    name=f"{spec.label} ripple pk-pk",
                    passed=ok,
                    detail=(
                        f"{val_mv:.2f} mV pk-pk  [MIL limit: {spec.ripple_pk_mv:.0f} mV, "
                        f"robustness target <= {margin_limit_mv:.1f} mV]"
                    ),
                )
            )
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
            abs_margin_target = MIL704_TRANSIENT_OVER_V * (1.0 - TRANSIENT_MARGIN_FRACTION)
            frac_margin_target = CS116_OVERSHOOT_LIMIT * (1.0 - TRANSIENT_MARGIN_FRACTION)
            ok_abs = over <= abs_margin_target
            # Check fractional MIL-STD-461G CS116 limit (50 % above nominal)
            ok_cs116 = (over / nominal) <= frac_margin_target if nominal else True
            ok = ok_abs and ok_cs116
            limit_str = (
                f"+{abs_margin_target * 1000:.0f} mV (704F robust target) / "
                f"+{frac_margin_target * 100:.0f}% (CS116 robust target)"
            )
            results.append(
                CheckResult(
                    name=f"{spec.label} transient overshoot",
                    passed=ok,
                    detail=f"+{over * 1000:.1f} mV peak  [limit: {limit_str}]",
                )
            )

        if min_key in transients:
            val = transients[min_key]
            under = nominal - val
            abs_margin_target = MIL704_TRANSIENT_UNDER_V * (1.0 - TRANSIENT_MARGIN_FRACTION)
            frac_margin_target = CS116_UNDERSHOOT_LIMIT * (1.0 - TRANSIENT_MARGIN_FRACTION)
            ok_abs = under <= abs_margin_target
            ok_cs116 = (under / nominal) <= frac_margin_target if nominal else True
            ok = ok_abs and ok_cs116
            limit_str = (
                f"-{abs_margin_target * 1000:.0f} mV (704F robust target) / "
                f"-{frac_margin_target * 100:.0f}% (CS116 robust target)"
            )
            results.append(
                CheckResult(
                    name=f"{spec.label} transient undershoot",
                    passed=ok,
                    detail=f"-{under * 1000:.1f} mV peak  [limit: {limit_str}]",
                )
            )

    return results


def _check_load_step(load_step: dict[str, float]) -> list[CheckResult]:
    """Validate load-step droop and recovery time metrics."""
    results: list[CheckResult] = []
    for rail, droop_limit_mv in LOAD_STEP_DROOP_LIMIT_MV.items():
        dip_key = f"ls_dip_{rail}"
        recover_key = f"ls_recover_{rail}"
        rail_label = rail.upper()

        if dip_key in load_step:
            droop_mv = load_step[dip_key] * 1000.0
            ok = droop_mv <= droop_limit_mv
            results.append(
                CheckResult(
                    name=f"{rail_label} load-step droop",
                    passed=ok,
                    detail=(f"{droop_mv:.1f} mV  " f"[target <= {droop_limit_mv:.0f} mV]"),
                )
            )

        if recover_key in load_step:
            recover_us = load_step[recover_key] * 1_000_000.0
            target_us = LOAD_STEP_RECOVERY_TARGET_US[rail]
            ok = recover_us <= target_us
            results.append(
                CheckResult(
                    name=f"{rail_label} load-step recovery",
                    passed=ok,
                    detail=(f"{recover_us:.1f} us  " f"[target <= {target_us:.0f} us]"),
                )
            )

    return results


def _check_startup_brownout(startup_brownout: dict[str, float]) -> list[CheckResult]:
    """Validate startup rise time and brownout dip/recovery metrics."""
    results: list[CheckResult] = []
    for rail, rise_target_ms in STARTUP_RISE_TARGET_MS.items():
        rise_key = f"su_rise_{rail}"
        dip_key = f"bo_dip_{rail}"
        recover_key = f"bo_recover_{rail}"
        rail_label = rail.upper()

        if rise_key in startup_brownout:
            rise_ms = startup_brownout[rise_key] * 1000.0
            ok = rise_ms <= rise_target_ms
            results.append(
                CheckResult(
                    name=f"{rail_label} startup rise",
                    passed=ok,
                    detail=f"{rise_ms:.2f} ms  [target <= {rise_target_ms:.1f} ms]",
                )
            )

        if dip_key in startup_brownout:
            dip_mv = startup_brownout[dip_key] * 1000.0
            dip_limit_mv = BROWNOUT_DIP_LIMIT_MV[rail]
            ok = dip_mv <= dip_limit_mv
            results.append(
                CheckResult(
                    name=f"{rail_label} brownout dip",
                    passed=ok,
                    detail=f"{dip_mv:.1f} mV  [target <= {dip_limit_mv:.0f} mV]",
                )
            )

        if recover_key in startup_brownout:
            recover_us = startup_brownout[recover_key] * 1_000_000.0
            recover_target_us = BROWNOUT_RECOVERY_TARGET_US[rail]
            ok = recover_us <= recover_target_us
            results.append(
                CheckResult(
                    name=f"{rail_label} brownout recovery",
                    passed=ok,
                    detail=f"{recover_us:.1f} us  [target <= {recover_target_us:.0f} us]",
                )
            )

    return results


def _check_pi_frequency(pi_metrics: dict[str, float]) -> list[CheckResult]:
    """Validate frequency-domain PI metrics (impedance + PSRR proxies)."""
    results: list[CheckResult] = []

    for key, value in pi_metrics.items():
        if key.startswith("pi_zout_"):
            # key: pi_zout_<rail>_<band>
            parts = key.split("_")
            if len(parts) < 4:
                continue
            rail = parts[2]
            band = "_".join(parts[3:])
            if rail not in PI_ZOUT_TARGET_MOHM:
                continue
            zout_mohm = value * 1000.0
            target_mohm = PI_ZOUT_TARGET_MOHM[rail]
            ok = zout_mohm <= target_mohm
            results.append(
                CheckResult(
                    name=f"{rail.upper()} PI impedance ({band})",
                    passed=ok,
                    detail=f"{zout_mohm:.1f} mOhm  [target <= {target_mohm:.1f} mOhm]",
                )
            )
            continue

        if key.startswith("pi_psrr_"):
            # key: pi_psrr_<rail>_<band>
            parts = key.split("_")
            if len(parts) < 4:
                continue
            rail = parts[2]
            band = "_".join(parts[3:])
            if rail not in PI_PSRR_TARGET_DB:
                continue
            psrr_db = value
            target_db = PI_PSRR_TARGET_DB[rail]
            ok = psrr_db >= target_db
            results.append(
                CheckResult(
                    name=f"{rail.upper()} PI PSRR ({band})",
                    passed=ok,
                    detail=f"{psrr_db:.1f} dB  [target >= {target_db:.1f} dB]",
                )
            )

    return results


# ── Component tolerance corner sweep ──────────────────────────────────────────

# Voltage deviation allowed at any corner relative to nominal ± tolerance.
# IPC-2221A requires power rails stay within ±5 % across component corners.
CORNER_VOUT_TOLERANCE_FRACTION = 0.05

# Ripple budget for component corners in mV (tighter than MIL hard limit).
CORNER_RIPPLE_LIMIT_MV: dict[str, float] = {
    "5v": 60.0,
    "3v3": 36.0,
    "1v8": 24.0,
}

# Nominal output voltages for each rail (used to compute %-deviation).
CORNER_NOMINAL_V: dict[str, float] = {
    "5v": 5.0,
    "3v3": 3.3,
    "1v8": 1.8,
}


def _parse_meas_corner(text: str) -> dict[str, float]:
    """Extract component-tolerance corner sweep results.

    Convention:
        - corner_vout_<rail>    : average output voltage during settled period
        - corner_ripple_<rail>  : peak-to-peak ripple during settled period
        - corner_fb_<rail>      : feedback node voltage
    """
    results = _parse_measurements(text)
    return {
        k: v
        for k, v in results.items()
        if k.startswith("corner_vout_")
        or k.startswith("corner_ripple_")
        or k.startswith("corner_fb_")
    }


def _check_corner_sweep(corner: dict[str, float]) -> list[CheckResult]:
    """Validate component-corner sweep output voltages and ripple budgets."""
    results: list[CheckResult] = []

    for rail, nominal in CORNER_NOMINAL_V.items():
        vout_key = f"corner_vout_{rail}"
        if vout_key in corner:
            vout = corner[vout_key]
            dev_frac = abs(vout - nominal) / nominal
            ok = dev_frac <= CORNER_VOUT_TOLERANCE_FRACTION
            results.append(
                CheckResult(
                    name=f"{rail.upper()} corner Vout deviation",
                    passed=ok,
                    detail=(
                        f"{vout:.4f} V  dev={dev_frac * 100:.2f} %  "
                        f"[limit ±{CORNER_VOUT_TOLERANCE_FRACTION * 100:.0f} % of {nominal} V]"
                    ),
                )
            )

        ripple_key = f"corner_ripple_{rail}"
        if ripple_key in corner:
            ripple_mv = corner[ripple_key] * 1000.0
            limit_mv = CORNER_RIPPLE_LIMIT_MV.get(rail, 60.0)
            ok = ripple_mv <= limit_mv
            results.append(
                CheckResult(
                    name=f"{rail.upper()} corner ripple",
                    passed=ok,
                    detail=f"{ripple_mv:.2f} mV pk-pk  [limit {limit_mv:.0f} mV]",
                )
            )

    return results


# ── Main ──────────────────────────────────────────────────────────────────────


def _run_single_file(path: Path) -> tuple[int, int]:
    label = path.stem

    if not path.exists():
        print(
            f"[MIL-SPICE:{label}] '{path}' not found — "
            "no simulation data (add SPICE models + .meas directives "
            "to activate MIL-STD-704F / CS116 checks)."
        )
        return 0, 0

    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        print(f"[MIL-SPICE:{label}] Output empty — skipping checks.")
        return 0, 0

    measurements = _parse_measurements(text)
    ripple = _parse_meas_ripple(text)
    transients = _parse_meas_transient(text)
    load_step = _parse_meas_load_step(text)
    startup_brownout = _parse_meas_startup_brownout(text)
    pi_metrics = _parse_meas_pi(text)
    corner = _parse_meas_corner(text)

    if not measurements:
        print(f"[MIL-SPICE:{label}] No measurements found — skipping checks.")
        return 0, 0

    all_results: list[CheckResult] = []
    all_results += _check_dc(measurements)
    all_results += _check_ripple(ripple)
    all_results += _check_transients(measurements, transients)
    all_results += _check_load_step(load_step)
    all_results += _check_startup_brownout(startup_brownout)
    all_results += _check_pi_frequency(pi_metrics)
    all_results += _check_corner_sweep(corner)

    if not all_results:
        print(f"[MIL-SPICE:{label}] No matching rail measurements found.")
        return 0, 0

    print(f"[MIL-SPICE:{label}] Results:")
    for r in all_results:
        print(r)

    failures = [r for r in all_results if not r.passed]
    if failures:
        print(f"\n[MIL-SPICE:{label}] {len(failures)} violation(s) detected.")
        return 1, len(all_results)

    print(f"\n[MIL-SPICE:{label}] All {len(all_results)} checks passed.")
    return 0, len(all_results)


def main(output_files: list[str]) -> int:
    rc = 0
    total_checks = 0
    for output_file in output_files:
        file_rc, check_count = _run_single_file(Path(output_file))
        rc = max(rc, file_rc)
        total_checks += check_count

    print(f"\n[MIL-SPICE] Checked {len(output_files)} file(s), {total_checks} total checks.")
    return rc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate MIL SPICE output files.")
    parser.add_argument(
        "outputs",
        nargs="+",
        help="One or more ngspice output files to validate (single case or sweep cases).",
    )
    args = parser.parse_args()
    sys.exit(main(args.outputs))
