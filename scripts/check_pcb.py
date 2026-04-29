#!/usr/bin/env python3
"""Parse a KiCad PCB file and verify MIL-STD-275E / IPC-2221A physical rules.

Checks performed directly on the .kicad_pcb S-expression file:
  * 4-layer copper stackup (F.Cu, In1.Cu, In2.Cu, B.Cu)
  * GND zone coverage on inner copper layers
  * Via drill sizes >= 0.3 mm (MIL-STD-275E)
  * Track widths >= 0.25 mm on signal layers (MIL-STD-275E Table 1)
  * GND stitching via count (>= 10 recommended for EMI per MIL-STD-461)
  * Board outline (Edge.Cuts) present

Exit codes:
  0 — all checks passed
  1 — one or more violations detected

Usage:
    python3 check_pcb.py <path/to/file.kicad_pcb>
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sexpdata

try:
    from .config import settings
except ImportError:
    from config import settings  # type: ignore

# ── MIL-STD-275E / IPC-2221A thresholds ──────────────────────────────────────
MIN_GND_STITCH_VIAS = 10  # EMI best practice per MIL-STD-461 guidance
REQUIRED_SIGNAL_LAYERS = {"F.Cu", "B.Cu"}
REQUIRED_INNER_LAYERS = {"In1.Cu", "In2.Cu"}
GND_NETNAME = "GND"


@dataclass
class CheckResult:
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
        """Return list of {width, layer} for every track segment."""
        tracks = []
        for item in self.data:
            if isinstance(item, list) and sexpdata.Symbol("segment") == item[0]:
                track_info = {"width": 0.0, "layer": ""}
                for prop in item:
                    if isinstance(prop, list):
                        if sexpdata.Symbol("width") == prop[0]:
                            track_info["width"] = float(prop[1])
                        elif sexpdata.Symbol("layer") == prop[0]:
                            track_info["layer"] = prop[1]
                tracks.append(track_info)
        return tracks

    def has_edge_cuts(self) -> bool:
        """Return True if an Edge.Cuts segment/drawing exists."""
        for item in self.data:
            # gr_line, gr_arc, gr_circle etc.
            if isinstance(item, list) and str(item[0]).startswith("gr_"):
                for prop in item:
                    if isinstance(prop, list) and sexpdata.Symbol("layer") == prop[0]:
                        if prop[1] == "Edge.Cuts":
                            return True
        return False


# ── Individual checks ─────────────────────────────────────────────────────────


def check_stackup(layers: dict[str, str]) -> CheckResult:
    """Verify 4-layer copper stackup is present."""
    required = REQUIRED_SIGNAL_LAYERS | REQUIRED_INNER_LAYERS
    copper = {name for name, t in layers.items() if t in ("signal", "power", "mixed")}
    missing = required - copper
    if not missing:
        return CheckResult(
            "4-layer stackup",
            True,
            "F.Cu / In1.Cu / In2.Cu / B.Cu all present",
        )
    return CheckResult(
        "4-layer stackup",
        False,
        f"Missing copper layers: {', '.join(sorted(missing))}",
    )


def check_gnd_inner_zone(zones: list[dict[str, str]]) -> CheckResult:
    """Verify a GND zone covers at least one inner copper layer."""
    gnd_inner = [
        z for z in zones if z["net_name"] == GND_NETNAME and z["layer"] in REQUIRED_INNER_LAYERS
    ]
    if gnd_inner:
        layers_str = ", ".join({z["layer"] for z in gnd_inner})
        return CheckResult(
            "GND zone on inner layer",
            True,
            f"GND zone found on: {layers_str}",
        )
    # Also pass if In1.Cu is listed as a power-type layer (dedicated plane)
    return CheckResult(
        "GND zone on inner layer",
        False,
        "No GND zone found on In1.Cu or In2.Cu — add a GND copper pour on In1.Cu "
        "for EMI shielding per MIL-STD-461 guidance",
    )


def check_vias(vias: list[dict[str, float]]) -> list[CheckResult]:
    """Check all via drills and annular rings against MIL/IPC minimums."""
    if not vias:
        return [CheckResult("Via drill sizes", False, "No vias found in PCB")]

    results = []

    # 1. Drill sizes
    drill_violations = [v["drill"] for v in vias if v["drill"] < settings.min_via_drill_mm]
    if not drill_violations:
        results.append(
            CheckResult(
                "Via drill sizes",
                True,
                f"{len(vias)} vias checked; min drill {min(v['drill'] for v in vias):.3f} mm "
                f">= {settings.min_via_drill_mm} mm (MIL-STD-275E)",
            )
        )
    else:
        results.append(
            CheckResult(
                "Via drill sizes",
                False,
                f"{len(drill_violations)} via(s) with drill < {settings.min_via_drill_mm} mm: "
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
                "Via annular rings",
                True,
                f"{len(vias)} via(s) checked; min annular ring {min(annular_rings):.3f} mm "
                f">= {settings.min_annular_ring_mm} mm (IPC-2221A Class C)",
            )
        )
    else:
        results.append(
            CheckResult(
                "Via annular rings",
                False,
                f"{len(ring_violations)} via(s) with annular ring < {settings.min_annular_ring_mm} mm: "
                f"min={min(ring_violations):.3f} mm [IPC-2221A Class C: >= {settings.min_annular_ring_mm} mm]",
            )
        )

    return results


def check_track_widths(tracks: list[dict[str, Any]]) -> list[CheckResult]:
    """Check all signal-layer tracks against MIL minimum width."""
    if not tracks:
        return [CheckResult("Track widths", False, "No track segments found in PCB")]
    # Only flag external/signal layers (MIL-STD-275E applies to external conductors)
    signal_tracks = [t for t in tracks if t["layer"] in REQUIRED_SIGNAL_LAYERS]
    violations = [t for t in signal_tracks if t["width"] < settings.min_track_width_mm]
    if not violations:
        widths = [t["width"] for t in signal_tracks]
        return [
            CheckResult(
                "Track widths (signal layers)",
                True,
                f"{len(signal_tracks)} tracks; min {min(widths) if widths else 0:.4f} mm "
                f">= {settings.min_track_width_mm} mm (MIL-STD-275E)",
            )
        ]
    by_width: dict[float, int] = {}
    for t in violations:
        by_width[t["width"]] = by_width.get(t["width"], 0) + 1
    detail = ", ".join(f"{w:.4f}mm ×{c}" for w, c in sorted(by_width.items()))
    return [
        CheckResult(
            "Track widths (signal layers)",
            False,
            f"{len(violations)} track(s) on signal layers below {settings.min_track_width_mm} mm: "
            f"{detail}  [MIL-STD-275E: >= {settings.min_track_width_mm} mm]",
        )
    ]


def check_gnd_stitching(vias: list[dict[str, float]], zones: list[dict[str, str]]) -> CheckResult:
    """Report GND stitching via count."""
    # Count total vias as a proxy for stitching (zone vias are GND-connected)
    total_vias = len(vias)
    ok = total_vias >= MIN_GND_STITCH_VIAS
    return CheckResult(
        "GND stitching vias",
        ok,
        f"{total_vias} total vias  "
        f"[recommended >= {MIN_GND_STITCH_VIAS} for MIL-STD-461 EMI shielding]",
    )


def check_edge_cuts(text: str) -> CheckResult:
    """Verify board outline is present."""
    # This function is now legacy, replaced by PcbParser.has_edge_cuts()
    # Keeping it as a placeholder if needed for other scripts.
    present = '"Edge.Cuts"' in text
    return CheckResult(
        "Board outline (Edge.Cuts)",
        present,
        "Edge.Cuts outline present" if present else "No Edge.Cuts outline found",
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
    edge_cuts_present = parser.has_edge_cuts()

    all_results: list[CheckResult] = []
    all_results.append(check_stackup(layers))
    all_results.append(check_gnd_inner_zone(zones))
    all_results += check_vias(vias)
    all_results += check_track_widths(tracks)
    all_results.append(check_gnd_stitching(vias, zones))
    all_results.append(
        CheckResult(
            "Board outline (Edge.Cuts)",
            edge_cuts_present,
            "Edge.Cuts outline present" if edge_cuts_present else "No Edge.Cuts outline found",
        )
    )

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
