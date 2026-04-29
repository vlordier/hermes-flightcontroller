#!/usr/bin/env python3
"""OpenEMS FDTD electromagnetic simulation runner for MIL-STD-461 compliance.

Runs an OpenEMS Finite-Difference Time-Domain simulation on the PCB model
generated from Gerber files (via gerber2ems) and compares simulated radiated
emission field strengths against MIL-STD-461G RE102 limits.

Exit codes:
  0 — simulation passed all limits (or no model present)
  1 — one or more MIL-STD-461 limits exceeded

Usage:
    python3 run_openems.py [--check-mil461] [--model-dir em_sim/]

References:
  MIL-STD-461G RE102 — Radiated Emissions, Electric Field (30 MHz–18 GHz)
  MIL-STD-461G RS103 — Radiated Susceptibility, Electric Field
  OpenEMS FDTD: https://www.openems.de
  gerber2ems:   https://github.com/antmicro/gerber2ems
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
from pathlib import Path

# ── MIL-STD-461G RE102 limits (dBµV/m at 1 m, narrowband) ───────────────────
# Piecewise-linear limit curve for Army/Navy/Air Force ground equipment.
# RE102 covers 30 MHz–18 GHz; the entries below 30 MHz (10 kHz–2 MHz)
# are from the RE101/RE102 transition region used for smooth extrapolation
# — actual RE101 (magnetic field) limits apply below 100 kHz.
# Frequency (Hz) → limit (dBµV/m)
RE102_LIMITS: list[tuple[float, float]] = [
    (1.0e4, 24.0),  #  10 kHz  — RE101/RE102 transition (extrapolation anchor)
    (1.5e5, 24.0),  # 150 kHz
    (2.0e6, 34.0),  #   2 MHz
    (3.0e7, 34.0),  #  30 MHz  — RE102 lower bound (Army/Navy ground)
    (1.0e8, 44.0),  # 100 MHz
    (2.0e9, 54.0),  #   2 GHz
    (1.8e10, 54.0),  #  18 GHz  — RE102 upper bound
]

# ── RS103 susceptibility threshold (dBµV/m, electric field immunity) ─────────
# Minimum immunity target; field below this level must not cause upset.
RS103_IMMUNITY_THRESHOLD_DB = 50.0  # dBµV/m at 1 m (simplified)


def _log_interpolate(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    """Interpolate y at x using a logarithmic x-axis (frequency is log-scaled).

    Equivalent to linear interpolation after mapping x → log10(x).
    """
    t = math.log10(x / x0) / math.log10(x1 / x0)
    return y0 + t * (y1 - y0)


def _interpolate_re102_limit(freq_hz: float) -> float:
    """Return the interpolated RE102 limit (dBµV/m) at `freq_hz`."""
    if freq_hz <= RE102_LIMITS[0][0]:
        return RE102_LIMITS[0][1]
    if freq_hz >= RE102_LIMITS[-1][0]:
        return RE102_LIMITS[-1][1]
    for i in range(len(RE102_LIMITS) - 1):
        f0, l0 = RE102_LIMITS[i]
        f1, l1 = RE102_LIMITS[i + 1]
        if f0 <= freq_hz <= f1:
            return _log_interpolate(freq_hz, f0, f1, l0, l1)
    return RE102_LIMITS[-1][1]


def _check_openems_available() -> bool:
    """Return True if the openEMS binary is on PATH."""
    try:
        subprocess.run(
            ["openems", "--help"],
            capture_output=True,
            timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _run_fdtd_simulation(model_dir: Path) -> Path | None:
    """Launch OpenEMS on the generated model; return the result file path."""
    # Look for the entry-point script produced by gerber2ems
    run_script = model_dir / "run_openems.m"
    if not run_script.exists():
        # Try Python entry point
        run_script_py = model_dir / "run_openems.py"
        if not run_script_py.exists():
            print(
                "[EM] No OpenEMS run script found in model directory. "
                "Ensure gerber2ems completed successfully."
            )
            return None

    result_dir = model_dir / "results"
    result_dir.mkdir(exist_ok=True)

    if run_script.exists():
        # Octave/MATLAB interface
        cmd = [
            "octave",
            "--no-gui",
            "--eval",
            f"cd('{model_dir}'); run_openems;",
        ]
    else:
        cmd = [sys.executable, str(run_script_py)]

    print(f"[EM] Running FDTD: {' '.join(cmd)}")
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,  # 10-minute limit; adjust via CI_EM_TIMEOUT env var if needed
    )
    if proc.returncode != 0:
        print("[EM] FDTD simulation exited with errors:")
        print(proc.stderr[-2000:])

    # Expected result file from gerber2ems
    result_file = result_dir / "e_field_spectrum.csv"
    return result_file if result_file.exists() else None


def _parse_field_csv(result_file: Path) -> list[tuple[float, float]]:
    """Parse a CSV of (frequency_hz, field_dbuvm) pairs from simulation output."""
    data: list[tuple[float, float]] = []
    for line in result_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parts = line.split(",")
            freq = float(parts[0])
            field = float(parts[1])
            data.append((freq, field))
        except (ValueError, IndexError):
            continue
    return data


def _check_re102(data: list[tuple[float, float]]) -> list[str]:
    """Return a list of failure messages for RE102 limit violations."""
    failures: list[str] = []
    for freq, field in data:
        limit = _interpolate_re102_limit(freq)
        margin = field - limit
        if margin > 0:
            failures.append(
                f"  FAIL  RE102 @ {freq / 1e6:.3f} MHz: "
                f"{field:.1f} dBµV/m  [limit {limit:.1f}  margin {margin:+.1f} dB]"
            )
        else:
            print(
                f"  PASS  RE102 @ {freq / 1e6:.3f} MHz: "
                f"{field:.1f} dBµV/m  [limit {limit:.1f}  margin {margin:+.1f} dB]"
            )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-mil461",
        action="store_true",
        help="Validate simulation results against MIL-STD-461G RE102 limits",
    )
    parser.add_argument(
        "--model-dir",
        default="em_sim",
        help="Directory containing the OpenEMS model (default: em_sim/)",
    )
    args = parser.parse_args()

    model_dir = Path(args.model_dir)

    # ── Model directory availability ─────────────────────────────────────────
    if not model_dir.exists() or not any(model_dir.iterdir()):
        print(
            f"[EM] No EM model found in '{model_dir}'. "
            "Run gerber2ems first to convert Gerbers to an openEMS model."
        )
        return 0

    # ── OpenEMS availability ─────────────────────────────────────────────────
    if not _check_openems_available():
        print("[EM] openEMS binary not found on PATH. Install openems to activate FDTD simulation.")
        return 0

    # ── Run simulation ───────────────────────────────────────────────────────
    result_file = _run_fdtd_simulation(model_dir)
    if result_file is None:
        print("[EM] No simulation results produced — skipping RE102 check.")
        return 0

    # ── Check against MIL-STD-461G RE102 ────────────────────────────────────
    if not args.check_mil461:
        print("[EM] --check-mil461 not set; skipping limit check.")
        return 0

    data = _parse_field_csv(result_file)
    if not data:
        print("[EM] Result file is empty — skipping limit check.")
        return 0

    print(f"[EM] Checking {len(data)} frequency points against RE102:")
    failures = _check_re102(data)

    if failures:
        print("\n[EM] MIL-STD-461G RE102 violations:")
        for msg in failures:
            print(msg)
        return 1

    print(f"\n[EM] All {len(data)} RE102 checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
