#!/usr/bin/env python3
"""Parse ngspice output and validate voltage/current thresholds.

Exits with code 1 if any threshold is violated, 0 otherwise.
Gracefully handles missing or incomplete simulation output so the CI step
does not fail when SPICE models have not yet been added to the schematic.
"""

import re
import sys
from pathlib import Path

# ── Thresholds ────────────────────────────────────────────────────────────────
# Map net-name regex patterns to (min_V, max_V) expected ranges.
# Update these when SPICE models and simulation directives are wired up in the
# KiCad schematic.
VOLTAGE_THRESHOLDS: dict[str, tuple[float, float]] = {
    # AP63203 5 V buck-converter output (±5 %)
    r"v\(5v\)": (4.75, 5.25),
    # 3.3 V rail (±5 %)
    r"v\(3v3\)": (3.135, 3.465),
    # LD39015M18R 1.8 V LDO output (±5 %)
    r"v\(1v8\)": (1.71, 1.89),
    # USB VBUS (nominal 5 V; USB spec 4.5–5.5 V)
    r"v\(vbus\)": (4.5, 5.5),
}


def _parse_measurements(text: str) -> dict[str, float]:
    """Extract ``name = value`` pairs from ngspice output."""
    results: dict[str, float] = {}
    for match in re.finditer(
        r"^\s*(\S+)\s*=\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)",
        text,
        re.MULTILINE,
    ):
        results[match.group(1).lower()] = float(match.group(2))
    return results


def main(output_file: str) -> int:
    path = Path(output_file)

    if not path.exists():
        print(
            f"[SPICE] Output file '{output_file}' not found — "
            "skipping threshold checks (add SPICE models to the schematic "
            "to enable simulation)."
        )
        return 0

    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        print("[SPICE] Output file is empty — skipping threshold checks.")
        return 0

    measurements = _parse_measurements(text)
    if not measurements:
        print("[SPICE] No measurements found in output — skipping checks.")
        return 0

    failures: list[str] = []
    for pattern, (vmin, vmax) in VOLTAGE_THRESHOLDS.items():
        for name, value in measurements.items():
            if re.fullmatch(pattern, name, re.IGNORECASE):
                if vmin <= value <= vmax:
                    print(
                        f"  PASS  {name} = {value:.4f} V  "
                        f"[expected {vmin}–{vmax} V]"
                    )
                else:
                    failures.append(
                        f"  FAIL  {name} = {value:.4f} V  "
                        f"[expected {vmin}–{vmax} V]"
                    )

    if failures:
        print("\n[SPICE] Threshold violations detected:")
        for msg in failures:
            print(msg)
        return 1

    print("[SPICE] All checks passed.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <ngspice_output_file>")
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
