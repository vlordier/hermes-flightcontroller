# MIL-Grade Compliance Report -- Hermes Flight Controller

Generated: 2026-04-29 16:07 UTC

## Summary

| Status | Count |
|--------|-------|
| OK PASS    | 8 |
| FAIL       | 5 |
| WARN       | 0 |
| PENDING    | 10 |
| **Total**  | **23** |

## Failures (must be resolved before release)

- **MIL-STD-461G RE101** -- Radiated emissions -- magnetic field, 30 Hz-100 kHz  (Evidence: [PCB-CHECK] 3 violation(s) detected.)
- **MIL-STD-275E Trace Width** -- Min trace 0.25 mm signal; power traces >= 0.8 mm  (Evidence: [PCB-CHECK] 3 violation(s) detected.)
- **MIL-STD-275E Via Drill** -- Via drill >= 0.3 mm (plated-through-holes)  (Evidence: [PCB-CHECK] 3 violation(s) detected.)
- **Net Naming Hygiene** -- Nets follow uppercase convention and naming hygiene  (Evidence:   FAIL  Net Naming: 74 violations found: Net 'VDDIO_1v8' contains lowercase characters, Net 'Net-(C15-Pad1)' contains lowercase characters, Net 'unconnected-(U1-PB15-Pad36)' contains lowercase characters...)
- **MIL-PRF-55110 Ground Plane** -- Continuous GND plane on inner layer; < 5 % voiding  (Evidence: No GND zone on In1.Cu or In2.Cu -- add a GND copper pour on In1.Cu for EMI shielding (MIL-STD-461G / MIL-PRF-55110))

## Detailed Checklist

| # | Section | Requirement | Verification Method | Status | Evidence |
|---|---------|-------------|---------------------|--------|----------|
| 1 | MIL-STD-461G RE102 | Radiated emissions (electric field) within limits at 1 m, 30 MHz-18 GHz | OpenEMS FDTD simulation (scripts/run_openems.py --check-mil461) | 🔲 PENDING | EM simulation not run |
| 2 | MIL-STD-461G CS101 | Conducted susceptibility -- power leads, 30 Hz-150 kHz | ngspice transient simulation (check_mil_spice.py) | 🔲 PENDING | sim_output.txt not found |
| 3 | MIL-STD-461G CS116 | Conducted susceptibility -- damped sinusoids on power/signal | ngspice CS116 transient + check_mil_spice.py overshoot check | 🔲 PENDING | sim_output.txt not found |
| 4 | MIL-STD-461G RE101 | Radiated emissions -- magnetic field, 30 Hz-100 kHz | check_pcb.py: ground plane and stitching via verification | ❌ FAIL | [PCB-CHECK] 3 violation(s) detected. |
| 5 | MIL-STD-461G RS103 | Radiated susceptibility -- electric field, 10 kHz-18 GHz | OpenEMS susceptibility sweep (scripts/run_openems.py) | 🔲 PENDING | EM simulation not run |
| 6 | MIL-STD-704F sec 4.4 | 5 V steady-state within +-5 % (4.75-5.25 V) | ngspice .op (check_mil_spice.py DC check) | 🔲 PENDING | sim_output.txt not found |
| 7 | MIL-STD-704F sec 4.5 | Transient overshoot < 500 mV above nominal, recovery < 50 ms | ngspice .tran (check_mil_spice.py transient check) | 🔲 PENDING | sim_output.txt not found |
| 8 | MIL-STD-704F Ripple | Ripple < 50 mV pk-pk on 5 V; < 30 mV on 3.3 V | ngspice .meas pp_5v/pp_3v3 (check_mil_spice.py ripple check) | 🔲 PENDING | sim_output.txt not found |
| 9 | MIL-STD-275E Clearance | Min trace clearance 0.25 mm (external), per Table 1 | KiCad DRC with mil_rules.kicad_dru + check_pcb.py | 🔲 PENDING | drc_report.txt not found |
| 10 | MIL-STD-275E Trace Width | Min trace 0.25 mm signal; power traces >= 0.8 mm | check_pcb.py track-width scan of .kicad_pcb | ❌ FAIL | [PCB-CHECK] 3 violation(s) detected. |
| 11 | MIL-STD-275E Edge Clearance | Copper >= 1.25 mm from board edge | KiCad DRC with mil_rules.kicad_dru | 🔲 PENDING | drc_report.txt not found |
| 12 | MIL-STD-275E Via Drill | Via drill >= 0.3 mm (plated-through-holes) | check_pcb.py via-drill scan of .kicad_pcb | ❌ FAIL | [PCB-CHECK] 3 violation(s) detected. |
| 13 | Annular Ring Integrity | Via annular rings >= 0.15 mm | check_pcb_integrity.py annular ring scan | ✅ PASS |   PASS  Annular Rings: All via annular rings >= 0.15mm |
| 14 | Net Naming Hygiene | Nets follow uppercase convention and naming hygiene | check_pcb_integrity.py net convention check | ❌ FAIL |   FAIL  Net Naming: 74 violations found: Net 'VDDIO_1v8' contains lowercase characters, Net 'Net-(C15-Pad1)' contains lowercase characters, Net 'unconnected-(U1-PB15-Pad36)' contains lowercase characters... |
| 15 | MIL-STD-275E Custom Rules | mil_rules.kicad_dru present and applied to PCB | File presence check | ✅ PASS | mil_rules.kicad_dru present |
| 16 | IPC-2221A ERC | No unconnected pins, no electrical rule violations | KiCad ERC (kicad-cli sch erc) | 🔲 PENDING | erc_report.txt not found |
| 17 | MIL-PRF-55110 Stackup | 4-layer stackup: F.Cu / In1.Cu(GND) / In2.Cu(PWR) / B.Cu | check_pcb.py layer enumeration from .kicad_pcb | ✅ PASS | 4-layer stackup: F.Cu / In1.Cu(GND) / In2.Cu(PWR) / B.Cu |
| 18 | MIL-PRF-55110 Ground Plane | Continuous GND plane on inner layer; < 5 % voiding | check_pcb.py zone-net scan of .kicad_pcb | ❌ FAIL | No GND zone on In1.Cu or In2.Cu -- add a GND copper pour on In1.Cu for EMI shielding (MIL-STD-461G / MIL-PRF-55110) |
| 19 | Component LCSC Coverage | All BOM components have LCSC part numbers for traceability | check_bom.py LCSC field scan of bom.csv | ✅ PASS | [BOM-CHECK] All 6 checks passed. |
| 20 | TVS / ESD Protection | TVS diodes on all external connectors (USB, GPIO headers) | check_bom.py ESD/TVS device scan of bom.csv | ✅ PASS | [BOM-CHECK] All 6 checks passed. |
| 21 | Power Management ICs | Buck converter and LDO regulators present in BOM | check_bom.py power-IC scan of bom.csv | ✅ PASS | [BOM-CHECK] All 6 checks passed. |
| 22 | Revision Control | Git SHA / tag embedded in build; clean working tree | git describe --tags --always --dirty | ✅ PASS | Git revision: 435a546-dirty |
| 23 | Requirements Traceability | Functional and hardware requirements mapped and validated | Doorstop requirements tree validation | ✅ PASS | 4 requirements validated; 100% traceability coverage |

## Standards Referenced

- **MIL-STD-461G** -- Requirements for the Control of Electromagnetic Interference Characteristics of Subsystems and Equipment
- **MIL-STD-704F** -- Aircraft Electric Power Characteristics
- **MIL-STD-1275D** -- Characteristics of 28 Volt DC Electrical Systems in Military Vehicles
- **MIL-STD-275E** -- Printed Wiring for Electronic Equipment
- **MIL-PRF-55110** -- Performance Specification: Printed Wiring Boards
- **IPC-2221A** -- Generic Standard on Printed Board Design

> **Note:** PENDING items require SPICE or EM model setup.
> See `CONTRIBUTING.md` for how to activate full simulation.
