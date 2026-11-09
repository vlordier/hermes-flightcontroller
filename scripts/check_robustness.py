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
    start: tuple[float, float] = (0.0, 0.0)
    end: tuple[float, float] = (0.0, 0.0)

    @property
    def length_mm(self) -> float:
        """Euclidean length of this segment in mm."""
        return math.hypot(self.end[0] - self.start[0], self.end[1] - self.start[1])


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

# Per-net current budgets for current-density checks (net keyword → amps).
# Based on worst-case MCU + sensor loads at each rail.
NET_CURRENT_BUDGET_A: dict[str, float] = {
    "VBUS": 2.0,  # USB upstream VBUS; buck input up to 2 A
    "5V": 1.5,  # AP63203 output feeds MCU + sensors at up to 1.5 A
    "3V3": 1.0,  # 3.3 V digital rail; STM32 + IMU + other peripherals
    "BAT": 2.0,  # Battery input; same headroom as VBUS
    "RAW": 2.0,  # Unregulated input; full input current
}

# Minimum track width (mm) required to carry the rated current at ≤10 °C rise
# on an external copper layer (1 oz / 35 µm).  Values derived from IPC-2221A
# external-conductor chart: I = 0.048 · ΔT^0.44 · W^0.725  (W in mm).
NET_MIN_WIDTH_FOR_CURRENT_MM: dict[str, float] = {
    "VBUS": 1.0,
    "5V": 0.8,
    "3V3": 0.5,
    "BAT": 1.0,
    "RAW": 1.0,
}

# USB high-speed impedance-controlled differential pair net-name keywords.
USB_DP_KEYWORDS = ("D+", "DP", "USB_DP", "USBDP", "USB+")
USB_DM_KEYWORDS = ("D-", "DM", "USB_DM", "USBDM", "USB-")

# Target width range for 90 Ω USB differential pair on standard 4-layer FR4
# (1.6 mm, 1 oz copper, internal reference plane).  Typical design target:
# ~0.17 mm ± 30 % manufacturing tolerance.
USB_DIFF_WIDTH_MIN_MM = 0.10
USB_DIFF_WIDTH_MAX_MM = 0.30

# Maximum allowed length mismatch between USB D+ and D- (IPC-2141A guidance).
# USB 2.0 FS/HS differential skew budget ≈ 200 ps; at 180 mm/ns in FR4 that
# is ~36 mm, but practical routing targets keep mismatch under 2.5 mm.
USB_DIFF_SKEW_LIMIT_MM = 2.5

# Minimum number of test-point (TP*) footprints for adequate probeability.
MIN_TEST_POINTS = 2


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
            sx = sy = ex = ey = 0.0
            for prop in item:
                if not isinstance(prop, list):
                    continue
                if prop[0] == sexpdata.Symbol("width") and len(prop) >= 2:
                    width = float(prop[1])
                if prop[0] == sexpdata.Symbol("net") and len(prop) >= 2:
                    net_id = int(prop[1])
                if prop[0] == sexpdata.Symbol("start") and len(prop) >= 3:
                    sx, sy = float(prop[1]), float(prop[2])
                if prop[0] == sexpdata.Symbol("end") and len(prop) >= 3:
                    ex, ey = float(prop[1]), float(prop[2])
            tracks.append(Track(width_mm=width, net_id=net_id, start=(sx, sy), end=(ex, ey)))
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


def check_current_density(tracks: list[Track], net_map: dict[int, str]) -> list[CheckResult]:
    """Check that power net tracks are wide enough for their rated current.

    Uses IPC-2221A external-conductor current capacity as a gate:
    minimum track widths are pre-computed for ≤10 °C temperature rise at the
    rated current for each power net.
    """
    results: list[CheckResult] = []
    for net_key, required_width in NET_MIN_WIDTH_FOR_CURRENT_MM.items():
        rated_a = NET_CURRENT_BUDGET_A.get(net_key, 1.0)
        net_tracks = [t for t in tracks if net_key.upper() in net_map.get(t.net_id, "").upper()]
        if not net_tracks:
            # Net not present on this board — skip rather than flagging.
            continue
        min_w = min(t.width_mm for t in net_tracks)
        passed = min_w >= required_width
        results.append(
            CheckResult(
                name=f"Current density — {net_key}",
                passed=passed,
                detail=(
                    f"Min track width {min_w:.3f} mm >= {required_width:.3f} mm "
                    f"(IPC-2221A, {rated_a:.1f} A, ≤10 °C rise)"
                    if passed
                    else f"Min track width {min_w:.3f} mm < {required_width:.3f} mm "
                    f"(IPC-2221A, {rated_a:.1f} A, ≤10 °C rise) — widen track"
                ),
            )
        )
    if not results:
        results.append(
            CheckResult(
                name="Current density",
                passed=False,
                detail="No power net tracks found for current-density check",
            )
        )
    return results


def check_decoupling_proximity(
    footprints: list[Footprint],
    bom_values: dict[str, str],
    *,
    radius_mm: float = 5.0,
) -> list[CheckResult]:
    """Verify each IC has at least one bypass capacitor within *radius_mm*.

    ICs are footprints whose reference starts with 'U'.  Capacitors are
    footprints whose reference starts with 'C' and whose BOM value contains a
    capacitance unit (n/µ/p followed by F, or plain numeric pF/nF/µF).
    """
    ic_fps = [fp for fp in footprints if fp.ref.upper().startswith("U")]
    cap_fps = [
        fp
        for fp in footprints
        if fp.ref.upper().startswith("C")
        and re.search(r"\d+\s*[nupµ]?[fF]", bom_values.get(fp.ref, ""))
    ]

    if not ic_fps:
        return [
            CheckResult(
                name="Decoupling proximity",
                passed=False,
                detail="No IC footprints (U*) found for decoupling check",
            )
        ]

    results: list[CheckResult] = []
    for ic in ic_fps:
        nearby_caps = [
            c for c in cap_fps if _distance((ic.x_mm, ic.y_mm), (c.x_mm, c.y_mm)) <= radius_mm
        ]
        results.append(
            CheckResult(
                name=f"Decoupling proximity — {ic.ref}",
                passed=len(nearby_caps) >= 1,
                detail=(
                    f"{len(nearby_caps)} bypass cap(s) within {radius_mm:.0f} mm"
                    if nearby_caps
                    else f"No bypass capacitor within {radius_mm:.0f} mm — add local decoupling"
                ),
            )
        )
    return results


def check_diff_pair_length_matching(
    tracks: list[Track], net_map: dict[int, str]
) -> list[CheckResult]:
    """Check that USB D+ and D- route lengths are within the skew budget.

    Sums track segment lengths per differential signal and flags pairs whose
    mismatch exceeds USB_DIFF_SKEW_LIMIT_MM.  Length matching is critical for
    USB signal integrity; IPC-2141A recommends < 5 mil (0.13 mm) per inch of
    trace, but practical 2.5 mm total-mismatch targets are industry standard.
    """

    def _net_length(keywords: tuple[str, ...]) -> float | None:
        segs = [
            t
            for t in tracks
            if any(kw.upper() in net_map.get(t.net_id, "").upper() for kw in keywords)
        ]
        if not segs:
            return None
        return sum(t.length_mm for t in segs)

    len_dp = _net_length(USB_DP_KEYWORDS)
    len_dm = _net_length(USB_DM_KEYWORDS)

    if len_dp is None or len_dm is None:
        return [
            CheckResult(
                name="USB diff-pair length matching",
                passed=False,
                detail="One or both USB D+/D- nets not found — check net naming",
            )
        ]

    skew = abs(len_dp - len_dm)
    passed = skew <= USB_DIFF_SKEW_LIMIT_MM
    return [
        CheckResult(
            name="USB diff-pair length matching",
            passed=passed,
            detail=(
                f"D+={len_dp:.2f} mm  D-={len_dm:.2f} mm  skew={skew:.2f} mm "
                f"[limit {USB_DIFF_SKEW_LIMIT_MM} mm]"
                if passed
                else f"Skew {skew:.2f} mm exceeds {USB_DIFF_SKEW_LIMIT_MM} mm limit "
                f"(D+={len_dp:.2f} mm, D-={len_dm:.2f} mm) — add length-matching serpentines"
            ),
        )
    ]


def check_test_point_coverage(footprints: list[Footprint]) -> CheckResult:
    """Verify adequate test-point (TP*) footprints exist for probeability.

    Test points on key power and signal nets enable in-circuit debugging and
    production testing.  MIL-STD-2000A and IPC-7711/7721 recommend accessible
    test pads for all critical nets.
    """
    tp_fps = [fp for fp in footprints if fp.ref.upper().startswith("TP")]
    count = len(tp_fps)
    passed = count >= MIN_TEST_POINTS
    return CheckResult(
        name="Test-point coverage",
        passed=passed,
        detail=(
            f"{count} test-point footprint(s) found [target >= {MIN_TEST_POINTS}]"
            if passed
            else f"Only {count} test-point footprint(s) found — add TP* pads on key nets "
            f"(power rails, UART, SWD) [target >= {MIN_TEST_POINTS}]"
        ),
    )


def check_impedance_controlled_traces(
    tracks: list[Track], net_map: dict[int, str]
) -> list[CheckResult]:
    """Check USB D+/D- differential pair tracks for controlled-impedance width.

    USB 2.0 full-speed / high-speed differential pairs target 90 Ω.  On a
    standard 4-layer FR4 board (1.6 mm, 1 oz copper, internal reference plane)
    this corresponds to roughly 0.17 mm single-ended width.  The checker flags
    tracks outside the tolerance window USB_DIFF_WIDTH_MIN_MM–USB_DIFF_WIDTH_MAX_MM.
    """
    results: list[CheckResult] = []

    def _check_pair(keywords: tuple[str, ...], label: str) -> None:
        pair_tracks = [
            t
            for t in tracks
            if any(kw.upper() in net_map.get(t.net_id, "").upper() for kw in keywords)
        ]
        if not pair_tracks:
            return  # net not present; skip rather than raising
        violations = [
            t
            for t in pair_tracks
            if not (USB_DIFF_WIDTH_MIN_MM <= t.width_mm <= USB_DIFF_WIDTH_MAX_MM)
        ]
        passed = not violations
        if passed:
            widths = [t.width_mm for t in pair_tracks]
            detail = (
                f"All {len(pair_tracks)} segment(s) within "
                f"{USB_DIFF_WIDTH_MIN_MM:.2f}–{USB_DIFF_WIDTH_MAX_MM:.2f} mm "
                f"(90 Ω diff target); min={min(widths):.3f} mm"
            )
        else:
            bad_widths = sorted({t.width_mm for t in violations})
            detail = (
                f"{len(violations)} segment(s) outside 90 Ω target window "
                f"[{USB_DIFF_WIDTH_MIN_MM:.2f}–{USB_DIFF_WIDTH_MAX_MM:.2f} mm]: "
                f"{', '.join(f'{w:.3f}' for w in bad_widths[:5])}"
            )
        results.append(
            CheckResult(
                name=f"Impedance control — USB {label}",
                passed=passed,
                detail=detail,
            )
        )

    _check_pair(USB_DP_KEYWORDS, "D+")
    _check_pair(USB_DM_KEYWORDS, "D-")

    if not results:
        results.append(
            CheckResult(
                name="Impedance control — USB differential pair",
                passed=False,
                detail="No USB D+/D- nets found — check net naming (expected D+/D-, DP/DM, USB_DP/USB_DM)",
            )
        )
    return results


# Keywords identifying debug/programming port nets (SWD, JTAG, UART console).
_DEBUG_NET_KEYWORDS = (
    "SWDIO",
    "SWDCLK",
    "SWO",
    "SWCLK",  # ARM SWD
    "TCK",
    "TDI",
    "TDO",
    "TMS",  # JTAG
    "UART",
    "TX",
    "RX",
    "USART",  # Serial console
)

# Critical control/reset net keywords.
_RESET_NET_KEYWORDS = ("NRST", "RESET", "/RST", "RST")

# Minimum required debug nets present (at least SWDIO + SWDCLK or TCK+TDI).
_MIN_DEBUG_NETS = 2


def check_debug_port_nets(net_map: dict[int, str]) -> list[CheckResult]:
    """Verify that SWD/JTAG debug port nets are present on the board.

    ARM Cortex-M MCUs require SWDIO and SWDCLK (or full JTAG TDI/TDO/TCK/TMS)
    for programming and in-circuit debugging.  Absence from the net list
    indicates missing debug connector routing — a common oversight that prevents
    firmware updates without a bootloader and complicates production testing.
    """
    all_net_names = {n.upper() for n in net_map.values()}

    debug_found = [kw for kw in _DEBUG_NET_KEYWORDS if any(kw in n for n in all_net_names)]
    debug_ok = len(debug_found) >= _MIN_DEBUG_NETS

    results: list[CheckResult] = [
        CheckResult(
            name="Debug port nets (SWD/JTAG)",
            passed=debug_ok,
            detail=(
                f"Debug nets present: {', '.join(debug_found)}"
                if debug_ok
                else f"Only {len(debug_found)} debug net(s) found [{', '.join(debug_found) or 'none'}] — "
                f"add SWD connector (SWDIO + SWDCLK minimum) [target >= {_MIN_DEBUG_NETS}]"
            ),
        )
    ]

    reset_found = [kw for kw in _RESET_NET_KEYWORDS if any(kw in n for n in all_net_names)]
    results.append(
        CheckResult(
            name="Reset net presence",
            passed=bool(reset_found),
            detail=(
                f"Reset net found: {', '.join(reset_found)}"
                if reset_found
                else "No NRST/RESET net found — MCU reset line may be floating"
            ),
        )
    )

    return results


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
    results += check_current_density(tracks, net_map)
    results += check_decoupling_proximity(footprints, bom_values)
    results += check_impedance_controlled_traces(tracks, net_map)
    results += check_diff_pair_length_matching(tracks, net_map)
    results.append(check_test_point_coverage(footprints))
    results += check_debug_port_nets(net_map)

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
