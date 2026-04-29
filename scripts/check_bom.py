#!/usr/bin/env python3
"""Parse the KiCad-exported BOM CSV and verify MIL / IPC component requirements."""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path
from typing import Any, cast

try:
    from pydantic import BaseModel, ConfigDict, Field, field_validator
except ImportError:
    print("[ERROR] Missing dependencies: pydantic v2 required.")
    sys.exit(1)

try:
    from .config import settings
except ImportError:
    from config import settings  # type: ignore

DERATING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"X7R|X8R|C0G|NP0", re.IGNORECASE),
]
LOW_GRADE_DIELECTRIC: re.Pattern[str] = re.compile(r"X5R|Y5V|Z5U", re.IGNORECASE)


class BomRow(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)
    internal_designators: list[str] = Field(alias="designators")
    footprint: str
    quantity: int = Field(gt=0)
    value: str
    lcsc: str = Field(alias="lcsc_part_number")

    @field_validator("internal_designators", mode="before")
    @classmethod
    def split_designators(cls, v: Any) -> Any:  # noqa: ANN401
        if isinstance(v, str):
            return [d.strip() for d in v.split(",") if d.strip()]
        return cast(Any, v)

    @field_validator("lcsc")
    @classmethod
    def check_lcsc_presence(cls, v: str) -> str:
        if not v or v.lower() == "nan":
            raise ValueError("LCSC Part # missing")
        return v


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        tag = "PASS" if self.passed else "FAIL"
        return f"[{tag}] {self.name}: {self.detail}"


def _read_bom(path: Path) -> list[BomRow]:
    rows: list[BomRow] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            norm = {k.strip().lower(): v.strip() for k, v in row.items() if k is not None}
            raw_designators = norm.get("designator", norm.get("reference", ""))
            try:
                bom_row = BomRow(
                    designators=cast(Any, raw_designators),
                    footprint=norm.get("footprint", ""),
                    quantity=int(norm.get("quantity", "1") or "1"),
                    value=norm.get("value", ""),
                    lcsc_part_number=norm.get("lcsc part #", norm.get("pcbli", "")),
                )
                rows.append(bom_row)
            except Exception as e:
                print(f"[BOM-ERR] Skipping row due to validation error: {e}")
                continue
    return rows


def check_lcsc_coverage(rows: list[BomRow]) -> CheckResult:
    missing = [r for r in rows if not r.lcsc or r.lcsc.upper() in ("", "N/A", "-", "TBD", "DNP")]
    if not missing:
        return CheckResult(
            name="LCSC part number coverage",
            passed=True,
            detail=f"All {len(rows)} BOM lines have LCSC codes",
        )
    return CheckResult(
        name="LCSC part number coverage",
        passed=False,
        detail=f"{len(missing)} line(s) missing LCSC code",
    )


def check_esd_tvs(rows: list[BomRow]) -> CheckResult:
    found = [
        q
        for q in rows
        if any(p.search(q.value) or p.search(q.footprint) for p in settings.tvs_regex)
    ]
    if found:
        return CheckResult(
            name="TVS/ESD protection", passed=True, detail=f"Found {len(found)} device(s)"
        )
    return CheckResult(
        name="TVS/ESD protection", passed=False, detail="No TVS or ESD protection devices found"
    )


def check_decoupling_caps(rows: list[BomRow]) -> CheckResult:
    total = sum(r.quantity for r in rows if any(d.startswith("C") for d in r.internal_designators))
    ok = total >= settings.min_decoupling_caps
    return CheckResult(
        name="Decoupling capacitors",
        passed=ok,
        detail=f"{total} capacitor(s) [min {settings.min_decoupling_caps}]",
    )


def check_empty_values(rows: list[BomRow]) -> CheckResult:
    empty = [r for r in rows if not r.value]
    if not empty:
        return CheckResult(
            name="BOM value completeness", passed=True, detail="All lines have a value"
        )
    return CheckResult(
        name="BOM value completeness", passed=False, detail=f"{len(empty)} line(s) with empty value"
    )


def check_power_ics(rows: list[BomRow]) -> CheckResult:
    power_patterns = [re.compile(r"APB6320\d|LD390\d\d|buck|ldo|regulator", re.IGNORECASE)]
    found = sum(
        1 for r in rows if any(p.search(r.value) or p.search(r.footprint) for p in power_patterns)
    )
    if found:
        return CheckResult(
            name="Power management ICs", passed=True, detail=f"Found {found} power IC(s)"
        )
    return CheckResult(
        name="Power management ICs", passed=False, detail="No power management ICs detected"
    )


def check_capacitor_dielectric(rows: list[BomRow]) -> CheckResult:
    low_grade = sum(
        1
        for r in rows
        if (LOW_GRADE_DIELECTRIC.search(r.value) or LOW_GRADE_DIELECTRIC.search(r.footprint))
        and any(d.startswith("C") for d in r.internal_designators)
    )
    if not low_grade:
        return CheckResult(
            name="Capacitor Dielectric Grade", passed=True, detail="No low-grade dielectrics found"
        )
    return CheckResult(
        name="Capacitor Dielectric Grade",
        passed=False,
        detail=f"Found {low_grade} low-grade cap(s)",
    )


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: check_bom.py <BOM_CSV>")
        sys.exit(1)
    bom_file = Path(sys.argv[1])
    if not bom_file.exists():
        print(f"[BOM-CHECK] File not found: {bom_file}")
        sys.exit(1)
    rows = _read_bom(bom_file)
    if not rows:
        print(f"[BOM-CHECK] No BOM rows found in {bom_file.name}")
        sys.exit(1)
    results = [
        check_lcsc_coverage(rows),
        check_esd_tvs(rows),
        check_decoupling_caps(rows),
        check_empty_values(rows),
        check_power_ics(rows),
        check_capacitor_dielectric(rows),
    ]
    for r in results:
        print(r)
    if any(not r.passed for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
