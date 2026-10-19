# Recording lives inside the automator; the old recorder script stays as a manual backup

Stream recording used to be a standalone PowerShell loop on a second computer: it restarted ffmpeg on disconnect and wrote timestamped clips into a `Recordings` folder. We decided to fold recording into the automator itself (`main.py`), on the same computer: the automator now owns starting, stopping, reconnecting, and watching the recorder (ffmpeg, run as a child process). A single setting selects the **Recording Mode** — `off`, `long-living`, or `follow-schedule` — and **follow-schedule** consults the very same `schedule.json` that gates the window work. The old `ffmpeg_cmd.ps1` is deliberately kept in the repository as an unsupported manual fallback for recording by hand; the Python recorder is the only supported path.

## Considered Options

- **Keep recording on the second computer, controlled remotely** — rejected: the automator would need network control over another machine, one more failure surface, and two computers to keep alive for one job.
- **Separate recording service (its own process or Task Scheduler job)** — rejected: same "two things must agree on the clock" problem the schedule feature already solved by making the automator the time keeper (ADR-0002).
- **Python wrapper around the PowerShell script** — rejected: two runtimes for one loop, output parsing across shells, and the script would still be load-bearing while looking like a backup.
- **Recorder as a child process of the automator, driven in Python** (chosen) — one program, one log, one settings file; the schedule gate already exists in the monitor loop and can gate recording too.

## Consequences

- **Recording** is independent of the Primary Window and Target Window: Recovery and Click Retry never touch it. Only the Recording Mode and the Schedule decide its fate.
- **long-living** records day and night and ignores `schedule.json` entirely, even while the automator is in **Suspend**. **follow-schedule** stops at the end of the Allowed Period (**Recording Stop**, closing the current **Clip** so it plays) and begins a fresh Clip at the next period start (**Recording Resume**).
- Every **Reconnection** after a stream drop begins a fresh Clip rather than appending to the old one.
- The settings surface is eight values: recording mode, stream address, clips folder, clip length, reconnection wait, the recorder program's path, and the two disk levels (warning and stop). The clips folder must be an absolute path; a relative value warns and falls back to the `Recordings` folder next to the program. Clip names stay timestamped as before.
- Recording faults never fail the window job: a missing recorder program or wrong stream address at launch logs loudly and retries; the automator still runs. A full disk warns first and then stops recording (**Disk Stop**) rather than filling the machine — clips are never deleted automatically.
- The dry run never records; it rehearses window work only.
- A future reader may "clean up" `ffmpeg_cmd.ps1` as dead code. It is a deliberate manual backup; deleting it is fine only if the Python recorder is already proven in production.
