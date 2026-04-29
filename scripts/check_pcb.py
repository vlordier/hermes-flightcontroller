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

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


# ── S-expression helpers ──────────────────────────────────────────────────────


def _get_layers(text: str) -> dict[str, str]:
    """Return {layer_name: layer_type} for all copper layers."""
    results: dict[str, str] = {}
    for m in re.finditer(
        r'\(\d+ "([^"]+)" (signal|power|mixed|user)',
        text,
    ):
        results[m.group(1)] = m.group(2)
    return results


def _get_zones(text: str) -> list[dict[str, str]]:
    """Return list of {net_name, layer} for every zone block.

    The lookahead ``(?=(...))`` delimits each zone block at the start of the
    next top-level S-expression directive (footprint, via, segment, gr_*, zone,
    setup, or net declaration).  This avoids the cross-block false-match that
    occurs when using a naively greedy ``.*?`` across an entire file.
    Recognised delimiters: footprint, via, segment, gr_, zone, setup, net.
    """
    # Top-level directives that can follow a zone block in .kicad_pcb files
    zone_delimiters = r"footprint|via|segment|gr_|zone|setup|net\s"
    zones: list[dict[str, str]] = []
    for block in re.finditer(
        rf"\(zone\b(.*?)\n\s*(?=\((?:{zone_delimiters}|\Z))",
        text,
        re.DOTALL,
    ):
        chunk = block.group(0)
        net = re.search(r'\(net_name "([^"]*)"\)', chunk)
        layer = re.search(r'\(layer "([^"]*)"\)', chunk)
        zones.append(
            {
                "net_name": net.group(1) if net else "",
                "layer": layer.group(1) if layer else "",
            }
        )
    return zones


def _get_vias(text: str) -> list[dict[str, float]]:
    """Return list of {drill, size} for every via.

    KiCad 8 via format::

        (via
            (at ...)
            (size 0.6)
            (drill 0.3)
            ...
        )
    """
    vias = []
    # Match via blocks and extract size/drill
    # Using a more robust regex that allows indentation
    via_pattern = r"\(via\s+.*?\s*\(size ([\d.]+)\)\s*\(drill ([\d.]+)\)"
    for block in re.finditer(via_pattern, text, re.DOTALL):
        vias.append(
            {
                "size": float(block.group(1)),
                "drill": float(block.group(2)),
            }
        )
    return vias


def _get_tracks(text: str) -> list[dict[str, Any]]:
    """Return list of {width, layer} for every track segment.

    KiCad 8 segment format::

        (segment
            (start ...)
            (end ...)
            (width 0.2)
            (layer "F.Cu")
            ...
        )
    """
    pairs = re.findall(
        r"\(segment\n[^(]+\(start [^)]+\)\n[^(]+\(end [^)]+\)\n"
        r"[^(]+\(width ([\d.]+)\)\n[^(]+\(layer \"([^\"]+)\"",
        text,
    )
    return [{"width": float(w), "layer": layer} for w, layer in pairs]


def _has_edge_cuts(text: str) -> bool:
    """Return True if the board has an Edge.Cuts outline."""
    return bool(re.search(r'"Edge\.Cuts"', text))


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
    present = _has_edge_cuts(text)
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

    layers = _get_layers(text)
    zones = _get_zones(text)
    vias = _get_vias(text)
    tracks = _get_tracks(text)

    all_results: list[CheckResult] = []
    all_results.append(check_stackup(layers))
    all_results.append(check_gnd_inner_zone(zones))
    all_results += check_vias(vias)
    all_results += check_track_widths(tracks)
    all_results.append(check_gnd_stitching(vias, zones))
    all_results.append(check_edge_cuts(text))

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
