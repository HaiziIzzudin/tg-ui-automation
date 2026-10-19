# Telegram Automator

A Windows automation context that keeps Telegram and a target window positioned, sized, and running via a persistent monitor loop.

## Language

**Primary Window**:
The Telegram window that the automator launches, resizes, and clicks inside.
_Avoid_: Main window, Telegram app

**Target Window**:
The secondary window, identified by its exact title, that the automator moves and resizes after the Click Sequence.
_Avoid_: Second window, secondary window

**Click Sequence**:
A fixed list of three clicks made at pixel offsets from the Primary Window's top-left corner; running it is what brings the Target Window into existence.
_Avoid_: Actions, clicks

**Click Target**:
One measured pixel offset from the Primary Window's top-left corner, expressed in 100%-scale pixels; the automator converts it to real pixels using the current Scale Factor. Environment-specific and tuned manually per machine.
_Avoid_: Touch target, coordinate

**Scale Factor**:
The primary screen's display scaling multiplier (1.0, 1.5, 2.0, ...), read from Windows at runtime, never changed by the automator. All configured sizes and coordinates are in 100%-scale pixels and are multiplied by the Scale Factor before use.
_Avoid_: DPI, scaling percentage

**Setup Sequence**:
Launch Primary Window, run the Click Sequence, then position and resize the Target Window. Run when the Primary Window process is absent.
_Avoid_: Initialization, bootstrap

**Click Retry**:
Re-running the Click Sequence to recreate a missing Target Window, without restarting the Primary Window.
_Avoid_: Re-setup, re-click

**Recovery**:
Killing the Primary Window process and relaunching it, used when Click Retry cannot restore the Target Window.
_Avoid_: Restart, reset

**Cooldown**:
The wait period after Recovery before the monitor loop checks again.
_Avoid_: Delay, backoff

**Rotation**:
The orientation of the primary screen in degrees (0, 90, 180, 270); the automator enforces the configured Rotation alongside resolution.
_Avoid_: Orientation, tilt

## Scheduling

**Schedule**:
An optional file listing when the automator may run. Multiple entries are allowed; if any entry matches, the automator runs. A missing file means "always allowed".
_Avoid_: Timetable, whitelist

**Schedule Entry**:
One item in the Schedule: a day key (a single day name, a day span, or "all") mapped to one or more Time Ranges. Every entry needs at least one Time Range; an empty list is a broken file, not a whole-day entry.
_Avoid_: Rule, slot

**Time Range**:
A start and end clock time inside one Schedule Entry, using the computer's own clock. Start counts as inside; end counts as outside. May cross midnight.
_Avoid_: Interval, window (collides with windows on screen)

**Allowed Period**:
A stretch of clock time when running is permitted, computed from matching Schedule Entries.
_Avoid_: Active time, window

**Shutdown**:
Stopping the Primary Window process because the time left the Allowed Period; the Target Window closes with it. The automator itself keeps running and waits for the next Allowed Period.
_Avoid_: Exit, quit, close the app (they wrongly suggest the automator stops)

**Suspend**:
The quiet state while outside any Allowed Period: no Primary Window running, no window work, only clock checks.
_Avoid_: Sleep mode, idle

## Recording

**Recording**:
Capturing the live stream straight from the network and writing it to disk, done by the automator on its own computer. Independent of the Primary Window and the Target Window: it survives Recovery and Click Retry.
_Avoid_: Taping, capture session

**Clip**:
One recorded file covering a stretch of stream. A Clip is closed properly so it plays on its own, and is named by the moment it started.
_Avoid_: Segment, chunk, part, video file

**Recording Mode**:
The setting deciding when recording happens. Exactly one of three values: off, Long-living, or Follow-schedule.
_Avoid_: Record setting, capture mode

**Long-living**:
A Recording Mode where recording runs day and night and never consults the Schedule, even while the automator Suspend.
_Avoid_: 24/7 mode, continuous mode

**Follow-schedule**:
A Recording Mode where recording is allowed only inside an Allowed Period. When the period ends the recording stops and closes its Clip; at the next period start a fresh Clip begins.
_Avoid_: Scheduled mode, timetable mode

**Recording Stop**:
Closing the current Clip and waiting, because the Allowed Period ended under Follow-schedule. Distinct from Shutdown, which concerns the Primary Window only.
_Avoid_: Recording shutdown

**Recording Resume**:
Starting a fresh Clip at the start of the next Allowed Period under Follow-schedule.
_Avoid_: Recording restart

**Reconnection**:
The automatic restart of the recording after the stream drops or the recorder stops. It always begins a fresh Clip and repeats until recording is stopped for good.
_Avoid_: Reconnect loop, retry

**Disk Stop**:
Stopping recording because the disk is critically low on space, after a warning. No Clip is ever deleted for the user.
_Avoid_: Cleanup, eviction
