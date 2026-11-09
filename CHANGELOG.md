# Changelog

## [Unreleased]

### Fixed

-   Fixes

### Added

-   CI: `pcbcam check` machinability gate (Makera Z1) — KiCad export + machine
    DRC + hole-size checks on any board carrying `manufacturing/recipe.yaml`,
    run in a `kicad/kicad` container (no pcb2gcode needed, `check` stops before
    CAM). Dormant until the `PCBCAM_SRC` Actions variable points at the public
    pcbcam repo (e.g. `git+https://github.com/<you>/CNC-PCB-milling.git@main`).
-   Additions

### Changed

-   Changes

### Removed

-   Deletions
