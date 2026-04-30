#!/usr/bin/env python3
"""Cross-domain PCB robustness checks (thermal / RF / mechanical).

This checker complements DRC/ERC/SPICE by adding practical robustness gates:
  - Thermal: high-current track widths and thermal-via relief near power ICs
  - RF/timing: crystal network presence and local via fencing near oscillator
  - Mechanical/vibration: mounting-hole count and edge margin for heavy parts

These are heuristic design-for-reliability checks intended for early CI gating.
"""

from __future__ import annotations

import csv
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import sexpdata
from pydantic import BaseModel


@dataclass(frozen=True)
class Footprint:
    ref: str
    name: str
    x_mm: float
    y_mm: float
    th_pads: int
    smd_pads: int


@dataclass(frozen=True)
class Via:
    x_mm: float
    y_mm: float


@dataclass(frozen=True)
class Track:
    width_mm: float
    net_id: int


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        tag = "PASS" if self.passed else "FAIL"
        return f"  {tag}  {self.name}: {self.detail}"


POWER_VALUE_RE = re.compile(r"buck|ldo|regulator|ap\d|ld\d", re.IGNORECASE)
HEAVY_PART_RE = re.compile(r"mounting|usb|switch|inductor|l_\d|electrolytic", re.IGNORECASE)
CONNECTOR_RE = re.compile(r"usb|conn|header|jst|socket|terminal", re.IGNORECASE)


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _read_bom(path: Path) -> dict[str, str]:
    """Return map of designator -> value from grouped BOM."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            norm = {
                k.strip().lower(): (v.strip() if isinstance(v, str) else "")
                for k, v in row.items()
                if k
            }
            designators = norm.get("designator", norm.get("reference", ""))
            value = norm.get("value", "")
            for d in (d.strip() for d in designators.split(",") if d.strip()):
                out[d] = value
    return out


def _parse_board(  # noqa: C901
    text: str,
) -> tuple[
    list[Footprint],
    list[Via],
    list[Track],
    dict[int, str],
    tuple[float, float, float, float] | None,
]:
    """Parse footprints, vias, tracks, nets and board bounding box from KiCad PCB."""
    data = sexpdata.loads(text)

    footprints: list[Footprint] = []
    vias: list[Via] = []
    tracks: list[Track] = []
    net_map: dict[int, str] = {}
    edge_points: list[tuple[float, float]] = []

    for item in data:
        if not isinstance(item, list):
            continue
        sym = item[0]

        if sym == sexpdata.Symbol("net") and len(item) >= 3:
            net_map[int(item[1])] = str(item[2])
            continue

        if sym == sexpdata.Symbol("footprint"):
            fp_name = str(item[1]) if len(item) > 1 else ""
            at_x = 0.0
            at_y = 0.0
            ref = ""
            th_pads = 0
            smd_pads = 0
            for prop in item:
                if not isinstance(prop, list):
                    continue
                if prop[0] == sexpdata.Symbol("at") and len(prop) >= 3:
                    at_x = float(prop[1])
                    at_y = float(prop[2])
                if (
                    prop[0] == sexpdata.Symbol("fp_text")
                    and len(prop) >= 3
                    and prop[1] == sexpdata.Symbol("reference")
                ):
                    ref = str(prop[2])
                if (
                    prop[0] == sexpdata.Symbol("property")
                    and len(prop) >= 3
                    and str(prop[1]) == "Reference"
                ):
                    ref = str(prop[2])
                if prop[0] == sexpdata.Symbol("pad") and len(prop) >= 3:
                    pad_type = str(prop[2]).lower()
                    if "thru_hole" in pad_type:
                        th_pads += 1
                    elif "smd" in pad_type:
                        smd_pads += 1
            footprints.append(
                Footprint(
                    ref=ref,
                    name=fp_name,
                    x_mm=at_x,
                    y_mm=at_y,
                    th_pads=th_pads,
                    smd_pads=smd_pads,
                )
            )
            continue

        if sym == sexpdata.Symbol("via"):
            x = y = 0.0
            for prop in item:
                if isinstance(prop, list) and prop[0] == sexpdata.Symbol("at") and len(prop) >= 3:
                    x = float(prop[1])
                    y = float(prop[2])
            vias.append(Via(x_mm=x, y_mm=y))
            continue

        if sym == sexpdata.Symbol("segment"):
            width = 0.0
            net_id = 0
            for prop in item:
                if not isinstance(prop, list):
                    continue
                if prop[0] == sexpdata.Symbol("width") and len(prop) >= 2:
                    width = float(prop[1])
                if prop[0] == sexpdata.Symbol("net") and len(prop) >= 2:
                    net_id = int(prop[1])
            tracks.append(Track(width_mm=width, net_id=net_id))
            continue

        if str(sym).startswith("gr_"):
            layer = None
            pts: list[tuple[float, float]] = []
            for prop in item:
                if not isinstance(prop, list):
                    continue
                if prop[0] == sexpdata.Symbol("layer") and len(prop) >= 2:
                    layer = str(prop[1])
                if (
                    prop[0]
                    in (sexpdata.Symbol("start"), sexpdata.Symbol("end"), sexpdata.Symbol("mid"))
                    and len(prop) >= 3
                ):
                    pts.append((float(prop[1]), float(prop[2])))
            if layer == "Edge.Cuts":
                edge_points.extend(pts)

    bbox = None
    if edge_points:
        xs = [p[0] for p in edge_points]
        ys = [p[1] for p in edge_points]
        bbox = (min(xs), min(ys), max(xs), max(ys))

    return footprints, vias, tracks, net_map, bbox


def check_power_track_robustness(tracks: list[Track], net_map: dict[int, str]) -> CheckResult:
    critical = [
        t
        for t in tracks
        if any(k in net_map.get(t.net_id, "").upper() for k in ("VBUS", "5V", "BAT", "RAW"))
    ]
    if not critical:
        return CheckResult(
            name="Thermal power-track width", passed=False, detail="No critical power tracks found"
        )
    min_w = min(t.width_mm for t in critical)
    ok = min_w >= 0.8
    return CheckResult(
        name="Thermal power-track width",
        passed=ok,
        detail=f"Min critical power width {min_w:.3f} mm [target >= 0.800 mm]",
    )


def check_power_ic_thermal_vias(
    footprints: list[Footprint], vias: list[Via], bom_values: dict[str, str]
) -> CheckResult:
    power_refs = [ref for ref, value in bom_values.items() if POWER_VALUE_RE.search(value)]
    power_fps = [fp for fp in footprints if fp.ref in power_refs]
    if not power_fps:
        return CheckResult(
            name="Thermal vias near power ICs",
            passed=False,
            detail="No power IC footprints matched BOM",
        )

    offenders: list[str] = []
    for fp in power_fps:
        near = sum(1 for v in vias if _distance((fp.x_mm, fp.y_mm), (v.x_mm, v.y_mm)) <= 4.0)
        if near < 2:
            offenders.append(f"{fp.ref} ({near} via)")
    if offenders:
        return CheckResult(
            name="Thermal vias near power ICs",
            passed=False,
            detail=f"Insufficient local vias: {', '.join(offenders)} [target >= 2 within 4 mm]",
        )
    return CheckResult(
        name="Thermal vias near power ICs",
        passed=True,
        detail=f"All {len(power_fps)} power ICs have >=2 vias within 4 mm",
    )


def check_crystal_network(
    bom_values: dict[str, str], footprints: list[Footprint], vias: list[Via]
) -> list[CheckResult]:
    crystal_refs = [ref for ref in bom_values if ref.upper().startswith("Y")]
    cap_refs = [
        ref
        for ref, val in bom_values.items()
        if ref.upper().startswith("C") and re.search(r"\d+p", val, re.IGNORECASE)
    ]

    results: list[CheckResult] = []
    results.append(
        CheckResult(
            name="RF/timing crystal presence",
            passed=bool(crystal_refs),
            detail=f"Crystal refs: {', '.join(crystal_refs)}"
            if crystal_refs
            else "No crystal found (Y*)",
        )
    )
    results.append(
        CheckResult(
            name="RF/timing load capacitors",
            passed=len(cap_refs) >= 2,
            detail=f"Found {len(cap_refs)} pF capacitor(s) [target >= 2 near crystal network]",
        )
    )

    crystal_fps = [fp for fp in footprints if fp.ref in crystal_refs]
    if crystal_fps:
        for fp in crystal_fps:
            near = sum(1 for v in vias if _distance((fp.x_mm, fp.y_mm), (v.x_mm, v.y_mm)) <= 6.0)
            results.append(
                CheckResult(
                    name=f"RF crystal ground-via fence ({fp.ref})",
                    passed=near >= 2,
                    detail=f"{near} vias within 6 mm [target >= 2]",
                )
            )
    else:
        results.append(
            CheckResult(
                name="RF crystal ground-via fence",
                passed=False,
                detail="No crystal footprint found for via-fence check",
            )
        )

    return results


def _edge_distance(fp: Footprint, bbox: tuple[float, float, float, float]) -> float:
    min_x, min_y, max_x, max_y = bbox
    return min(fp.x_mm - min_x, max_x - fp.x_mm, fp.y_mm - min_y, max_y - fp.y_mm)


def check_mechanical_vibration(
    footprints: list[Footprint], bbox: tuple[float, float, float, float] | None
) -> list[CheckResult]:
    results: list[CheckResult] = []

    mounting = [fp for fp in footprints if "mountinghole" in fp.name.lower()]
    results.append(
        CheckResult(
            name="Mechanical mounting points",
            passed=len(mounting) >= 4,
            detail=f"{len(mounting)} mounting holes found [target >= 4]",
        )
    )

    if bbox is None:
        results.append(
            CheckResult(
                name="Vibration edge margin",
                passed=False,
                detail="Board Edge.Cuts bounding box unavailable",
            )
        )
        return results

    heavy = [fp for fp in footprints if HEAVY_PART_RE.search(fp.name)]
    if not heavy:
        results.append(
            CheckResult(
                name="Vibration edge margin",
                passed=False,
                detail="No heavy/mechanical footprints identified",
            )
        )
        return results

    min_margin = min(_edge_distance(fp, bbox) for fp in heavy)
    offenders = [fp.ref for fp in heavy if _edge_distance(fp, bbox) < 1.0 and fp.ref]
    results.append(
        CheckResult(
            name="Vibration edge margin",
            passed=min_margin >= 1.0,
            detail=(
                f"Min heavy-part edge margin {min_margin:.2f} mm [target >= 1.00 mm]"
                if min_margin >= 1.0
                else f"Low edge margin parts: {', '.join(offenders[:6])}"
            ),
        )
    )

    return results


def check_connector_anchoring(footprints: list[Footprint], vias: list[Via]) -> CheckResult:
    """Verify connectors have adequate mechanical anchoring features."""
    connectors = [fp for fp in footprints if CONNECTOR_RE.search(fp.name)]
    if not connectors:
        return CheckResult(
            name="Connector anchoring",
            passed=False,
            detail="No connector footprints matched for anchoring check",
        )

    weak: list[str] = []
    for fp in connectors:
        near_vias = sum(1 for v in vias if _distance((fp.x_mm, fp.y_mm), (v.x_mm, v.y_mm)) <= 3.0)
        anchored = fp.th_pads >= 2 or near_vias >= 2
        if not anchored:
            label = fp.ref or fp.name
            weak.append(f"{label} (TH={fp.th_pads}, near_vias={near_vias})")

    if weak:
        return CheckResult(
            name="Connector anchoring",
            passed=False,
            detail=f"{len(weak)} connector(s) weakly anchored: {', '.join(weak[:5])}",
        )

    return CheckResult(
        name="Connector anchoring",
        passed=True,
        detail=f"All {len(connectors)} connector footprint(s) have TH pads or via anchors",
    )


def main(pcb_file: str, bom_file: str) -> int:
    pcb_path = Path(pcb_file)
    bom_path = Path(bom_file)
    if not pcb_path.exists():
        print(f"[ROBUSTNESS] PCB file not found: {pcb_path}")
        return 1
    if not bom_path.exists():
        print(f"[ROBUSTNESS] BOM file not found: {bom_path}")
        return 1

    text = pcb_path.read_text(encoding="utf-8", errors="replace")
    footprints, vias, tracks, net_map, bbox = _parse_board(text)
    bom_values = _read_bom(bom_path)

    results: list[CheckResult] = []
    results.append(check_power_track_robustness(tracks, net_map))
    results.append(check_power_ic_thermal_vias(footprints, vias, bom_values))
    results += check_crystal_network(bom_values, footprints, vias)
    results.append(check_connector_anchoring(footprints, vias))
    results += check_mechanical_vibration(footprints, bbox)

    print(f"[ROBUSTNESS] Results for {pcb_path.name} + {bom_path.name}:")
    for r in results:
        print(r)

    failures = [r for r in results if not r.passed]
    if failures:
        print(f"\n[ROBUSTNESS] {len(failures)} violation(s) detected.")
        return 1

    print(f"\n[ROBUSTNESS] All {len(results)} checks passed.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <file.kicad_pcb> <bom.csv>")
        sys.exit(1)
    sys.exit(main(sys.argv[1], sys.argv[2]))
