# Telegram Automator

A specialized Windows Python utility that keeps Telegram and a target secondary window correctly positioned, resized, and monitored — and keeps the screen resolution and rotation correct too.

## Features
- **Auto-Launch**: Automatically starts Telegram if it's not running.
- **Window Management**: Resizes Telegram and performs a click sequence at specific relative coordinates.
- **Secondary Window Control**: Targets a second window (variable title) to move and resize it, or maximize it.
- **Persistent Monitoring**: Continuously checks if Telegram is running and restarts the setup sequence if it crashes or closes.
- **Click Retry Ladder**: If the secondary window disappears while Telegram is still running, the click sequence is retried up to a configurable limit before recovery.
- **Advanced Error Recovery**: If the retry ladder is exhausted or the secondary window is not found, the script kills Telegram and waits a configurable cooldown before retrying.
- **Resolution & Rotation Enforcement**: Checks the screen resolution and rotation every cycle and fixes them automatically — the screen is turned to Rotation 0 (horizontal) first, the resolution is set there, then the screen is turned to the configured rotation (via `rotate-screen` and the bundled `SetResolution.exe`).
- **Dry Run (`--dry-run`)**: Rehearsal mode — everything runs for real up to the second click, then the program stops itself with a summary; the third click and all secondary-window work are skipped.
- **Display Scaling Compensation**: All configured sizes and coordinates are measured in 100% scaling pixels and converted to real pixels at runtime (see "About pixel targeting").
- **Secure Configuration**: Uses a `.env` file for sensitive window titles and all tunable settings.
- **Time Ranges (Schedule)**: Optionally limit when Telegram runs to day/time ranges defined in a `schedule.json` file (see "Schedule" below).
- **Stream Recording (optional)**: Records a live stream straight to disk while the automator runs — plus around the clock for the "long-living" mode or only during the schedule's hours (see "Recording" below).
- **Broadcasting (optional)**: Sends this whole screen live through OBS Studio, starting and stopping the stream itself and following the Schedule (see "Broadcasting" below).

## Prerequisites
- Windows OS
- Python 3.x
- `pip`
- The bundled `SetResolution.exe` (included in the repository)

## Installation
1. Clone or download this repository.
2. Install the required Python packages:
   ```bash
   pip install -r requirements.txt
   ```
3. Create a `.env` file in the root directory (copy `example.env` as a starting point):
   ```env
   SECOND_WINDOW_TITLE=YourTargetWindowTitle
   ```

## Usage
Run the script using Python:
```bash
python main.py
```
The script will run in the console and log its activity to `telegram_monitor.log`.

Two optional flags sit beside it: `python main.py --dry-run` for a rehearsal, and `python main.py --check-obs` to report whether this computer is ready to broadcast, without going live.

Run the tests with:
```bash
pytest
```

## Dry run (`--dry-run`)
A rehearsal mode to watch the automator work without the second-window part:

```bash
python main.py --dry-run
```

- The screen check runs for real — the same turn-to-horizontal, set-size, turn-back fix as a normal run.
- If Telegram is already running it is closed first, then launched fresh, resized, and the first two clicks of the Click Sequence are performed for real.
- The third click is skipped on purpose — it is the click that opens the secondary window — and no secondary-window work, click retry, or recovery ever runs.
- When the rehearsal finishes the program stops itself with a summary; Telegram is left open for inspection.
- Outside the allowed hours nothing is rehearsed and the program stops with a message. (It behaves exactly like a normal run there: any running Telegram is closed.)

## Schedule (`schedule.json`)
The automator normally runs 24/7. Optionally, place a `schedule.json` file next to the app to restrict when Telegram runs. If the file is **absent**, the automator behaves exactly as always (24/7).

The automator itself is the time keeper — it never exits on its own. Outside the allowed time ranges it stops Telegram (the secondary window closes with it), then quietly waits and starts Telegram again by itself when the next time range begins. Only you stop the automator (Ctrl+C). While suspended, it also stops enforcing resolution and rotation — the machine is fully yours.

Format: day keys mapped to a list of time ranges. Day keys are day names (`mon`…`sun`, case-insensitive), day spans (`mon-fri`, may wrap the week, e.g. `sat-mon`), or `all` (every day). Ranges are `HH:MM-HH:MM`, optionally with seconds (`HH:MM:SS-HH:MM:SS`); both ends of one range must use the same precision. Ranges may cross midnight — the part after midnight belongs to the day the range started on. The range start counts as inside, the end counts as outside. Overlapping ranges simply add up.

Example:

```json
{
  "mon-fri": ["08:00-17:00"],
  "sat":     ["10:00-12:30", "13:30-18:00"],
  "all":     ["22:00-02:00"]
}
```

Notes:
- Times use your computer's own local clock. There is no time-zone setting.
- Edit the file at any time — it is re-read on every monitor cycle (a few seconds), so the change applies without a restart.
- A broken file (bad day name, bad time, start equal to end, empty list, empty or non-object content) stops the automator at startup with a clear error. While running, a broken edit keeps the last working schedule and logs the problem loudly.
- Deliberately skipping the full setup near the end of a range: when Telegram isn't already running and the range ends within the launch window (about 7 seconds), the automator waits instead of launching Telegram just to close it seconds later.
- Use `example.schedule.json` as a starting point.

## Recording (optional)
The automator can record a live stream to disk while it looks after the windows. Recording runs on the same computer as the automator and is independent of Telegram and the target window: recovery, click retry, shutdown, and the screen fix never interrupt it. One stream address, one folder for the recorded files.

One setting picks the mode. There are two modes plus off:

| `RECORD_MODE` | What it does |
|---|---|
| `off` (default) | Never records. |
| `long-living` | Records day and night — including outside the allowed hours and while the automator is suspended — and ignores the schedule. |
| `follow-schedule` | Records only during the allowed hours in `schedule.json` (with no schedule file, everything is allowed, so it records 24/7). It closes the current file when a period ends and starts a fresh one when the next period begins. |

The nine settings (all optional, see `example.env`):

| Variable | Default | Description |
|---|---|---|
| `RECORD_MODE` | `off` | `off`, `long-living`, or `follow-schedule`. Anything else warns and falls back to `off`. |
| `RECORD_STREAM_URL` | *(empty)* | The stream address to record. Empty means recording cannot start. |
| `RECORDINGS_DIR` | the `Recordings` folder next to the program | Folder for the records. **Must be an absolute path** — a drive path like `D:\Recordings` or a UNC path like `\\server\share\clips`. A relative or empty value warns and falls back to the default. Created when missing. |
| `RECORD_CLIP_SECONDS` | `7200` | Length of one recorded file, in seconds. |
| `RECORD_RECONNECT_SECONDS` | `5` | Wait after the recorder stops unexpectedly before it reconnects. |
| `RECORD_FFMPEG_PATH` | `ffmpeg` | The recorder program. A bare name is looked up on the system path; a full path is used as given. |
| `RECORD_DISK_WARN_GB` | `10` | Free space, in gigabytes, below which a warning is logged once per crossing. |
| `RECORD_DISK_STOP_GB` | `2` | Free space, in gigabytes, below which recording stops. Must be below the warning level. |
| `LOG_LEVEL` | `INFO` | How much is written to the log: `DEBUG`, `INFO`, `WARNING`, or `ERROR` (case-insensitive). Anything else warns and falls back to `INFO`. Set `DEBUG` to see the recorder's own words. |

**Where recordings land.** Each recorded file covers one stretch of the stream and is named by the moment it started, e.g. `20261019_140000.mp4`, inside `RECORDINGS_DIR`.

**Where it starts.** Before recording begins — on the first start and on every reconnection — the automator looks at the stream's playlist and finds the newest segment that really exists, then tells the recorder to begin just behind it. This matters because the playlist advertises far more history than the server keeps, so the older entries answer "not found". If the stream cannot be read in time, recording still starts, using a safe default position, and says so in the log.

The log names the file currently being written, once each time a new one starts. The recorder's own words are read quietly in the background: the useful few lines (the stream it opened, its details, the output it created, and its ready line) are written at normal level, everything else — the per-segment chatter — only when `LOG_LEVEL` is `DEBUG`. If the recorder stops on its own, its last twenty lines are logged as a warning so the failure explains itself. A normal stop never dumps those lines.

**Recording never stops the window job.** A missing recorder program, a wrong or empty stream address, or a failed start is only logged loudly and retried; the automator keeps running. If the disk gets critically low the automator warns, stops recording so the disk never fills, and starts again once space is back — it never deletes a file.

The dry run (`--dry-run`) never records: a rehearsal is window work only.

The old `ffmpeg_cmd.ps1` PowerShell script stays in the repository as a manual backup only. It is not supported and is not used by the automator.

## Broadcasting (optional)
The automator can send **this whole screen** out to a stream site through OBS Studio while it keeps looking after the windows. Nobody presses *Start Streaming* or *Stop Streaming*: the automator presses both, at the right moments, and keeps an eye on the stream in between.

**Broadcasting is not recording.** Recording pulls *someone else's* stream down from the network into files on this computer. Broadcasting pushes *this screen* out to a stream site. The two directions share nothing — no setting, no switch, no state — so turning one on never turns the other on, and a problem with one never touches the other.

**The one switch.** `BROADCAST_ENABLED` is the only control. It is off by default, and off means exactly today's behaviour: OBS is not opened, and no stream is started or stopped.

**It follows the Schedule.** Inside an allowed period the stream goes live at the first check — *before* the windows are placed, by choice, so a viewer may watch the desktop, the setup clicks, or a recovery happen. The screen fix runs first, so a resolution or rotation change never shows up in the middle of a live picture. When the period ends, or when you press Ctrl+C, the stream is stopped and OBS is left open and idle. With no `schedule.json`, every moment is allowed, so it broadcasts around the clock.

**What must already be true inside OBS.** The automator presses two buttons OBS already has, and nothing more. Before you switch broadcasting on, OBS itself must have:
- its WebSocket server turned on;
- a password set on that server, with its port and that password known to you;
- the stream destination (server address) and the stream key already filled in.

The automator never reads or writes OBS's own settings, never switches scene or profile, and never sees the stream key. Where the stream goes stays entirely inside OBS, exactly as you typed it there.

**Both programs at the same privilege level.** Start the automator and OBS with the same rights — either both normally, or both as administrator. Windows will not let a normal program open, close, or drive a program that was started as administrator, and the reverse is also true. If they differ, OBS is up and answers nothing; the check mode below is what reports that silence.

**What the automator does with OBS.** It opens OBS itself, once, when it starts — folded to the tray, so it cannot steal a click, on whatever scene it had last. It does that even when the clock is outside an allowed period, so OBS is ready the moment a period starts. If you close OBS while it is needed, it is opened again at the next check. Nobody closes it: OBS is left open, idle, when the run ends.

The five settings (all optional, see `example.env`):

| Variable | Default | Description |
|---|---|---|
| `BROADCAST_ENABLED` | `false` | The only switch. Accepts `true/false`, `1/0`, `yes/no`, `on/off`, any case. Anything else warns and stays off. |
| `BROADCAST_OBS_HOST` | `127.0.0.1` | Where OBS's remote control listens. |
| `BROADCAST_OBS_PORT` | `4455` | Its port. |
| `BROADCAST_OBS_PASSWORD` | *(empty)* | The WebSocket password. Empty is accepted here; OBS itself then refuses and the log says why once. |
| `BROADCAST_OBS_PATH` | `C:\Program Files\obs-studio\bin\64bit\obs64.exe` | The OBS program. A missing file is logged once and broadcasting is off for that run; the window job keeps running. |

An empty host or an empty program path falls back to the default in the table, with one warning, rather than leaving broadcasting half-configured.

**Check a new machine (`--check-obs`).** Before anything goes live on a computer, ask the automator whether that computer is ready:

```bash
python main.py --check-obs
```

It reports six things, one line each, and then stops. It never opens OBS, never starts or stops a stream, and never touches a window, the screen, or the rotation, so it is safe on a machine that is mid-shift.

1. whether the switch is on;
2. whether the OBS program file is really there at `BROADCAST_OBS_PATH`;
3. whether a password is set — said as *set* or *not set*; the password itself is never printed;
4. whether OBS is running right now;
5. what OBS answers about the stream: `live`, `idle`, or the reason it could not be reached;
6. whether broadcasting would be live at this moment, given the clock and the Schedule.

It ends with exit code `0` when OBS answered, and `1` when it did not — so a script can tell the two apart, and so can you with `$LASTEXITCODE` in PowerShell. The lines also land in `telegram_monitor.log`, like every other log line. Given `--dry-run` at the same time, the check wins and nothing else runs.

The first run on a new computer, in this order:

1. With `BROADCAST_ENABLED=false`, start the automator once. The log says broadcasting is off and the window job works as always.
2. Turn the switch on and run the check mode. It must say `idle` (or `live`, if a stream is running), or name exactly what is missing.
3. Only then run for real, inside an allowed period.

No step in that list can start a stream by itself.

**The quiet facts.** Nothing here needs your attention while it is working:
- OBS closed by you is opened again at the next check, and the stream follows. That is limited to three reopenings per allowed period (the Restart Budget), counted afresh at the start of each period.
- OBS that stops answering — refusing, timing out, or rejecting the password — is killed and opened again after about two minutes of silence, also counted against those three. When the three are used up, the log says so once and OBS is left alone until the next period.
- A stream that drops is started again at the next check: one warning, then one line when it is back.
- A start the stream site refuses (a wrong key, a rejected account, an encoder problem) is retried at every check, forever, without spending a reopening: one warning when the trouble appears and one line when it clears. The automator cannot say which of those it is — OBS's own log holds the reason.
- A rehearsal (`--dry-run`) never opens OBS and never goes live. With the switch on, a stream it finds running is stopped.
- With the switch on, a stream left running by a crash is stopped at the first check outside an allowed period, on the next startup. With the switch off, the automator leaves OBS alone entirely.
- While OBS is silent, each check waits up to three seconds for the answer before doing its window work, so the cycle is slower until OBS answers again.
- Broadcast trouble never stops the window job. Telegram keeps being managed exactly as usual, and the automator never exits because of OBS.

## List Recording (optional)
A second, separate program records **everyone on the live stream list** — the people a local bridge reports as streaming right now. The automator never starts it. It reads no Schedule and no Recording Mode, and it does no window or screen work. Start it in its own console, beside the automator or on its own.

It needs two things: the list address answering, and the recorder program available. The list is a plain JSON array published by the bridge (`STREAMS_URL`, `http://127.0.0.1:8479/` by default). The recorder program is `STREAMS_FFMPEG_PATH` (`ffmpeg` by default). If the recorder program cannot be found the program says so and stops, because nothing could ever be recorded; every other problem is only logged and the program carries on.

```bash
python record_streams.py
```

The program reads the list every few seconds and keeps one recording per person on it. Press **Ctrl+C** to stop it. Every open file is closed properly first, so the last file plays to its end, and no recorder is left running behind it.

The nine settings (all optional, see `example.env`):

| Variable | Default | Description |
|---|---|---|
| `STREAMS_URL` | `http://127.0.0.1:8479/` | The Stream List address. |
| `STREAMS_POLL_SECONDS` | `5` | Seconds between two readings of the list. |
| `STREAMS_CLIP_SECONDS` | `900` | Length of one file, in seconds. 15 minutes. |
| `STREAMS_RECONNECT_SECONDS` | `5` | Wait after a stream drops before each reconnection attempt. |
| `STREAMS_RETRY_LIMIT` | `2` | Reconnection attempts per drop before the person is given up. |
| `STREAMS_RETRY_RESET_SECONDS` | `120` | How long a reconnected stream must last to count as a good run and give the person their attempts back. |
| `STREAMS_FFMPEG_PATH` | `ffmpeg` | The recorder program. A bare name is looked up on the system path; a full path is used as given. |
| `STREAMS_DISK_WARN_GB` | `10` | Free space, in gigabytes, below which a warning is logged once per crossing. |
| `STREAMS_DISK_STOP_GB` | `2` | Free space, in gigabytes, below which every List Recording stops. Must be below the warning level. |

It also reads `RECORDINGS_DIR` and `LOG_LEVEL`, exactly as the automator does. Those two are the only shared values.

**Where Clips land.** The same folder as the automator's recordings (`RECORDINGS_DIR`), straight in it — no folders per person. A List Recording is named after the person first, then the moment it started: `Alif_Naim_261025_143000.mp4` is the person `Alif Naim` starting on 2026-10-25 at 14:30:00. The shape is the Clean Name, an underscore, a two-digit year, month, day, underscore, hour, minute, second, then `.mp4`. In the Clean Name, spaces become underscores and the characters Windows refuses in a file name become underscores; two people whose names end up the same get `_2`, `_3`, and so on. That leading name is what tells the two kinds of file apart: a List Recording starts with the person's name, the automator's own files start with the year, e.g. `20261019_140000.mp4`.

**Files roll every 15 minutes.** One file covers one stretch of one person's stream. When it reaches `STREAMS_CLIP_SECONDS` (15 minutes by default) the recorder closes it and starts the next one, each file playable on its own. The source only makes a clean cut point about every 30 seconds, so a file may run a few seconds over or under the length you set.

**When someone leaves.** A person is recorded while they are on the list. Missing from a single reply changes nothing; missing from two replies in a row (about ten seconds at the default poll) means they left, and their file is closed properly. A reply that cannot be read at all proves nothing either: a bridge restart or a dropped connection stops no recording, the failure is written to the log, and the log says so when the list answers again.

**A stream that dies but the person stays listed.** The recorder waits `STREAMS_RECONNECT_SECONDS` and tries again, up to `STREAMS_RETRY_LIMIT` times. A stream that cannot be started at all counts the same way. When the attempts are used up the person is **Given Up**: no more attempts while they stay listed, with one line in the log saying why. They are recorded again once they leave the list and come back, or if their stream address changes. A reconnected stream that lasts longer than `STREAMS_RETRY_RESET_SECONDS` counts as a good run, so a later drop gets the two attempts back.

**The disk guard works like the automator's.** Free space under `STREAMS_DISK_WARN_GB` logs one warning and recording continues. Free space under `STREAMS_DISK_STOP_GB` stops every List Recording so the disk never fills, and nothing new starts until space is back at the warning level. No Clip is ever deleted. Stopping for the disk is not the person's fault, so everyone starts over when space returns, including a person who had been given up.

**Where it writes.** Its console and its own log file, `record_streams.log`, next to the program. Non-Latin names survive in it. The automator's `telegram_monitor.log` is never written to.

**What it does not do.** No Schedule — it records at 3 a.m. as happily as at noon. No Recording Mode. No Telegram and no screen work: it never moves a window, clicks, or changes the resolution. It is not a Clip manager: it never deletes, moves, compresses, or uploads a file. It does not install itself as a service or a scheduled task; you start it and you stop it.

**Both programs at once.** It can run alongside the automator. Both write into the same Clips folder and neither disturbs the other, and the two sets of names cannot clash because one kind leads with the person's name and the other with the year. The folder simply fills faster when both are running.

## Configuration (`.env`)
All settings are optional and fall back to sensible defaults (see `example.env`):

| Variable | Default | Description |
|---|---|---|
| `SECOND_WINDOW_TITLE` | `secondWindowTitle` | Exact title of the secondary window to control. |
| `SCREEN_RESOLUTION` | `1920x1080` | Expected screen resolution, enforced every cycle. |
| `SCREEN_ROTATION` | `0` | Expected screen rotation in degrees: `0`, `90`, `180`, or `270`. |
| `CLICK_POINTS` | `44,240;207,119;755,113` | Semicolon-separated `x,y` click targets inside the Telegram window, in 100% scaling pixels. |
| `SECOND_WINDOW_SIZE` | `1280x1032` | Secondary window size as `WxH`, or `maximized` to maximize it. |
| `SECOND_WINDOW_POS` | `0,0` | Secondary window position as `x,y`. |
| `KILL_COOLDOWN_SECONDS` | `60` | Seconds to wait after killing Telegram before retrying. |
| `SECOND_WINDOW_RETRY_LIMIT` | `3` | Click retry attempts before recovery kicks in. |
| `CLICK_RETRY_INTERVAL` | `3.0` | Seconds between click retry attempts. |

The nine recording settings are listed in "Recording" above, the nine List Recording settings in "List Recording" above, and the five broadcasting settings in "Broadcasting" above.

## About pixel targeting
Every size and coordinate in `.env` (`CLICK_POINTS`, `SECOND_WINDOW_SIZE`, `SECOND_WINDOW_POS`, and the built-in Telegram window size) is measured in **100% scaling pixels**.

The script reads the monitor's display scaling (100%, 150%, 200%, ...) at runtime and converts these values to real screen pixels automatically, so the same `.env` values work at any scaling. No manual adjustment is needed.

To measure Click Targets, use a tool like AutoIt3 (Au3Info.exe) while your display scaling is at **100%**. If you must measure at a different scaling, divide the measured pixel by your scaling factor (e.g. 66 px measured at 150% → write 44).

The script logs the scale factor and the real pixel positions it clicks, so you can verify targeting in `telegram_monitor.log`.

## How it Works
1. **Schedule Gate** (optional): If `schedule.json` exists and the current time is outside all allowed ranges, stops Telegram (if running), skips all other work this cycle (no screen enforcement), and waits for the next range. Everything below is skipped while suspended.
2. **Detection**: Checks the Windows process list for `Telegram.exe`.
3. **Screen Guard**: Every cycle, compares actual rotation and resolution against `SCREEN_ROTATION` / `SCREEN_RESOLUTION`. When either is off, it fixes both in one pass — turn the screen to Rotation 0 (horizontal), set the resolution there, then turn the screen to the configured rotation. Each move is verified by reading the screen back and retried up to two times. If the first turn fails, the resolution step is skipped that cycle (the resolution is never set on a sideways screen); if the resolution step fails, the screen still finishes at the configured rotation; whatever is still off is retried next cycle.
4. **Setup**: Launches Telegram, resizes it to 800x600, and clicks the configured `CLICK_POINTS` inside.
5. **Adjustment**: Moves and resizes the secondary window specified in your `.env` (or maximizes it).
6. **Monitoring**: Stays active:
   - If the Telegram process disappears, re-runs the full setup.
   - If the secondary window is lost while Telegram is running, retries the click sequence up to `SECOND_WINDOW_RETRY_LIMIT` times (spaced by `CLICK_RETRY_INTERVAL`); if that fails, kills Telegram and waits `KILL_COOLDOWN_SECONDS` before restarting.

## Tested on
Microsoft Windows with a 1080p (100% scaling) monitor as primary monitor.

## Troubleshooting
- **Coordinates**: Click coordinates are measured at 100% scaling; the script compensates for other scaling factors automatically. See "About pixel targeting" above.
- **Window Titles**: Ensure the window title in `.env` is exactly as it appears in the taskbar.
- **Resolution/Rotation keep changing**: Check `SCREEN_RESOLUTION` and `SCREEN_ROTATION` in `.env`; the script enforces these values every cycle and logs mismatches to `telegram_monitor.log`.
