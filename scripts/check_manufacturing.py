#!/usr/bin/env python3
"""Cross-check BOM and pick-and-place files for manufacturing readiness.

Validates that every part in the BOM has placement data and vice-versa,
that all LCSC part numbers are filled in, and reports any bottom-side parts
that require a double-pass reflow oven cycle.

Usage:
    python scripts/check_manufacturing.py [<bom.csv>] [<positions.csv>]

Defaults to PCB/HermesFC/manufacturing/bom.csv and positions.csv.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

try:
    from pydantic import BaseModel
except ImportError:
    print("[ERROR] Missing dependency: pydantic v2 required.")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BOM = REPO_ROOT / "PCB/HermesFC/manufacturing/bom.csv"
DEFAULT_POS = REPO_ROOT / "PCB/HermesFC/manufacturing/positions.csv"


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        tag = "PASS" if self.passed else "FAIL"
        return f"[{tag}] {self.name}: {self.detail}"


# ── Parsers ───────────────────────────────────────────────────────────────────


def _read_bom(path: Path) -> dict[str, str]:
    """Return {designator: lcsc_part_number} from a grouped BOM CSV."""
    result: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            norm = {k.strip().lower(): v.strip() for k, v in row.items() if k is not None}
            raw_refs = norm.get("designator", norm.get("reference", norm.get("references", "")))
            lcsc = norm.get("lcsc part #", norm.get("lcsc_part_number", ""))
            for des in (d.strip() for d in raw_refs.split(",") if d.strip()):
                result[des] = lcsc
    return result


def _read_positions(path: Path) -> dict[str, str]:
    """Return {designator: layer} from a pick-and-place CSV."""
    result: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            norm = {k.strip().lower(): v.strip() for k, v in row.items() if k is not None}
            des = norm.get("designator", norm.get("ref", "")).strip()
            layer = norm.get("layer", "top").strip().lower()
            if des:
                result[des] = layer
    return result


# ── Checks ────────────────────────────────────────────────────────────────────


def check_bom_vs_positions(bom: dict[str, str], positions: dict[str, str]) -> CheckResult:
    """Every BOM designator must have a position entry (unless DNP)."""
    missing = sorted(d for d in bom if d not in positions)
    if missing:
        return CheckResult(
            name="BOM → Positions",
            passed=False,
            detail=f"{len(missing)} BOM part(s) missing placement data: {', '.join(missing)}",
        )
    return CheckResult(
        name="BOM → Positions",
        passed=True,
        detail=f"All {len(bom)} BOM designators have placement coordinates",
    )


def check_positions_vs_bom(bom: dict[str, str], positions: dict[str, str]) -> CheckResult:
    """Every position entry must appear in the BOM (no ghost placements)."""
    orphans = sorted(d for d in positions if d not in bom)
    if orphans:
        return CheckResult(
            name="Positions → BOM",
            passed=False,
            detail=f"{len(orphans)} placement(s) not in BOM: {', '.join(orphans)}",
        )
    return CheckResult(
        name="Positions → BOM",
        passed=True,
        detail=f"All {len(positions)} placed parts are listed in the BOM",
    )


def check_lcsc_coverage(bom: dict[str, str]) -> CheckResult:
    """Every part must have an LCSC part number for JLCPCB assembly."""
    missing = sorted(
        d for d, lcsc in bom.items() if not lcsc or lcsc.lower() in ("nan", "tbd", "n/a", "-", "")
    )
    if missing:
        return CheckResult(
            name="LCSC Coverage",
            passed=False,
            detail=f"{len(missing)} part(s) missing LCSC number: {', '.join(missing)}",
        )
    return CheckResult(
        name="LCSC Coverage",
        passed=True,
        detail=f"All {len(bom)} parts have LCSC part numbers",
    )


def check_bottom_side(positions: dict[str, str]) -> CheckResult:
    """Report bottom-side parts — they require a double-pass reflow cycle."""
    bottom = sorted(d for d, layer in positions.items() if layer in ("bottom", "b"))
    if bottom:
        return CheckResult(
            name="Bottom-Side Parts",
            passed=True,  # Informational: not a failure, but requires double-pass
            detail=f"{len(bottom)} bottom-side part(s) need double-pass reflow: {', '.join(bottom)}",
        )
    return CheckResult(
        name="Bottom-Side Parts",
        passed=True,
        detail="All parts are top-side — single-pass reflow sufficient",
    )


def check_unique_lcsc(bom: dict[str, str]) -> CheckResult:
    """Detect designators sharing an LCSC number but with different footprints.

    Duplicate LCSC numbers across the same value group are fine (e.g. 16×
    100n caps), but if the same LCSC number appears for obviously different
    designator prefixes it may indicate a copy-paste error in the BOM.
    This is a best-effort heuristic — not a hard failure.
    """
    from collections import defaultdict

    lcsc_to_refs: dict[str, list[str]] = defaultdict(list)
    for des, lcsc in bom.items():
        if lcsc:
            lcsc_to_refs[lcsc].append(des)

    # Flag LCSC codes shared across different component type prefixes (C vs R vs U etc.)
    suspicious: list[str] = []
    for lcsc, refs in lcsc_to_refs.items():
        prefixes = {r.rstrip("0123456789") for r in refs}
        if len(prefixes) > 1:
            suspicious.append(f"{lcsc} → {', '.join(sorted(refs))}")

    if suspicious:
        return CheckResult(
            name="LCSC Uniqueness",
            passed=False,
            detail=f"{len(suspicious)} LCSC number(s) shared across different component types: {'; '.join(suspicious)}",
        )
    return CheckResult(
        name="LCSC Uniqueness",
        passed=True,
        detail=f"{len(lcsc_to_refs)} unique LCSC part numbers, no cross-type duplicates",
    )


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> int:
    bom_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_BOM
    pos_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_POS

    if not bom_path.exists():
        print(f"[ERROR] BOM file not found: {bom_path}")
        return 1
    if not pos_path.exists():
        print(f"[ERROR] Position file not found: {pos_path}")
        return 1

    bom = _read_bom(bom_path)
    positions = _read_positions(pos_path)

    print(f"\nManufacturing Readiness — {bom_path.name} + {pos_path.name}")
    print(f"  {len(bom)} BOM designators | {len(positions)} placed parts")
    print()

    checks = [
        check_bom_vs_positions(bom, positions),
        check_positions_vs_bom(bom, positions),
        check_lcsc_coverage(bom),
        check_bottom_side(positions),
        check_unique_lcsc(bom),
    ]

    for result in checks:
        print(result)

    failures = [r for r in checks if not r.passed]
    print()
    if failures:
        print(f"[FAIL] {len(failures)} check(s) failed — manufacturing files need attention.")
        return 1
    print("[PASS] All manufacturing checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
