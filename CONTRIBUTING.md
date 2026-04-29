# Contributing to Hermes Flight Controller

Thank you for your interest in contributing! This project is a hardware design
for a drone flight controller. Contributions to the schematic, PCB layout,
documentation, and firmware (when it arrives) are all welcome.

## Getting Started

1. **Fork** this repository and create a feature branch from `main`.
2. Make your changes, following the guidelines below.
3. Open a **Pull Request** with a clear description of what you changed and why.

## KiCad Design Guidelines

- Use **KiCad 7** or later.
- Follow the existing layer stack: 4-layer board (F.Cu / In1.Cu / In2.Cu /
  B.Cu).
- Keep the design rule settings consistent with those in `HermesFC.kicad_pro`.
- Run **ERC** (Electrical Rules Check) and **DRC** (Design Rule Check) before
  submitting. Fix all errors; annotate any intentional warnings in the PR
  description. CI will run both automatically via `kicad-cli` in the
  `kicad.yml` workflow.
- Use **LCSC** part numbers in the `LCSC Part #` symbol field so the BOM stays
  JLCPCB-compatible.
- Export manufacturing files to `PCB/HermesFC/manufacturing/` using the
  Fabrication Toolkit plugin (or equivalent):
  - Gerbers as `HermesFC-Gerber.zip`
  - BOM as `bom.csv`
  - Pick-and-place as `positions.csv`
  - IPC-D-356 netlist as `netlist.ipc`
- KiBot will automatically regenerate Gerbers, BOM, PDFs, and 3D renders
  when the CI pipeline runs (see `PCB/kibot.yaml`).

## SPICE Simulation

The CI pipeline exports a SPICE netlist and validates power-rail voltages via
`scripts/check_spice.py`. To enable real simulation:

1. Add SPICE models to each component's symbol properties in KiCad.
2. Add simulation directives (`.op`, `.tran`, `.ac`) to the schematic.
3. Update the thresholds in `scripts/check_spice.py` if the power topology
   changes.

## Documentation Guidelines

- Keep `README.md` and `PCB/HermesFC/README.md` up to date with any design
  changes.
- Lines in Markdown files should not exceed 80 characters (tables and code
  blocks excluded).
- Run `markdownlint` before submitting:

  ```sh
  npx markdownlint-cli "**/*.md" --ignore node_modules
  ```

## Commit Messages

Use short, imperative-mood commit messages, e.g.:

- `Add USB ESD protection diode to schematic`
- `Fix DRC clearance violation on inner layers`
- `Update BOM with new LCSC part number for U5`

## License

By contributing, you agree that your contributions will be licensed under the
[MIT License](LICENSE).
