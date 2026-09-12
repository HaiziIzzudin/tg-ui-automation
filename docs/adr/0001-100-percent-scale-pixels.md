# All configured coordinates and sizes are 100%-scale pixels

Display scaling varies across machines (100%, 150%, 170%, 200%), and at any scaling above 100% the automator's coordinate targeting breaks. We decided that every size and coordinate in configuration (Click Targets, Target Window size/position, Primary Window size) means **pixels at 100% scaling**, and the automator reads the primary screen's Scale Factor from Windows at runtime and converts to real pixels before every resize, move, and click. The automator never changes scaling — it only reads and logs it.

## Considered Options

- **Enforce 100% scaling** like Rotation and Resolution — rejected: `SetResolution.exe` cannot set scaling, and programmatic enforcement (registry + broadcast) is unreliable and may require sign-out.
- **Raw per-machine pixels** (re-measure Click Targets whenever scaling changes) — rejected: silently breaks targeting when scaling changes; error-prone hand re-measurement.

## Consequences

- The process must be DPI-aware (Per-Monitor-V2) so Windows reports real pixels consistently; Scale Factor is read each monitor cycle and assumed 1.0 (with a warning) if unreadable.
- `SCREEN_RESOLUTION` remains real pixels — it matches what Windows display settings shows for the monitor.
