# Telegram Automator

A specialized Windows Python utility that ensures Telegram and a target secondary window are correctly positioned, resized, and monitored.

## Features
- **Auto-Launch**: Automatically starts Telegram if it's not running.
- **Window Management**: Resizes Telegram and performs a sequence of clicks at specific relative coordinates.
- **Secondary Window Control**: Targets a second window (variable title) to move and resize it.
- **Persistent Monitoring**: Continuously checks if Telegram is running and restarts the setup sequence if it crashes or closes.
- **Advanced Error Recovery**: If the secondary window is not found, the script automatically restarts Telegram and retries after a 30-second delay.
- **Secure Configuration**: Uses a `.env` file for sensitive window titles.

## Prerequisites
- Windows OS
- Python 3.x
- `pip`

## Installation
1. Clone or download this repository.
2. Install the required Python packages:
   ```bash
   pip install -r requirements.txt
   ```
3. Create a `.env` file in the root directory (already included if using the provided setup):
   ```env
   SECOND_WINDOW_TITLE=YourTargetWindowTitle
   ```

## Usage
Run the script using Python:
```bash
python telegram_automator.py
```
The script will run in the background (console) and log its activity to `telegram_monitor.log`. 

## About pixel targeting
Every size and coordinate in `.env` (`CLICK_POINTS`, `SECOND_WINDOW_SIZE`, `SECOND_WINDOW_POS`, and the built-in Telegram window size) is measured in **100% scaling pixels**.

The script reads the monitor's display scaling (100%, 150%, 200%, ...) at runtime and converts these values to real screen pixels automatically, so the same `.env` values work at any scaling. No manual adjustment is needed.

To measure Click Targets, use a tool like AutoIt3 (Au3Info.exe) while your display scaling is at **100%**. If you must measure at a different scaling, divide the measured pixel by your scaling factor (e.g. 66 px measured at 150% → write 44).

The script logs the scale factor and the real pixel positions it clicks, so you can verify targeting in `telegram_monitor.log`.

## How it Works
1. **Detection**: Checks the Windows process list for `Telegram.exe`.
2. **Setup**: Launches Telegram, resizes it to 800x600, and clicks three specific points inside.
3. **Adjustment**: Moves and resizes the secondary window specified in your `.env`.
4. **Monitoring**: Stays active, re-running the setup if Telegram process disappears or if the secondary window is lost.

## Tested on
Microsoft Windows with a 1080p (100% scaling) monitor as primary monitor.

## Troubleshooting
- **Coordinates**: Click coordinates are measured at 100% scaling; the script compensates for other scaling factors automatically. See "About pixel targeting" above.
- **Window Titles**: Ensure the window title in `.env` is exactly as it appears in the taskbar.
