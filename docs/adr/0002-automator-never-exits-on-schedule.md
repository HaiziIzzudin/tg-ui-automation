# The automator never exits on schedule — it is the time keeper

The original request for day/time-based execution said "exit the application if not inside time ranges", and a first design considered exactly that: the automator shuts down outside the Allowed Period, and something external (Windows Task Scheduler, or a hand launch) brings it back. We rejected that. The automator is a permanently running time keeper: it decides when the time is in range or out of range, and it launches and stops Telegram accordingly. **Shutdown** means it stops the Primary Window process (the Target Window closes with it) — never the automator itself. Outside the Allowed Period it holds a quiet **Suspend** state (no resolution, rotation, or window work — the machine belongs to the user) and wakes itself when the next Allowed Period begins. Only the user stops the automator (Ctrl+C).

## Considered Options

- **True exit + Task Scheduler relaunch** — rejected: adds external machinery to maintain and creates a dead window where nothing restarts the automator if the schedule shifts.
- **True exit + parent/watchdog process** — rejected: same external dependency, one more moving part, same failure mode.
- **Automator as time keeper** (chosen) — no external components; behavior in range and out of range are both handled inside the existing monitor loop; the process is always inspectable and logs continuously.

## Consequences

- The word "exit" in the schedule feature must never mean the automator process; a future reader tempted to "fix" the app to exit outside hours would break wake-up entirely — the app would sleep forever.
- Shutdown reuses the existing hard-kill path (the same one used by Recovery) rather than a polite close — one motion path, already proven.
- The schedule file (`schedule.json`, Format A: day/spans/`all` keys mapping to time-range lists) is re-read every monitor cycle so it can be edited live; a broken file mid-run preserves the last good schedule and logs loudly. A broken or empty schedule at launch refuses to start rather than silently running 24/7.
- Boundary rule: range start is inside, range end is outside; near-period setup launch guard (~the launch timeout) prevents launch-then-instant-kill.
