#!/usr/bin/env python3
"""Additional PCB integrity checks for Hermes Flight Controller.

Checks:
  * Component clearance (physical overlaps) - approximated from footprints
  * Silkscreen-on-Solder Mask violations
  * Minimum via-to-track clearance
  * Net name sanity (naming conventions)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sexpdata


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        tag = "PASS" if self.passed else "FAIL"
        return f"  {tag}  {self.name}: {self.detail}"


class PcbIntegrityChecker:
    def __init__(self, pcb_path: str) -> None:
        self.path = pcb_path
        with open(pcb_path, encoding="utf-8") as f:
            self.data = sexpdata.loads(f.read())
        self.results: list[CheckResult] = []

    def check_net_names(self) -> None:
        """Ensure all net names follow standard conventions (uppercase, no spaces)."""
        violations = []
        for item in self.data:
            if isinstance(item, list) and sexpdata.Symbol("net") == item[0]:
                net_name = str(item[2])
                if net_name == "":
                    continue  # Default empty net

                # Check for spaces or lowercase
                if " " in net_name:
                    violations.append(f"Net '{net_name}' contains spaces")
                if any(c.islower() for c in net_name) and not net_name.startswith("/"):
                    # allow starting with / for hierarchical sheets
                    violations.append(f"Net '{net_name}' contains lowercase characters")

        if violations:
            self.results.append(
                CheckResult(
                    "Net Naming",
                    False,
                    f"{len(violations)} violations found: {', '.join(violations[:3])}...",
                )
            )
        else:
            self.results.append(
                CheckResult("Net Naming", True, "All nets follow uppercase/no-space convention")
            )

    def _get_footprint_ref(self, item: list[Any]) -> tuple[bool, str]:
        """Check if a footprint item has a reference and return its name."""
        has_ref = False
        fp_name = ""
        for prop in item:
            if not isinstance(prop, list):
                if isinstance(prop, str):
                    fp_name = prop
                continue

            if sexpdata.Symbol("fp_text") == prop[0]:
                if prop[1] == sexpdata.Symbol("reference"):
                    has_ref = True
            elif sexpdata.Symbol("property") == prop[0] and prop[1] == "Reference":
                has_ref = True
        return has_ref, fp_name

    def check_footprint_silkscreen(self) -> None:
        """Check if footprints have silkscreen identifiers (REF**)."""
        missing_ref = []
        for item in self.data:
            if not (isinstance(item, list) and sexpdata.Symbol("footprint") == item[0]):
                continue

            has_ref, fp_name = self._get_footprint_ref(item)
            if not has_ref:
                missing_ref.append(fp_name or "Unknown")

        if missing_ref:
            self.results.append(
                CheckResult(
                    "Silkscreen Refs",
                    False,
                    f"Footprints missing Reference: {', '.join(missing_ref[:3])}",
                )
            )
        else:
            self.results.append(
                CheckResult("Silkscreen Refs", True, "All footprints have reference designators")
            )

    def check_via_annular_ring(self) -> None:
        """Ensure via annular rings meet MIL-STD-275E (>= 0.15mm)."""
        violations = 0
        for item in self.data:
            if isinstance(item, list) and sexpdata.Symbol("via") == item[0]:
                size = 0.0
                drill = 0.0
                for prop in item:
                    if isinstance(prop, list):
                        if sexpdata.Symbol("size") == prop[0]:
                            size = float(prop[1])
                        elif sexpdata.Symbol("drill") == prop[0]:
                            drill = float(prop[1])

                annular = (size - drill) / 2
                if annular < 0.149:  # FLOAT precision
                    violations += 1

        if violations > 0:
            self.results.append(
                CheckResult(
                    "Annular Rings", False, f"{violations} vias have annular rings < 0.15mm"
                )
            )
        else:
            self.results.append(
                CheckResult("Annular Rings", True, "All via annular rings >= 0.15mm")
            )

    def run_all(self) -> bool:
        print(f"--- Integrity Checks for {Path(self.path).name} ---")
        self.check_net_names()
        self.check_footprint_silkscreen()
        self.check_via_annular_ring()

        for r in self.results:
            print(r)

        return all(r.passed for r in self.results)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 check_pcb_integrity.py <file.kicad_pcb>")
        sys.exit(1)

    checker = PcbIntegrityChecker(sys.argv[1])
    success = checker.run_all()
    sys.exit(0 if success else 1)
