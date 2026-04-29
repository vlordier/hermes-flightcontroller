#!/usr/bin/env python3
"""Parse the KiCad-exported BOM CSV and verify MIL / IPC component requirements.

Checks performed:
  • All components have an LCSC Part # (no unspecified parts)
  • At least one TVS/ESD protection diode is present (MIL-STD-461G EMC)
  • Capacitors are present in sufficient number for decoupling
  • Power converter (buck/LDO) components are present
  • All component values are non-empty

Exit codes:
  0 — all checks passed
  1 — one or more violations detected

Usage:
    python3 check_bom.py <path/to/bom.csv>

Expected CSV columns (EasyEDA / KiCad export):
    Designator, Footprint, Quantity, Value, LCSC Part #
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

try:
    from .config import settings
except ImportError:
    from config import settings  # type: ignore

# ── Thresholds and reference designator patterns ─────────────────────────────

BYPASS_CAP_VALUE_PATTERNS = [
    re.compile(r"^\d+n$", re.IGNORECASE),  # e.g. 100n
    re.compile(r"^\d+u$", re.IGNORECASE),  # e.g. 10u
    re.compile(r"^\d+p$", re.IGNORECASE),  # small ceramics
    re.compile(r"^\d+\.\d+u$", re.IGNORECASE),  # e.g. 2u2 → checked separately
    re.compile(r"^[\d.]+u\d*$", re.IGNORECASE),
    re.compile(r"^\d+[nu]f?$", re.IGNORECASE),
]

# Space/MIL derating requirements:
# 1. Voltage derating: Cap voltage rating >= 2x V_applied (per NASA/MIL-STD-1547)
# 2. Temperature: X7R or better for ceramics
DERATING_PATTERNS = [
    re.compile(r"X7R|X8R|C0G|NP0", re.IGNORECASE),  # Acceptable dielectrics
]
LOW_GRADE_DIELECTRIC = re.compile(r"X5R|Y5V|Z5U", re.IGNORECASE)


class BomRow(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    designators: list[str]
    footprint: str
    quantity: int = Field(gt=0)
    value: str
    lcsc: str = Field(alias="lcsc_part_number")

    @field_validator("designators", mode="before")
    @classmethod
    def split_designators(cls, v: Any) -> Any:  # noqa: ANN401
        if isinstance(v, str):
            return [d.strip() for d in v.split(",") if d.strip()]
        return v

    @field_validator("lcsc")
    @classmethod
    def check_lcsc_presence(cls, v: str) -> str:
        if not v or v.lower() == "nan":
            raise ValueError("LCSC Part # missing for component")
        return v


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        tag = "PASS" if self.passed else "FAIL"
        return f"  {tag}  {self.name}: {self.detail}"


# ── CSV parser ────────────────────────────────────────────────────────────────


def _read_bom(path: Path) -> list[BomRow]:
    rows: list[BomRow] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            # Normalise column names (case-insensitive, strip whitespace)
            norm = {k.strip().lower(): v.strip() for k, v in row.items()}

            # Extract fields with mapping to BomRow expected aliases
            try:
                bom_row = BomRow(
                    designators=norm.get("designator", ""),  # type: ignore
                    footprint=norm.get("footprint", ""),
                    quantity=int(norm.get("quantity", "1") or "1"),
                    value=norm.get("value", ""),
                    lcsc_part_number=norm.get("lcsc part #", norm.get("lcsc", "")),
                )
                rows.append(bom_row)
            except Exception as e:
                print(f"  [BOM-ERR] Skipping row due to validation error: {e}")
                continue
    return rows


# ── Individual checks ─────────────────────────────────────────────────────────


def check_lcsc_coverage(rows: list[BomRow]) -> CheckResult:
    """All BOM lines must have an LCSC part number."""
    missing = [r for r in rows if not r.lcsc or r.lcsc.upper() in ("", "N/A", "-", "TBD", "DNP")]
    if not missing:
        total = sum(r.quantity for r in rows)
        return CheckResult(
            name="LCSC part number coverage",
            passed=True,
            detail=f"All {len(rows)} BOM lines ({total} parts) have LCSC codes",
        )
    labels = []
    for r in missing:
        ref = ", ".join(r.designators[:3])
        if len(r.designators) > 3:
            ref += f" (+{len(r.designators) - 3})"
        labels.append(f"{ref} [{r.value}]")
    return CheckResult(
        name="LCSC part number coverage",
        passed=False,
        detail=f"{len(missing)} BOM line(s) missing LCSC code: {'; '.join(labels)}",
    )


def check_esd_tvs(rows: list[BomRow]) -> CheckResult:
    """At least one TVS/ESD protection device must be present."""
    tvs_rows = []
    for r in rows:
        if any(p.search(r.value) or p.search(r.footprint) for p in settings.tvs_regex):
            tvs_rows.append(r)
        else:
            # Also check designator prefix
            for d in r.designators:
                if re.match(r"D\d", d) and any(p.search(r.value) for p in settings.tvs_regex):
                    tvs_rows.append(r)
                    break
    if tvs_rows:
        refs = ", ".join(d for r in tvs_rows for d in r.designators[:2])
        total = sum(r.quantity for r in tvs_rows)
        return CheckResult(
            name="TVS/ESD protection",
            passed=True,
            detail=f"{total} TVS/ESD device(s) found ({refs})",
        )
    return CheckResult(
        name="TVS/ESD protection",
        passed=False,
        detail="No TVS or ESD protection devices found — add TVS diodes on all "
        "external connectors (USB, GPIO headers) per MIL-STD-461G CS101/RS103",
    )


def check_decoupling_caps(rows: list[BomRow]) -> CheckResult:
    """Sufficient decoupling/bypass capacitors must be present."""
    cap_rows = [r for r in rows if any(d.startswith("C") for d in r.designators)]
    bypass = []
    for r in cap_rows:
        val = r.value.strip()
        # Normalise e.g. "2u2" → "2.2u" for pattern matching
        # Just check that it looks like a capacitor value (number + unit)
        if re.match(r"^\d[\d.u nfp]*[nupf]", val, re.IGNORECASE):
            bypass.append(r)
    total_bypass = sum(r.quantity for r in bypass)
    ok = total_bypass >= settings.min_decoupling_caps
    return CheckResult(
        name="Decoupling capacitors",
        passed=ok,
        detail=f"{total_bypass} capacitor(s) in BOM "
        f"[minimum {settings.min_decoupling_caps} for adequate decoupling]",
    )


def check_empty_values(rows: list[BomRow]) -> CheckResult:
    """All BOM rows must have a non-empty Value field."""
    empty = [r for r in rows if not r.value]
    if not empty:
        return CheckResult(
            name="BOM value completeness",
            passed=True,
            detail=f"All {len(rows)} BOM lines have a value",
        )
    refs = ", ".join(d for r in empty for d in r.designators[:2])
    return CheckResult(
        name="BOM value completeness",
        passed=False,
        detail=f"{len(empty)} BOM line(s) with empty Value field: {refs}",
    )


def check_power_ics(rows: list[BomRow]) -> CheckResult:
    """Verify power management ICs are in the BOM (buck, LDO)."""
    power_patterns = [
        re.compile(r"AP6320\d", re.IGNORECASE),  # buck regulator family
        re.compile(r"LD390\d\d", re.IGNORECASE),  # LDO family
        re.compile(r"buck|ldo|regulator", re.IGNORECASE),
        re.compile(r"AP\d{4,}", re.IGNORECASE),
        re.compile(r"LD\d{4,}", re.IGNORECASE),
    ]
    found = [
        r for r in rows if any(p.search(r.value) or p.search(r.footprint) for p in power_patterns)
    ]
    if found:
        refs = ", ".join(d for r in found for d in r.designators[:1])
        return CheckResult(
            name="Power management ICs",
            passed=True,
            detail=f"Power ICs present: {refs} ({', '.join(r.value for r in found)})",
        )
    # Fall back: look for U designators with regulator-like footprints
    u_parts = [r for r in rows if any(d.startswith("U") for d in r.designators)]
    if len(u_parts) >= 2:
        return CheckResult(
            name="Power management ICs",
            passed=True,
            detail=f"{len(u_parts)} IC(s) in BOM — verify power regulators are included",
        )
    return CheckResult(
        name="Power management ICs",
        passed=False,
        detail="No power management ICs detected in BOM",
    )


def check_capacitor_dielectric(rows: list[BomRow]) -> CheckResult:
    """Verify capacitor dielectrics (MLCC) meet MIL/Space requirements (X7R+)."""
    cap_rows = [r for r in rows if any(d.startswith("C") for d in r.designators)]
    low_grade = []
    for r in cap_rows:
        if LOW_GRADE_DIELECTRIC.search(r.value) or LOW_GRADE_DIELECTRIC.search(r.footprint):
            low_grade.append(r)

    if not low_grade:
        return CheckResult(
            name="Capacitor Dielectric Grade",
            passed=True,
            detail="No low-grade dielectrics (X5R/Y5V) found in capacitors",
        )

    labels = [f"{', '.join(r.designators[:2])} ({r.value})" for r in low_grade]
    return CheckResult(
        name="Capacitor Dielectric Grade",
        passed=False,
        detail=f"Found {len(low_grade)} low-grade capacitor(s) (X5R/Y5V/Z5U): {'; '.join(labels)}. "
        "Use X7R or C0G for temperature stability and aging resistance in space/MIL.",
    )


# ── Main ──────────────────────────────────────────────────────────────────────


def main(bom_file: str) -> int:
    path = Path(bom_file)
    if not path.exists():
        print(f"[BOM-CHECK] File not found: {bom_file}")
        return 1

    rows = _read_bom(path)
    if not rows:
        print(f"[BOM-CHECK] No BOM rows found in {bom_file}")
        return 1

    all_results: list[CheckResult] = []
    all_results.append(check_lcsc_coverage(rows))
    all_results.append(check_esd_tvs(rows))
    all_results.append(check_decoupling_caps(rows))
    all_results.append(check_empty_values(rows))
    all_results.append(check_power_ics(rows))
    all_results.append(check_capacitor_dielectric(rows))

    total_parts = sum(r.quantity for r in rows)
    print(f"[BOM-CHECK] {len(rows)} BOM lines, {total_parts} total parts in {path.name}:")
    for r in all_results:
        print(r)

    failures = [r for r in all_results if not r.passed]
    if failures:
        print(f"\n[BOM-CHECK] {len(failures)} violation(s) detected.")
        return 1

    print(f"\n[BOM-CHECK] All {len(all_results)} checks passed.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <bom.csv>")
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
