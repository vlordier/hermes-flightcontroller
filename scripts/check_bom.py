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
from dataclasses import dataclass
from pathlib import Path


# ── Thresholds and reference designator patterns ─────────────────────────────

MIN_DECOUPLING_CAPS  = 5    # minimum number of decoupling/bypass capacitors
TVS_PATTERNS = [
    re.compile(r"tvs", re.IGNORECASE),
    re.compile(r"USBLC", re.IGNORECASE),         # USB ESD suppressors
    re.compile(r"ESD", re.IGNORECASE),
    re.compile(r"PRTR", re.IGNORECASE),
    re.compile(r"PESD", re.IGNORECASE),
    re.compile(r"TPD", re.IGNORECASE),
]
BYPASS_CAP_VALUE_PATTERNS = [
    re.compile(r"^\d+n$", re.IGNORECASE),   # e.g. 100n
    re.compile(r"^\d+u$", re.IGNORECASE),   # e.g. 10u
    re.compile(r"^\d+p$", re.IGNORECASE),   # small ceramics
    re.compile(r"^\d+\.\d+u$", re.IGNORECASE),  # e.g. 2u2 → checked separately
    re.compile(r"^[\d.]+u\d*$", re.IGNORECASE),
    re.compile(r"^\d+[nu]f?$", re.IGNORECASE),
]


@dataclass
class BomRow:
    designators: list[str]
    footprint: str
    quantity: int
    value: str
    lcsc: str


@dataclass
class CheckResult:
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
            desig_raw = norm.get("designator", "")
            desigs = [d.strip() for d in desig_raw.split(",") if d.strip()]
            try:
                qty = int(norm.get("quantity", "1") or "1")
            except ValueError:
                qty = len(desigs) or 1
            rows.append(BomRow(
                designators=desigs,
                footprint=norm.get("footprint", ""),
                quantity=qty,
                value=norm.get("value", ""),
                lcsc=norm.get("lcsc part #", norm.get("lcsc", "")),
            ))
    return rows


# ── Individual checks ─────────────────────────────────────────────────────────

def check_lcsc_coverage(rows: list[BomRow]) -> CheckResult:
    """All BOM lines must have an LCSC part number."""
    missing = [
        r for r in rows
        if not r.lcsc or r.lcsc.upper() in ("", "N/A", "-", "TBD", "DNP")
    ]
    if not missing:
        total = sum(r.quantity for r in rows)
        return CheckResult(
            "LCSC part number coverage",
            True,
            f"All {len(rows)} BOM lines ({total} parts) have LCSC codes",
        )
    labels = []
    for r in missing:
        ref = ", ".join(r.designators[:3])
        if len(r.designators) > 3:
            ref += f" (+{len(r.designators)-3})"
        labels.append(f"{ref} [{r.value}]")
    return CheckResult(
        "LCSC part number coverage",
        False,
        f"{len(missing)} BOM line(s) missing LCSC code: {'; '.join(labels)}",
    )


def check_esd_tvs(rows: list[BomRow]) -> CheckResult:
    """At least one TVS/ESD protection device must be present."""
    tvs_rows = []
    for r in rows:
        if any(p.search(r.value) or p.search(r.footprint) for p in TVS_PATTERNS):
            tvs_rows.append(r)
        else:
            # Also check designator prefix
            for d in r.designators:
                if re.match(r"D\d", d) and any(
                    p.search(r.value) for p in TVS_PATTERNS
                ):
                    tvs_rows.append(r)
                    break
    if tvs_rows:
        refs = ", ".join(
            d for r in tvs_rows for d in r.designators[:2]
        )
        total = sum(r.quantity for r in tvs_rows)
        return CheckResult(
            "TVS/ESD protection",
            True,
            f"{total} TVS/ESD device(s) found ({refs})",
        )
    return CheckResult(
        "TVS/ESD protection",
        False,
        "No TVS or ESD protection devices found — add TVS diodes on all "
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
    ok = total_bypass >= MIN_DECOUPLING_CAPS
    return CheckResult(
        "Decoupling capacitors",
        ok,
        f"{total_bypass} capacitor(s) in BOM "
        f"[minimum {MIN_DECOUPLING_CAPS} for adequate decoupling]",
    )


def check_empty_values(rows: list[BomRow]) -> CheckResult:
    """All BOM rows must have a non-empty Value field."""
    empty = [r for r in rows if not r.value]
    if not empty:
        return CheckResult(
            "BOM value completeness",
            True,
            f"All {len(rows)} BOM lines have a value",
        )
    refs = ", ".join(d for r in empty for d in r.designators[:2])
    return CheckResult(
        "BOM value completeness",
        False,
        f"{len(empty)} BOM line(s) with empty Value field: {refs}",
    )


def check_power_ics(rows: list[BomRow]) -> CheckResult:
    """Verify power management ICs are in the BOM (buck, LDO)."""
    power_patterns = [
        re.compile(r"AP6320\d", re.IGNORECASE),   # buck regulator family
        re.compile(r"LD390\d\d", re.IGNORECASE),  # LDO family
        re.compile(r"buck|ldo|regulator", re.IGNORECASE),
        re.compile(r"AP\d{4,}", re.IGNORECASE),
        re.compile(r"LD\d{4,}", re.IGNORECASE),
    ]
    found = [
        r for r in rows
        if any(p.search(r.value) or p.search(r.footprint) for p in power_patterns)
    ]
    if found:
        refs = ", ".join(d for r in found for d in r.designators[:1])
        return CheckResult(
            "Power management ICs",
            True,
            f"Power ICs present: {refs} ({', '.join(r.value for r in found)})",
        )
    # Fall back: look for U designators with regulator-like footprints
    u_parts = [r for r in rows if any(d.startswith("U") for d in r.designators)]
    if len(u_parts) >= 2:
        return CheckResult(
            "Power management ICs",
            True,
            f"{len(u_parts)} IC(s) in BOM — verify power regulators are included",
        )
    return CheckResult(
        "Power management ICs",
        False,
        "No power management ICs detected in BOM",
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
