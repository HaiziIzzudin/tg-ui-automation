# Telegram Automator

A specialized Windows Python utility that keeps Telegram and a target secondary window correctly positioned, resized, and monitored — and keeps the screen resolution and rotation correct too.

## Features
- **Auto-Launch**: Automatically starts Telegram if it's not running.
- **Window Management**: Resizes Telegram and performs a click sequence at specific relative coordinates.
- **Secondary Window Control**: Targets a second window (variable title) to move and resize it, or maximize it.
- **Persistent Monitoring**: Continuously checks if Telegram is running and restarts the setup sequence if it crashes or closes.
- **Click Retry Ladder**: If the secondary window disappears while Telegram is still running, the click sequence is retried up to a configurable limit before recovery.
- **Advanced Error Recovery**: If the retry ladder is exhausted or the secondary window is not found, the script kills Telegram and waits a configurable cooldown before retrying.
- **Resolution & Rotation Enforcement**: Checks the screen resolution and rotation every cycle and fixes them automatically (via `rotate-screen` and the bundled `SetResolution.exe`).
- **Display Scaling Compensation**: All configured sizes and coordinates are measured in 100% scaling pixels and converted to real pixels at runtime (see "About pixel targeting").
- **Secure Configuration**: Uses a `.env` file for sensitive window titles and all tunable settings.

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

Run the tests with:
```bash
pytest
```

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

## About pixel targeting
Every size and coordinate in `.env` (`CLICK_POINTS`, `SECOND_WINDOW_SIZE`, `SECOND_WINDOW_POS`, and the built-in Telegram window size) is measured in **100% scaling pixels**.

The script reads the monitor's display scaling (100%, 150%, 200%, ...) at runtime and converts these values to real screen pixels automatically, so the same `.env` values work at any scaling. No manual adjustment is needed.

To measure Click Targets, use a tool like AutoIt3 (Au3Info.exe) while your display scaling is at **100%**. If you must measure at a different scaling, divide the measured pixel by your scaling factor (e.g. 66 px measured at 150% → write 44).

The script logs the scale factor and the real pixel positions it clicks, so you can verify targeting in `telegram_monitor.log`.

## How it Works
1. **Detection**: Checks the Windows process list for `Telegram.exe`.
2. **Screen Guard**: Every cycle, compares actual rotation and resolution against `SCREEN_ROTATION` / `SCREEN_RESOLUTION` and fixes mismatches (retries with read-back before giving up until the next cycle).
3. **Setup**: Launches Telegram, resizes it to 800x600, and clicks the configured `CLICK_POINTS` inside.
4. **Adjustment**: Moves and resizes the secondary window specified in your `.env` (or maximizes it).
5. **Monitoring**: Stays active:
   - If the Telegram process disappears, re-runs the full setup.
   - If the secondary window is lost while Telegram is running, retries the click sequence up to `SECOND_WINDOW_RETRY_LIMIT` times (spaced by `CLICK_RETRY_INTERVAL`); if that fails, kills Telegram and waits `KILL_COOLDOWN_SECONDS` before restarting.

## Tested on
Microsoft Windows with a 1080p (100% scaling) monitor as primary monitor.

## Troubleshooting
- **Coordinates**: Click coordinates are measured at 100% scaling; the script compensates for other scaling factors automatically. See "About pixel targeting" above.
- **Window Titles**: Ensure the window title in `.env` is exactly as it appears in the taskbar.
- **Resolution/Rotation keep changing**: Check `SCREEN_RESOLUTION` and `SCREEN_ROTATION` in `.env`; the script enforces these values every cycle and logs mismatches to `telegram_monitor.log`.
