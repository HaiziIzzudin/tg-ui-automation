import os
import sys
import time
import datetime
import subprocess
import shutil
import logging
import pyautogui
import pygetwindow as gw
import psutil
import ctypes
import rotatescreen
from dotenv import load_dotenv
from config import Config
from schedule import SCHEDULE_FILENAME, ScheduleError, load_schedule
from recorder import (
    Recorder,
    RecorderSettings,
    classify_free_space,
    ffmpeg_available,
)

# Load environment variables from .env file
load_dotenv()

# ==============================================================================
# CONFIGURATION
# ==============================================================================
TELEGRAM_EXE = "Telegram.exe"
TELEGRAM_PATH = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "Telegram Desktop", "Telegram.exe")
TELEGRAM_TITLE = "Telegram"

MONITOR_INTERVAL = 3.0      # Seconds to wait between process checks
LAUNCH_TIMEOUT = 7.0        # Seconds to wait for Telegram to launch
ACTION_DELAY = 0.5          # Seconds to wait between window actions (resize/click)


TELEGRAM_SIZE = (800, 600)

# In a dry run, only this many clicks of the Click Sequence are performed for
# real; the rest (starting with the third click, the one that opens the Target
# Window) are skipped.
DRY_RUN_CLICKS = 2

# Initialize config from environment variables
config = Config()

# ==============================================================================
# LOGGING SETUP
# ==============================================================================
# Set up logging to both console and a file in the same directory as the script.
log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_monitor.log")

# Optional schedule file, one fixed name next to the app (same directory as
# the log file). Its absence means the schedule feature is off.
DEFAULT_SCHEDULE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), SCHEDULE_FILENAME
)

# Ensure stdout/stderr support UTF-8 (emoji in window titles, etc.)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def log_level_value() -> int:
    """Return the logging level named by ``LOG_LEVEL``, defaulting to INFO.

    ``LOG_LEVEL`` is already validated when the settings load (anything else
    warns and falls back to ``INFO``); this only turns the name into the
    number the logging setup needs.
    """
    return getattr(logging, config.log_level, logging.INFO)


logging.basicConfig(
    level=log_level_value(),
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==============================================================================
# COMMAND LINE
# ==============================================================================

def dry_run_requested(argv) -> bool:
    """Return True when ``--dry-run`` appears on the command line.

    The argument list is passed in (not read from ``sys.argv``) so tests can
    exercise it without touching the real command line.
    """
    return "--dry-run" in argv

# ==============================================================================
# STARTUP SCHEDULE VALIDATION
# ==============================================================================

def validate_schedule_at_startup(path=DEFAULT_SCHEDULE_PATH):
    """Load the schedule file at launch, or refuse to start when it is broken.

    Three outcomes:

    * no file at *path* → return ``None`` and run as before (24/7),
    * a valid file → return the parsed :class:`Schedule`,
    * a broken or empty file → log the fault and raise ``SystemExit`` so a
      typo can never silently turn into 24/7 running.

    This is the one and only place the automator exits on its own (ADR-0002).
    It performs no window, process, resolution, or rotation work.
    """
    try:
        schedule = load_schedule(path)
    except ScheduleError as error:
        logger.error(f"Invalid schedule file '{path}': {error}")
        logger.error(
            "Refusing to start. Fix the schedule file, or remove it to run 24/7."
        )
        raise SystemExit(1)

    if schedule is None:
        logger.debug(f"No schedule file at '{path}'; schedule feature is off.")
    else:
        logger.info(f"Schedule loaded from '{path}'.")
    return schedule


def load_live_schedule(previous, path=DEFAULT_SCHEDULE_PATH):
    """Re-read the schedule file for this monitor cycle.

    Three outcomes, matching :func:`load_schedule` but safe to call while the
    automator is running:

    * no file at *path* → return ``None`` (feature off; a deleted file means
      running 24/7 again),
    * a valid file → return the parsed :class:`Schedule`,
    * a broken file → log the fault loudly and return *previous* (the last
      good schedule). The automator never exits here (ADR-0002).

    The file is read on every cycle so an edit takes effect within one cycle.
    """
    try:
        return load_schedule(path)
    except ScheduleError as error:
        logger.error(f"Invalid schedule file '{path}': {error}")
        logger.error("Keeping the last good schedule. Fix the file to apply the change.")
        return previous

# ==============================================================================
# DPI AWARENESS
# ==============================================================================

def set_dpi_awareness():
    """Make the process DPI-aware so Windows reports real pixels.

    Tries Per-Monitor-V2 first (Windows 10 1703+), then falls back to
    system-aware (older Windows). Logs the outcome.
    """
    try:
        # Per-Monitor V2 (Windows 10 1703+)
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        ctypes.windll.shcore.SetProcessDpiAwarenessContext(-4)
        logger.info("DPI awareness set to Per-Monitor-V2.")
    except Exception:
        try:
            # System-aware fallback (Windows 8.1+)
            # DPI_AWARENESS_PER_MONITOR_AWARE = 2
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            logger.info("DPI awareness set to System-Aware (fallback).")
        except Exception as e:
            logger.warning(f"Could not set DPI awareness: {e}. Coordinates may be virtualized.")

# ==============================================================================
# CORE FUNCTIONS
# ==============================================================================

def is_telegram_running() -> bool:
    """Check if the Telegram process is currently running."""
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and proc.info['name'].lower() == TELEGRAM_EXE.lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return False

def kill_telegram():
    """Terminate the Telegram process."""
    logger.info(f"Attempting to terminate {TELEGRAM_EXE}...")
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and proc.info['name'].lower() == TELEGRAM_EXE.lower():
                proc.terminate()
                proc.wait(timeout=5)
                logger.info(f"{TELEGRAM_EXE} terminated.")
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
        except psutil.TimeoutExpired:
            logger.warning(f"{TELEGRAM_EXE} did not terminate in time, killing...")
            try:
                proc.kill()
            except:
                pass
    return False

def launch_telegram() -> bool:
    """Launch Telegram and wait for the window to appear."""
    logger.info(f"Launching Telegram from: {TELEGRAM_PATH}")
    try:
        subprocess.Popen(TELEGRAM_PATH)
    except Exception as e:
        logger.error(f"Failed to launch Telegram: {e}")
        return False

    # Wait for the process and window to register
    start_time = time.time()
    while time.time() - start_time < LAUNCH_TIMEOUT:
        windows = gw.getWindowsWithTitle(TELEGRAM_TITLE)
        # Filter for exact match or primary window if possible
        target_win = None
        for w in windows:
            if w.title == TELEGRAM_TITLE:
                target_win = w
                break
        
        if target_win:
            logger.info("Telegram window found.")
            time.sleep(ACTION_DELAY) # Give UI a moment to fully render
            return True
        time.sleep(0.5)
        
    logger.error(f"Timeout: Telegram window did not appear after {LAUNCH_TIMEOUT} seconds.")
    return False

def perform_telegram_actions(dry_run: bool = False) -> bool:
    """Resize the Telegram window and perform the click sequence.

    In a dry run the click sequence is limited to its first clicks (see
    :func:`perform_click_sequence`); the resize itself still happens.
    """
    windows = gw.getWindowsWithTitle(TELEGRAM_TITLE)
    target_win = next((w for w in windows if w.title == TELEGRAM_TITLE), None)
    
    if not target_win:
        logger.error("Could not find Telegram window for actions.")
        return False
        
    try:
        # 1. Resize Telegram (scale-compensated)
        config.refresh_scale_factor()
        sf = config.scale_factor
        real_w = int(TELEGRAM_SIZE[0] * sf)
        real_h = int(TELEGRAM_SIZE[1] * sf)
        logger.info(
            f"Resizing Telegram: configured {TELEGRAM_SIZE[0]}x{TELEGRAM_SIZE[1]} "
            f"@ scale {sf} -> real {real_w}x{real_h}..."
        )
        if target_win.isMinimized:
            target_win.restore()
        
        target_win.resizeTo(real_w, real_h)
        target_win.activate() # Bring to front
        time.sleep(ACTION_DELAY)
        
        # 2. Perform Clicks
        return perform_click_sequence(dry_run=dry_run)
    
    except Exception as e:
        logger.error(f"Error during Telegram actions: {e}")
        return False

def perform_click_sequence(dry_run: bool = False) -> bool:
    """Perform the click sequence on the Telegram window without resizing.

    Click targets are in 100%-scale pixel coordinates (from .env).  They are
    multiplied by the current DPI scale factor so that at 150% scaling the
    real pixel position is 1.5× the configured offset, landing on the same
    UI elements as at 100%.  The scale factor is read fresh at the start of
    each run.

    In a dry run only the first ``DRY_RUN_CLICKS`` clicks are performed for
    real; the rest (starting with the third click, the one that opens the
    Target Window) are skipped and the stop is logged.  Nothing else about
    the run changes.
    """
    windows = gw.getWindowsWithTitle(TELEGRAM_TITLE)
    target_win = next((w for w in windows if w.title == TELEGRAM_TITLE), None)
    
    if not target_win:
        logger.error("Could not find Telegram window for click sequence.")
        return False
    
    if not config.click_points:
        logger.warning("No click points configured. Skipping click sequence.")
        return True
    
    try:
        # Read the scale factor fresh for this click run
        config.refresh_scale_factor()
        scale = config.scale_factor
        logger.info(f"Click Sequence Scale Factor: {scale}")
        
        if target_win.isMinimized:
            target_win.restore()
        target_win.activate()
        time.sleep(ACTION_DELAY)
        
        win_x, win_y = target_win.topleft
        logger.info(f"Telegram Window position: ({win_x}, {win_y})")
        
        for idx, (offset_x, offset_y) in enumerate(config.click_points):
            if dry_run and idx >= DRY_RUN_CLICKS:
                logger.info(
                    f"Dry run: stopping before click {idx + 1}/{len(config.click_points)} "
                    f"— that click would open the second window."
                )
                break
            real_x = win_x + offset_x * scale
            real_y = win_y + offset_y * scale
            logger.info(
                f"Action {idx+1}/{len(config.click_points)}: "
                f"Click target ({offset_x}, {offset_y}) * scale {scale} -> "
                f"real pixel ({real_x}, {real_y})"
            )
            
            pyautogui.moveTo(real_x, real_y, duration=0.2)
            pyautogui.click()
            time.sleep(ACTION_DELAY)
        
        return True
    except Exception as e:
        logger.error(f"Error during click sequence: {e}")
        return False

def setup_second_window() -> bool:
    """Find, resize, and move the secondary target window."""
    logger.info(f"Searching for secondary window: '{config.second_window_title}'...")
    windows = gw.getWindowsWithTitle(config.second_window_title)
    
    if not windows:
        logger.error(f"Secondary window ('{config.second_window_title}') not found. Ensure title is correct.")
        return False
        
    try:
        target_win = windows[0] # Assume the first matching window
        
        if target_win.isMinimized:
            target_win.restore()
            
        # If config.second_window_size is None, maximize the window
        if config.second_window_size is None:
            logger.info(f"Maximizing '{config.second_window_title}'...")
            target_win.maximize()
        else:
            sf = config.refresh_scale_factor()
            screen_w, screen_h = config.get_screen_bounds()
            req_w, req_h = config.second_window_size
            real_w = int(req_w * sf)
            real_h = int(req_h * sf)
            logger.info(
                f"Target size: configured {req_w}x{req_h} @ scale {sf} -> real {real_w}x{real_h}"
            )
            clamped_w = min(real_w, screen_w)
            clamped_h = min(real_h, screen_h)
            if clamped_w != real_w or clamped_h != real_h:
                logger.warning(
                    f"Converted size {real_w}x{real_h} exceeds screen bounds "
                    f"{screen_w}x{screen_h}; clamping to {clamped_w}x{clamped_h}."
                )

            req_x, req_y = config.second_window_pos
            real_x = int(req_x * sf)
            real_y = int(req_y * sf)
            logger.info(
                f"Target position: configured ({req_x}, {req_y}) @ scale {sf} -> real ({real_x}, {real_y})"
            )
            logger.info(f"Moving '{config.second_window_title}' to ({real_x}, {real_y})...")
            target_win.moveTo(real_x, real_y)
            time.sleep(ACTION_DELAY)

            logger.info(f"Resizing '{config.second_window_title}' to {clamped_w}x{clamped_h}...")
            target_win.resizeTo(clamped_w, clamped_h)
        time.sleep(ACTION_DELAY)
        
        return True
    except Exception as e:
        logger.error(f"Error adjusting secondary window: {e}")
        return False

# Settle waits and retry budget for each screen-fix move, in today's order of
# magnitude: about a second after a turn, about two after the size call.
SCREEN_FIX_SETTLE_TURN_SECONDS = 1
SCREEN_FIX_SETTLE_SIZE_SECONDS = 2
SCREEN_FIX_MAX_ATTEMPTS = 2


def _attempt_screen_move(action, verify, settle_seconds, description) -> bool:
    """Run one screen move with verify-and-retry; True once verified.

    At most two attempts. Each attempt runs ``action``, waits
    ``settle_seconds`` for the screen to settle, then calls ``verify``; a
    verification that returns True ends the move early. An exception from the
    action or from the verification only fails that attempt — it is logged
    and the next attempt runs. Never raises.
    """
    for attempt in range(1, SCREEN_FIX_MAX_ATTEMPTS + 1):
        try:
            action()
        except Exception as e:
            logger.debug(
                f"Attempt {attempt}/{SCREEN_FIX_MAX_ATTEMPTS} of "
                f"{description} failed: {e}"
            )

        time.sleep(settle_seconds)

        try:
            if verify():
                return True
        except Exception as e:
            logger.debug(
                f"Read-back after attempt {attempt}/{SCREEN_FIX_MAX_ATTEMPTS} "
                f"of {description} failed: {e}"
            )

    return False


def ensure_correct_screen(expected_size, expected_rotation, display, set_screen_size, read_screen_size) -> None:
    """Check the primary screen's Rotation and size and fix them when off.

    The whole screen fix lives here. It reads the current Rotation and the
    current size; when both already match the configuration it returns at
    once without touching the screen (no flicker), logging at debug level
    only. When either is off it warns and runs three moves in this fixed
    order:

    1. turn the screen to Rotation 0 (always issued, even as a no-op),
    2. set the configured size through the size tool — size only, never an
       orientation flag — while the screen is at Rotation 0,
    3. turn the screen to the configured Rotation (always issued).

    Every move gets at most two attempts; after each attempt the screen is
    read back and a match ends the move early. A failed first turn stops the
    fix for this cycle: the size is never set on a sideways screen. A failed
    size move still lets the final turn run, so the screen is never left
    sideways. Nothing raises out of this function — the process never exits.

    ``expected_size`` is the configured ``(width, height)`` exactly as
    written. It is compared with the OS-reported size with width and height
    swapped when ``expected_rotation`` is 90 or 270 (the OS reports the
    rotated size); the swap is comparison-only — the size is always *set*
    from the configured pair as written, at Rotation 0.
    """
    expected_width, expected_height = expected_size

    # Read the current state. When the screen cannot be read, leave it alone.
    try:
        current_rotation = display.current_orientation
        current_width, current_height = read_screen_size()
    except Exception as e:
        logger.warning(
            f"Could not read the current screen state: {e}. "
            f"Skipping the screen check this cycle."
        )
        return

    # The OS reports the size rotated with the screen at 90 and 270, so the
    # expected size is swapped for comparison only.
    if expected_rotation in (90, 270):
        expected_screen_size = (expected_height, expected_width)
    else:
        expected_screen_size = (expected_width, expected_height)

    rotation_correct = current_rotation == expected_rotation
    size_correct = (current_width, current_height) == expected_screen_size

    if rotation_correct and size_correct:
        logger.debug(
            f"Screen is correct (rotation {current_rotation} deg, "
            f"size {current_width}x{current_height})."
        )
        return

    if not rotation_correct:
        logger.warning(
            f"Rotation mismatch: current {current_rotation} deg, "
            f"expected {expected_rotation} deg. Fixing..."
        )
    if not size_correct:
        logger.warning(
            f"Resolution mismatch: current {current_width}x{current_height}, "
            f"expected {expected_screen_size[0]}x{expected_screen_size[1]} "
            f"(rotation {expected_rotation}). Fixing..."
        )

    # Move 1: turn to Rotation 0. Issued even when already there, so every fix
    # takes the same known-good path and the size is never set sideways.
    if _attempt_screen_move(
        lambda: display.rotate_to(0),
        lambda: display.current_orientation == 0,
        SCREEN_FIX_SETTLE_TURN_SECONDS,
        "turning the screen to Rotation 0",
    ):
        logger.info("Rotation corrected to 0 deg.")
    else:
        logger.warning(
            "Could not turn the screen to Rotation 0; skipping the screen "
            "fix this cycle. Will retry next cycle."
        )
        return

    # Move 2: set the configured size while at Rotation 0. Size only — never
    # an orientation flag. A failure here still lets the final turn run.
    if _attempt_screen_move(
        lambda: set_screen_size(expected_width, expected_height),
        lambda: read_screen_size() == (expected_width, expected_height),
        SCREEN_FIX_SETTLE_SIZE_SECONDS,
        f"setting the Resolution to {expected_width}x{expected_height}",
    ):
        logger.info(f"Resolution corrected to {expected_width}x{expected_height}.")
    else:
        logger.warning(
            f"Resolution still not {expected_width}x{expected_height} after "
            f"{SCREEN_FIX_MAX_ATTEMPTS} attempts; will retry next cycle."
        )

    # Move 3: turn to the configured Rotation. A failure leaves the next cycle
    # to retry; nothing raises.
    if _attempt_screen_move(
        lambda: display.rotate_to(expected_rotation),
        lambda: display.current_orientation == expected_rotation,
        SCREEN_FIX_SETTLE_TURN_SECONDS,
        f"turning the screen to Rotation {expected_rotation}",
    ):
        logger.info(f"Rotation corrected to {expected_rotation} deg.")
    else:
        logger.warning(
            f"Rotation still not {expected_rotation} deg after "
            f"{SCREEN_FIX_MAX_ATTEMPTS} attempts; will retry next cycle."
        )


def set_screen_size(width, height):
    """Set the primary screen's size through the bundled resolution tool.

    Runs SetResolution.exe exactly as before: no prompt, and size only —
    never an orientation flag (the tool's orientation option is broken
    upstream; Rotation is handled separately through the rotation library).
    May raise on failure; the caller treats that as a failed attempt.
    """
    exe_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SetResolution.exe")
    subprocess.run(
        ['powershell', f'& "{exe_path}" SET -w {width} -h {height} -noprompt'],
        check=True,
    )


def read_screen_size():
    """Return the OS-reported primary screen size as ``(width, height)``."""
    user32 = ctypes.windll.user32
    return (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))

# ==============================================================================
# RECORDING
# ==============================================================================
# Recording lives inside the automator: one recorder child, driven from the
# monitor cycle. It is independent of the windows — Recovery, Click Retry, and
# the screen fix never touch it. Recording faults are logged and never fail the
# window job.

# The one recorder child, kept at module level so it survives across monitor
# cycles. It is created on first use through :func:`get_recorder`, so tests can
# replace the accessor (or this module variable) with a fake instead of
# spawning a real recorder program.
_recorder = None

# Disk guard memory: whether a Disk Stop is in force, and the last free-space
# level seen, so the "low disk" warning is logged once per crossing into
# ``warn`` rather than every cycle.
_recording_disk_latched = False
_recording_disk_level = None


def get_recorder(settings) -> Recorder:
    """Return the module-level recorder, creating it on first use.

    The caller supplies the already-resolved settings the first time; later
    calls reuse the same recorder. Tests replace this function (or
    ``_recorder``) with a fake so no recorder program is ever spawned.
    """
    global _recorder
    if _recorder is None:
        _recorder = Recorder(settings)
    return _recorder


def resolve_recordings_dir() -> str:
    """Return the configured Clips folder as an absolute path.

    This is a thin accessor: the value is already absolute and validated when
    the settings load (a relative or empty value there falls back to the
    default), so it is returned exactly as configured — the program never
    joins a relative value onto its own folder.
    """
    return config.recordings_dir


def _build_recorder_settings(folder) -> RecorderSettings:
    """Collect the recording values into the recorder's settings object."""
    return RecorderSettings(
        stream_url=config.record_stream_url,
        output_dir=folder,
        clip_seconds=config.record_clip_seconds,
        reconnect_seconds=config.record_reconnect_seconds,
        ffmpeg_path=config.record_ffmpeg_path,
    )


def _nearest_existing_folder(folder) -> str:
    """Return *folder*, or its nearest existing ancestor when it is missing."""
    current = os.path.abspath(folder)
    while not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current


def _free_space_bytes(folder) -> int:
    """Return the free space in bytes on the drive that holds *folder*.

    The Clips folder may not exist yet, so the nearest existing parent is used
    for the disk reading.
    """
    return shutil.disk_usage(_nearest_existing_folder(folder)).free


def _free_gb(free_bytes: int) -> str:
    """Format a byte count as gigabytes for a log line."""
    return f"{free_bytes / (1024 ** 3):.1f}"


def _should_record(moment, schedule) -> bool:
    """Return True when the Recording Mode and the Schedule want recording now.

    ``off`` never records; ``long-living`` always does and ignores the Schedule
    entirely (so it keeps recording while the automator is in Suspend); and
    ``follow-schedule`` records when there is no Schedule file or when the
    moment is inside an Allowed Period.
    """
    mode = config.recording_mode
    if mode == "long-living":
        return True
    if mode == "follow-schedule":
        return schedule is None or schedule.is_allowed(moment)
    return False


def update_recording(moment, schedule, dry_run=False) -> None:
    """Bring recording in line with the mode, the Schedule, and free space.

    Called at the very top of every monitor cycle, before the schedule gate and
    before any early return, so Long-living keeps recording during Suspend.

    A dry run returns at once and never touches the recorder: the rehearsal is
    window work only.

    While recording is wanted the disk guard reads free space on the Clips
    folder drive, warns once per crossing into ``warn``, and stops the recorder
    once when it enters ``stop``. A Disk Stop suppresses starts and reconnects
    until free space is back at or above the warning level, then resumes with
    one log line. No Clip is ever deleted.
    """
    global _recorder, _recording_disk_latched, _recording_disk_level

    if dry_run:
        return

    try:
        if not _should_record(moment, schedule):
            # Mode off, or Follow-schedule outside its Allowed Period:
            # Recording Stop when it was running, and nothing started.
            if _recorder is not None and _recorder.is_running():
                _recorder.stop()
            _recorder = None
            _recording_disk_latched = False
            _recording_disk_level = None
            return

        folder = resolve_recordings_dir()
        settings = _build_recorder_settings(folder)
        free_bytes = _free_space_bytes(folder)
        warn_bytes = int(config.record_disk_warn_gb * 1024 ** 3)
        stop_bytes = int(config.record_disk_stop_gb * 1024 ** 3)
        level = classify_free_space(free_bytes, warn_bytes, stop_bytes)

        if _recording_disk_latched:
            if level != "ok":
                # Still below the warning level: keep suppressing starts and
                # reconnects until the space is back.
                return
            logger.info("Free space is back at the warning level; resuming recording.")
            _recording_disk_latched = False
        elif level == "stop":
            _recording_disk_latched = True
            _recording_disk_level = level
            logger.error(
                f"Disk space is critically low ({_free_gb(free_bytes)} GB free on "
                f"'{folder}'); stopping recording so the disk never fills. "
                f"No Clip is deleted."
            )
            if _recorder is not None:
                _recorder.stop_for_disk()
                _recorder = None
            return
        elif level == "warn" and _recording_disk_level != "warn":
            logger.warning(
                f"Low disk space: only {_free_gb(free_bytes)} GB free on "
                f"'{folder}'. Recording stops below "
                f"{config.record_disk_stop_gb:g} GB."
            )

        _recording_disk_level = level

        if _recorder is None:
            # First start (or a retry after a failed start): build the recorder
            # and start it. A failed start is logged and retried next cycle.
            recorder = get_recorder(settings)
            if not recorder.start(moment):
                _recorder = None
        else:
            # Already in the recording phase: let the recorder reconnect a
            # dropped stream after the configured wait.
            _recorder.supervise(moment)
    except Exception as error:
        logger.error(f"Unexpected problem while updating recording: {error}")


def log_recording_startup() -> None:
    """Log the Recording Mode once and name any reason recording cannot run.

    A missing stream address or a missing recorder program logs an error. The
    automator never exits or refuses to start; the window job keeps running.
    """
    mode = config.recording_mode
    logger.info(f"Recording mode: {mode}.")
    if mode == "off":
        return

    if not config.record_stream_url:
        logger.error(
            f"Recording mode is '{mode}' but RECORD_STREAM_URL is empty; "
            f"recording cannot start. The window job keeps running."
        )

    settings = _build_recorder_settings(resolve_recordings_dir())
    if not ffmpeg_available(settings):
        logger.error(
            f"Recording mode is '{mode}' but the recorder program "
            f"'{config.record_ffmpeg_path}' was not found; recording cannot "
            f"start. The window job keeps running."
        )


def stop_recording() -> None:
    """Stop the recorder gracefully when the user stops the automator.

    Called where the main loop notices Ctrl+C, so no orphan recorder child
    survives the exit.
    """
    if _recorder is not None:
        _recorder.stop()

# ==============================================================================
# MAIN LOOP
# ==============================================================================

# Last scale factor logged by the monitor cycle. Kept at module level so the
# "only log when it changes" behaviour survives the move of the cycle body
# into a reusable function.
_last_scale_factor = None


def has_room_to_launch(schedule, moment) -> bool:
    """Return True when a launch started now still fits in the Allowed Period.

    The guard is off (always True) when there is no schedule. With a schedule,
    True means the period still covers ``moment`` plus ``LAUNCH_TIMEOUT``, so a
    Setup Sequence started now will not be followed by a Shutdown seconds
    later. The caller supplies ``moment``; this helper never reads the clock.
    """
    if schedule is None:
        return True
    return schedule.is_allowed(moment + datetime.timedelta(seconds=LAUNCH_TIMEOUT))


def run_monitor_cycle(schedule, moment=None, dry_run=False):
    """Run exactly one monitor cycle.

    A cycle is the smallest repeatable unit of work the monitor performs. It
    holds the schedule gate, the screen guard, and the window management. It
    never sleeps ``MONITOR_INTERVAL`` and never exits the process; the outer
    :func:`main` loop sleeps between cycles.

    Recording is updated at the very top, before the schedule gate and every
    early return, so Long-living keeps recording while the automator is in
    Suspend. The recording decision is independent of the windows and of any
    window work that follows.

    The schedule gate sits just below the recording update, before the scale
    refresh, the screen guard, and any window work:

    * inside an Allowed Period → behave exactly as before,
    * outside every Allowed Period → perform a Shutdown when the Primary
      Window is running (the proven hard kill; the Target Window closes with
      it) and skip the rest of this cycle (Suspend).

    A launch guard sits just below the gate: inside an Allowed Period that ends
    within the launch window (``LAUNCH_TIMEOUT``) and with the Primary Window
    absent, the cycle waits quietly instead of starting a Setup Sequence it
    would have to tear down seconds later. The wait skips the screen guard and
    all window work; the next cycle re-checks the clock.

    ``moment`` is the local clock reading used by the gate. When it is
    ``None`` the current time is read here; the schedule checker itself never
    reads the clock.

    In a dry run (``dry_run=True``) the cycle runs the screen fix, closes a
    running Primary Window for a fresh start (returning ``None`` so the next
    cycle relaunches it), and on a fresh start runs the setup up to the second
    click only. A finished rehearsal returns ``"rehearsed"``; a closed
    schedule gate returns ``"blocked"``. Normal operation always returns
    ``None``.
    """
    global _last_scale_factor

    if moment is None:
        moment = datetime.datetime.now()

    # Recording comes first, before the schedule gate and every early return:
    # Long-living must keep recording while the automator is in Suspend.
    update_recording(moment, schedule, dry_run)

    # Schedule gate: Suspend (and Shutdown when needed) outside allowed hours.
    if schedule is not None and not schedule.is_allowed(moment):
        if is_telegram_running():
            logger.info("Outside the allowed period; shutting down Telegram.")
            kill_telegram()
        else:
            logger.debug("Outside the allowed period; Telegram is not running.")
        return "blocked" if dry_run else None

    # Launch guard: near the end of an Allowed Period, wait quietly rather than
    # launch Telegram only to shut it down seconds later. Only the launch (with
    # the Primary Window absent) is guarded; a running window is left alone.
    # ``telegram_running`` is remembered so the process check is not repeated.
    telegram_running = None
    if schedule is not None and not has_room_to_launch(schedule, moment):
        if not is_telegram_running():
            logger.info(
                "Allowed period ends within the launch window; waiting quietly "
                "instead of launching Telegram."
            )
            return
        telegram_running = True

    # Re-read scale factor each cycle; only log when it changes
    config.refresh_scale_factor()
    if config.scale_factor != _last_scale_factor:
        logger.info(f"Scale Factor: {config.scale_factor}")
        _last_scale_factor = config.scale_factor

    # Fix the screen (Rotation and Resolution) before any window work.
    ensure_correct_screen(
        config.screen_resolution,
        config.screen_rotation,
        rotatescreen.get_primary_display(),
        set_screen_size,
        read_screen_size,
    )

    if telegram_running is None:
        telegram_running = is_telegram_running()

    if not telegram_running:
        logger.warning(f"Process {TELEGRAM_EXE} not found. Initiating setup sequence...")

        # 1. Launch Telegram
        if not launch_telegram():
            logger.error("Failed to launch Telegram. Re-checking in next cycle.")
            return

        # 2. Automate Telegram window (resize, clicks)
        if not perform_telegram_actions(dry_run=dry_run):
            logger.error("Failed to complete Telegram actions. Re-checking in next cycle.")
            return

        if dry_run:
            logger.info(
                "Dry run: rehearsal finished — the first two clicks were performed; "
                "stopping before the third click and skipping all second-window work."
            )
            return "rehearsed"

        # 3. Setup Second Window
        if not setup_second_window():
            logger.warning("Secondary window not found after setup. Killing Telegram and waiting %d seconds before retry...", config.kill_cooldown_seconds)
            kill_telegram()
            time.sleep(config.kill_cooldown_seconds)
            return

        logger.info("Setup sequence completed successfully. Entering monitoring mode.")

    else:
        if dry_run:
            # A rehearsal needs a fresh start (the clicks only run then);
            # close the running Primary Window and try again next cycle.
            logger.info(
                "Dry run: Telegram is running; closing it for a fresh rehearsal."
            )
            kill_telegram()
            return

        # Telegram IS running. Now we must verify the second window is also present.
        # If it's missing, re-run the Click Sequence up to the retry limit.
        windows = gw.getWindowsWithTitle(config.second_window_title)
        if not windows:
            logger.warning(f"Telegram is running but secondary window '{config.second_window_title}' is missing.")
            retry_count = 0
            while retry_count < config.second_window_retry_limit:
                logger.info(f"Click Retry attempt {retry_count + 1}/{config.second_window_retry_limit}")
                perform_click_sequence()
                time.sleep(config.click_retry_interval)
                windows = gw.getWindowsWithTitle(config.second_window_title)
                if windows:
                    logger.info(f"Target window found after {retry_count + 1} attempt(s).")
                    break
                retry_count += 1

            if not windows:
                logger.warning("Click Retry limit exhausted. Initiating Recovery...")
                kill_telegram()
                time.sleep(config.kill_cooldown_seconds)
                return
            else:
                # Window found after retry, apply size/position settings
                if not setup_second_window():
                    logger.warning("Failed to setup second window after click retry.")


def run_dry_run(schedule):
    """Run the rehearsal until it is done, then stop with a summary.

    One rehearsal per run: the screen fix runs for real, Telegram is closed
    first when already running, then launched and resized, and the first two
    clicks are performed for real. The third click (the one that opens the
    Target Window) and every second-window action are skipped. Outside the
    allowed hours nothing is rehearsed. Telegram is left running.
    """
    logger.info(
        "Dry run: real actions until the second click; the third click and all "
        "second-window work are skipped."
    )

    while True:
        outcome = run_monitor_cycle(schedule, dry_run=True)

        if outcome == "rehearsed":
            logger.info(
                "Dry run complete: Telegram was launched and resized and the first "
                "two clicks were performed. Telegram is left running; the third "
                "click and the second window were skipped on purpose."
            )
            return

        if outcome == "blocked":
            logger.info(
                "Dry run: outside the allowed period; nothing was rehearsed."
            )
            return

        time.sleep(MONITOR_INTERVAL)


def main():
    logger.info("Starting Telegram Monitor Service...")
    schedule = validate_schedule_at_startup()
    set_dpi_awareness()
    log_recording_startup()

    if dry_run_requested(sys.argv[1:]):
        try:
            run_dry_run(schedule)
        except KeyboardInterrupt:
            logger.info("Dry run stopped by user.")
        return

    while True:
        try:
            # Re-read the schedule every cycle so an edit applies within a cycle
            schedule = load_live_schedule(schedule)
            run_monitor_cycle(schedule)

            # Sleep until the next check
            time.sleep(MONITOR_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Monitor Service stopped by user.")
            stop_recording()
            break
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
            time.sleep(MONITOR_INTERVAL)

if __name__ == "__main__":
    main()
