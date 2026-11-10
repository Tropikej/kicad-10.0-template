# Changelog

## [Unreleased]

### Fixed

-   Refresh the bundled pcbcam fixture footprint now that the fixture is verified
    (silk no longer says "UNVERIFIED pin positions"); geometry unchanged.
-   Fixes

### Added

-   CI: `pcbcam check` machinability gate (Makera Z1) — KiCad export + machine
    DRC + hole-size checks on any board carrying `manufacturing/recipe.yaml`,
    run via `docker run` in a `kicad/kicad` image (no pcb2gcode needed, `check`
    stops before CAM). pcbcam is installed with `uvx` from its public repo
    (github.com/Tropikej/pcbcam); boards without a recipe are skipped, so the
    job is harmless on non-pcbcam projects and on the template itself.
-   Additions

### Changed

-   Changes

### Removed

-   Deletions
