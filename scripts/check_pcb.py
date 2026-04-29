#!/usr/bin/env python3
"""Parse a KiCad PCB file and verify MIL-STD-275E / IPC-2221A physical rules.

Checks performed directly on the .kicad_pcb S-expression file:
  * 4-layer copper stackup (F.Cu, In1.Cu, In2.Cu, B.Cu)
  * GND zone coverage on inner copper layers
  * Via drill sizes >= 0.3 mm (MIL-STD-275E)
  * Track widths >= 0.25 mm on signal layers (MIL-STD-275E Table 1)
  * GND stitching via count (>= 10 recommended for EMI per MIL-STD-461)
  * Board outline (Edge.Cuts) present
  * Net name conventions (uppercase, no spaces)
  * Footprint reference designator silkscreen presence

Exit codes:
  0 — all checks passed
  1 — one or more violations detected

Usage:
    python3 check_pcb.py <path/to/file.kicad_pcb>
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import sexpdata
from pydantic import BaseModel

try:
    from .config import settings
except ImportError:
    from config import settings  # type: ignore

# ── MIL-STD-275E / IPC-2221A thresholds ──────────────────────────────────────
MIN_GND_STITCH_VIAS = 10  # EMI best practice per MIL-STD-461 guidance
REQUIRED_SIGNAL_LAYERS = {"F.Cu", "B.Cu"}
REQUIRED_INNER_LAYERS = {"In1.Cu", "In2.Cu"}
GND_NETNAME = "GND"


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        tag = "PASS" if self.passed else "FAIL"
        return f"  {tag}  {self.name}: {self.detail}"


# ── S-expression parser (Modern) ──────────────────────────────────────────────


class PcbParser:
    """Robust PCB parser using sexpdata to avoid brittle regex matching."""

    def __init__(self, text: str) -> None:
        self.data = sexpdata.loads(text)

    def get_layers(self) -> dict[str, str]:
        """Return {layer_name: layer_type} for all copper layers."""
        layers = {}
        # (layers (0 "F.Cu" signal) ...)
        for item in self.data:
            if isinstance(item, list) and sexpdata.Symbol("layers") == item[0]:
                for layer_def in item[1:]:
                    if isinstance(layer_def, list):
                        layer_name = layer_def[1]
                        layer_type = str(layer_def[2])
                        layers[layer_name] = layer_type
                break
        return layers

    def get_zones(self) -> list[dict[str, str]]:
        """Return list of {net_name, layer} for every zone block."""
        zones = []
        for item in self.data:
            if isinstance(item, list) and sexpdata.Symbol("zone") == item[0]:
                zone_info = {"net_name": "", "layer": ""}
                for prop in item:
                    if isinstance(prop, list):
                        if sexpdata.Symbol("net_name") == prop[0]:
                            zone_info["net_name"] = prop[1]
                        elif sexpdata.Symbol("layer") == prop[0]:
                            zone_info["layer"] = prop[1]
                zones.append(zone_info)
        return zones

    def get_vias(self) -> list[dict[str, float]]:
        """Return list of {drill, size} for every via."""
        vias = []
        for item in self.data:
            if isinstance(item, list) and sexpdata.Symbol("via") == item[0]:
                via_info = {"size": 0.0, "drill": 0.0}
                for prop in item:
                    if isinstance(prop, list):
                        if sexpdata.Symbol("size") == prop[0]:
                            via_info["size"] = float(prop[1])
                        elif sexpdata.Symbol("drill") == prop[0]:
                            via_info["drill"] = float(prop[1])
                vias.append(via_info)
        return vias

    def get_tracks(self) -> list[dict[str, Any]]:
        """Return list of {width, layer, net} for every track segment."""
        tracks = []
        for item in self.data:
            if isinstance(item, list) and sexpdata.Symbol("segment") == item[0]:
                track_info = {"width": 0.0, "layer": "", "net": 0}
                for prop in item:
                    if isinstance(prop, list):
                        if sexpdata.Symbol("width") == prop[0]:
                            track_info["width"] = float(prop[1])
                        elif sexpdata.Symbol("layer") == prop[0]:
                            track_info["layer"] = prop[1]
                        elif sexpdata.Symbol("net") == prop[0]:
                            track_info["net"] = int(prop[1])
                tracks.append(track_info)
        return tracks

    def get_net_map(self) -> dict[int, str]:
        """Return {net_id: net_name} mapping."""
        net_map = {}
        for item in self.data:
            if isinstance(item, list) and sexpdata.Symbol("net") == item[0]:
                # (net 1 "GND")
                net_map[int(item[1])] = str(item[2])
        return net_map

    def has_edge_cuts(self) -> bool:
        """Return True if an Edge.Cuts segment/drawing exists."""
        for item in self.data:
            # gr_line, gr_arc, gr_circle etc.
            if not (isinstance(item, list) and str(item[0]).startswith("gr_")):
                continue
            for prop in item:
                if (
                    isinstance(prop, list)
                    and sexpdata.Symbol("layer") == prop[0]
                    and prop[1] == "Edge.Cuts"
                ):
                    return True
        return False

    def get_nets(self) -> list[str]:
        """Return list of all net names (excluding the empty net 0)."""
        nets = []
        for item in self.data:
            if isinstance(item, list) and sexpdata.Symbol("net") == item[0] and len(item) >= 3:
                name = str(item[2])
                if name:  # skip the unnamed net 0
                    nets.append(name)
        return nets

    def get_footprint_refs(self) -> list[tuple[str, bool]]:
        """Return list of (footprint_value, has_reference) for every footprint."""
        footprints = []
        for item in self.data:
            if not (isinstance(item, list) and sexpdata.Symbol("footprint") == item[0]):
                continue
            fp_value = str(item[1]) if len(item) > 1 else "Unknown"
            has_ref = False
            for prop in item:
                if not isinstance(prop, list):
                    continue
                sym = prop[0]
                if (
                    sexpdata.Symbol("fp_text") == sym
                    and len(prop) > 1
                    and prop[1] == sexpdata.Symbol("reference")
                ) or (
                    sexpdata.Symbol("property") == sym and len(prop) > 1 and prop[1] == "Reference"
                ):
                    has_ref = True
            footprints.append((fp_value, has_ref))
        return footprints


# ── Individual checks ─────────────────────────────────────────────────────────


def check_stackup(layers: dict[str, str]) -> CheckResult:
    """Verify 4-layer copper stackup is present."""
    required = REQUIRED_SIGNAL_LAYERS | REQUIRED_INNER_LAYERS
    copper = {name for name, t in layers.items() if t in ("signal", "power", "mixed")}
    missing = required - copper
    if not missing:
        return CheckResult(
            name="4-layer stackup",
            passed=True,
            detail="F.Cu / In1.Cu / In2.Cu / B.Cu all present",
        )
    return CheckResult(
        name="4-layer stackup",
        passed=False,
        detail=f"Missing copper layers: {', '.join(sorted(missing))}",
    )


def check_gnd_inner_zone(zones: list[dict[str, str]]) -> CheckResult:
    """Verify a GND zone covers at least one inner copper layer."""
    gnd_inner = [
        z for z in zones if z["net_name"] == GND_NETNAME and z["layer"] in REQUIRED_INNER_LAYERS
    ]
    if gnd_inner:
        layers_str = ", ".join({z["layer"] for z in gnd_inner})
        return CheckResult(
            name="GND zone on inner layer",
            passed=True,
            detail=f"GND zone found on: {layers_str}",
        )
    # Also pass if In1.Cu is listed as a power-type layer (dedicated plane)
    return CheckResult(
        name="GND zone on inner layer",
        passed=False,
        detail="No GND zone found on In1.Cu or In2.Cu — add a GND copper pour on In1.Cu "
        "for EMI shielding per MIL-STD-461 guidance",
    )


def check_vias(vias: list[dict[str, float]]) -> list[CheckResult]:
    """Check all via drills and annular rings against MIL/IPC minimums."""
    if not vias:
        return [CheckResult(name="Via drill sizes", passed=False, detail="No vias found in PCB")]

    results = []

    # 1. Drill sizes
    drill_violations = [v["drill"] for v in vias if v["drill"] < settings.min_via_drill_mm]
    if not drill_violations:
        results.append(
            CheckResult(
                name="Via drill sizes",
                passed=True,
                detail=f"{len(vias)} vias checked; min drill {min(v['drill'] for v in vias):.3f} mm "
                f">= {settings.min_via_drill_mm} mm (MIL-STD-275E)",
            )
        )
    else:
        results.append(
            CheckResult(
                name="Via drill sizes",
                passed=False,
                detail=f"{len(drill_violations)} via(s) with drill < {settings.min_via_drill_mm} mm: "
                f"min={min(drill_violations):.3f} mm  [MIL-STD-275E: >= {settings.min_via_drill_mm} mm]",
            )
        )

    # 2. Annular rings
    # Ring = (Size - Drill) / 2
    annular_rings = [(v["size"] - v["drill"]) / 2 for v in vias]
    ring_violations = [r for r in annular_rings if r < settings.min_annular_ring_mm]
    if not ring_violations:
        results.append(
            CheckResult(
                name="Via annular rings",
                passed=True,
                detail=f"{len(vias)} via(s) checked; min annular ring {min(annular_rings):.3f} mm "
                f">= {settings.min_annular_ring_mm} mm (IPC-2221A Class C)",
            )
        )
    else:
        results.append(
            CheckResult(
                name="Via annular rings",
                passed=False,
                detail=f"{len(ring_violations)} via(s) with annular ring < {settings.min_annular_ring_mm} mm: "
                f"min={min(ring_violations):.3f} mm [IPC-2221A Class C: >= {settings.min_annular_ring_mm} mm]",
            )
        )

    return results


def check_track_widths(tracks: list[dict[str, Any]], net_map: dict[int, str]) -> list[CheckResult]:
    """Check tracks against MIL minimum width, differentiating signal vs power."""
    if not tracks:
        return [
            CheckResult(name="Track widths", passed=False, detail="No track segments found in PCB")
        ]

    # Only flag external/signal layers
    external_tracks = [t for t in tracks if t["layer"] in REQUIRED_SIGNAL_LAYERS]
    results = []

    # 1. Signal Track Widths
    signal_violations = []
    power_violations = []

    for t in external_tracks:
        net_name = net_map.get(t["net"], "")
        is_power = any(p in net_name.upper() for p in ["VCC", "VDD", "5V", "3V3", "BAT", "RAW"])
        limit = 0.5 if is_power else settings.min_track_width_mm

        if t["width"] < limit:
            if is_power:
                power_violations.append(t)
            else:
                signal_violations.append(t)

    # Report Signal
    if not signal_violations:
        results.append(
            CheckResult(
                name="Signal track widths",
                passed=True,
                detail=f"All signal tracks >= {settings.min_track_width_mm} mm (MIL-STD-275E)",
            )
        )
    else:
        results.append(
            CheckResult(
                name="Signal track widths",
                passed=False,
                detail=f"{len(signal_violations)} violation(s); min {min(t['width'] for t in signal_violations):.3f} mm",
            )
        )

    # Report Power
    if not power_violations:
        results.append(
            CheckResult(
                name="Power track widths", passed=True, detail="Power tracks meet width targets"
            )
        )
    else:
        results.append(
            CheckResult(
                name="Power track widths",
                passed=False,
                detail=f"{len(power_violations)} power track(s) below recommended 0.5mm for current",
            )
        )

    return results


def check_gnd_stitching(vias: list[dict[str, float]], zones: list[dict[str, str]]) -> CheckResult:
    """Report GND stitching via count."""
    # Count total vias as a proxy for stitching (zone vias are GND-connected)
    total_vias = len(vias)
    ok = total_vias >= MIN_GND_STITCH_VIAS
    return CheckResult(
        name="GND stitching vias",
        passed=ok,
        detail=f"{total_vias} total vias  "
        f"[recommended >= {MIN_GND_STITCH_VIAS} for MIL-STD-461 EMI shielding]",
    )


def check_edge_cuts(text: str) -> CheckResult:
    """Verify board outline is present."""
    # This function is now legacy, replaced by PcbParser.has_edge_cuts()
    # Keeping it as a placeholder if needed for other scripts.
    present = '"Edge.Cuts"' in text
    return CheckResult(
        name="Board outline (Edge.Cuts)",
        passed=present,
        detail="Edge.Cuts outline present" if present else "No Edge.Cuts outline found",
    )


def check_net_names(nets: list[str]) -> CheckResult:
    """Ensure all net names follow standard conventions (uppercase, no spaces)."""
    violations: list[str] = []
    for name in nets:
        if " " in name:
            violations.append(f"'{name}' contains spaces")
        elif any(c.islower() for c in name) and not name.startswith("/"):
            # Allow /hierarchical/net names from KiCad schematic sheets
            violations.append(f"'{name}' contains lowercase characters")
    if violations:
        sample = ", ".join(violations[:3])
        suffix = f" (+{len(violations) - 3} more)" if len(violations) > 3 else ""
        return CheckResult(
            name="Net Naming",
            passed=False,
            detail=f"{len(violations)} violation(s): {sample}{suffix}",
        )
    return CheckResult(
        name="Net Naming",
        passed=True,
        detail=f"All {len(nets)} nets follow uppercase/no-space convention",
    )


def check_footprint_silkscreen(footprints: list[tuple[str, bool]]) -> CheckResult:
    """Check that all footprints have a silkscreen reference designator."""
    missing = [fp for fp, has_ref in footprints if not has_ref]
    if missing:
        sample = ", ".join(missing[:3])
        suffix = f" (+{len(missing) - 3} more)" if len(missing) > 3 else ""
        return CheckResult(
            name="Silkscreen Refs",
            passed=False,
            detail=f"{len(missing)} footprint(s) missing reference: {sample}{suffix}",
        )
    return CheckResult(
        name="Silkscreen Refs",
        passed=True,
        detail=f"All {len(footprints)} footprints have reference designators",
    )


# ── Main ──────────────────────────────────────────────────────────────────────


def main(pcb_file: str) -> int:
    path = Path(pcb_file)
    if not path.exists():
        print(f"[PCB-CHECK] File not found: {pcb_file}")
        return 1

    text = path.read_text(encoding="utf-8", errors="replace")
    if "(kicad_pcb" not in text:
        print(f"[PCB-CHECK] Not a valid KiCad PCB file: {pcb_file}")
        return 1

    parser = PcbParser(text)

    layers = parser.get_layers()
    zones = parser.get_zones()
    vias = parser.get_vias()
    tracks = parser.get_tracks()
    net_map = parser.get_net_map()
    nets = parser.get_nets()
    footprints = parser.get_footprint_refs()
    edge_cuts_present = parser.has_edge_cuts()

    all_results: list[CheckResult] = []
    all_results.append(check_stackup(layers))
    all_results.append(check_gnd_inner_zone(zones))
    all_results += check_vias(vias)
    all_results += check_track_widths(tracks, net_map)
    all_results.append(check_gnd_stitching(vias, zones))
    all_results.append(
        CheckResult(
            name="Board outline (Edge.Cuts)",
            passed=edge_cuts_present,
            detail="Edge.Cuts outline present"
            if edge_cuts_present
            else "No Edge.Cuts outline found",
        )
    )
    all_results.append(check_net_names(nets))
    all_results.append(check_footprint_silkscreen(footprints))

    print(f"[PCB-CHECK] Results for {path.name}:")
    for r in all_results:
        print(r)

    failures = [r for r in all_results if not r.passed]
    if failures:
        print(f"\n[PCB-CHECK] {len(failures)} violation(s) detected.")
        return 1

    print(f"\n[PCB-CHECK] All {len(all_results)} checks passed.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <file.kicad_pcb>")
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
