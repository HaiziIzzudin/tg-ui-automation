import os
import sys
import time
import datetime
import subprocess
import shutil
import logging
import signal
import pyautogui
import pygetwindow as gw
import psutil
import ctypes
import rotatescreen
from dotenv import load_dotenv
from config import Config
from schedule import SCHEDULE_FILENAME, ScheduleError, load_schedule
import recorder
from recorder import (
    Recorder,
    RecorderSettings,
    classify_free_space,
    ffmpeg_available,
)
from broadcaster import BroadcastSettings, Broadcaster, check_report

# Load environment variables from .env file
load_dotenv()

# ==============================================================================
# CONFIGURATION
# ==============================================================================
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
# THE TELEGRAM PROGRAM AND THE CLIPS FOLDER
# ==============================================================================
# The Telegram program file is resolved once, at launch, from the settings;
# everything that finds, launches, or kills Telegram keys on that one resolved
# path. One cheap file-exists test per cycle refuses all window work while the
# file is missing, and speaks about it only when the answer changes.

def telegram_program_path() -> str:
    """Return the Telegram program file named by the settings."""
    return config.telegram_path


def telegram_process_name() -> str:
    """Return the Telegram program's file name, taken from the resolved path."""
    return os.path.basename(telegram_program_path())


def telegram_program_present() -> bool:
    """Return True when the resolved Telegram program is an existing file."""
    return os.path.isfile(telegram_program_path())


def log_startup_locations() -> None:
    """Name the Telegram program and the Clips folder once, at launch.

    Two lines, in this order, right after the DPI line and immediately before
    the Recording Mode line, on a real run and a rehearsal alike. Neither line
    has quotes and neither ends with a full stop.
    """
    logger.info(f"Telegram program: {telegram_program_path()}")
    logger.info(
        f"Clips folder: {resolve_recordings_dir()} "
        f"(each Listed Person records into a folder of their own inside it)"
    )


# Whether the last cycle found the program missing, so the log speaks once at
# the loss and once at the return instead of every three seconds.
_telegram_program_missing = False


def check_telegram_program() -> bool:
    """Return True when window work may run this cycle.

    One cheap file-exists test against the resolved path, done after the
    schedule gate and the launch guard and before any process check or window
    work. While the file is not an existing file the answer is False and every
    window action is refused; the screen fix, broadcasting, recording, and the
    List Recorder never read this path and carry on.

    The log speaks on change only: one error when the file goes missing,
    silence while it stays missing, one line when it comes back, and the error
    again at a later loss.
    """
    global _telegram_program_missing

    path = telegram_program_path()
    if not telegram_program_present():
        if not _telegram_program_missing:
            logger.error(
                f"The Telegram program '{path}' (TELEGRAM_PATH) was not found; "
                f"no Telegram or window work until it is back. Recording and "
                f"broadcasting are unaffected."
            )
            _telegram_program_missing = True
        return False

    if _telegram_program_missing:
        logger.info(f"The Telegram program '{path}' is back; window work resumes.")
        _telegram_program_missing = False
    return True


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
    """Check if the Telegram process is currently running.

    The process name is the file name of the resolved program path, so a copy
    with another name is found, restarted, and killed like the usual one.
    """
    process_name = telegram_process_name()
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and proc.info['name'].lower() == process_name.lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return False

def kill_telegram():
    """Terminate the Telegram process."""
    process_name = telegram_process_name()
    logger.info(f"Attempting to terminate {process_name}...")
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and proc.info['name'].lower() == process_name.lower():
                proc.terminate()
                proc.wait(timeout=5)
                logger.info(f"{process_name} terminated.")
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
        except psutil.TimeoutExpired:
            logger.warning(f"{process_name} did not terminate in time, killing...")
            try:
                proc.kill()
            except:
                pass
    return False

def launch_telegram() -> bool:
    """Launch Telegram and wait for the window to appear."""
    path = telegram_program_path()
    logger.info(f"Launching Telegram from: {path}")
    try:
        subprocess.Popen(path)
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
# BROADCASTING
# ==============================================================================
# Broadcasting lives inside the automator too: the whole screen is pushed out
# through OBS, driven from the monitor cycle. It shares nothing with recording
# and nothing with the windows — Recovery, Click Retry, and the screen fix never
# touch it. Broadcast faults are logged and never fail the window job.

# The one broadcaster, kept at module level so it survives across monitor
# cycles. It is created on first use through :func:`get_broadcaster`, so tests
# can replace the accessor (or this module variable) with a fake instead of
# talking to a real OBS.
_broadcaster = None


def get_broadcaster(settings) -> Broadcaster:
    """Return the module-level broadcaster, creating it on first use.

    The caller supplies the already-resolved settings the first time; later
    calls reuse the same broadcaster, so its Restart Budget and its last known
    stream state survive across monitor cycles. Tests replace this function (or
    ``_broadcaster``) with a fake so no OBS is ever opened.
    """
    global _broadcaster
    if _broadcaster is None:
        _broadcaster = Broadcaster(settings)
    return _broadcaster


def _build_broadcast_settings() -> BroadcastSettings:
    """Collect the broadcasting values into the broadcaster's settings object.

    The four tuning fields of ``BroadcastSettings`` keep their defaults: they
    are for tests, never for the user, so the settings file carries only the
    five values below.
    """
    return BroadcastSettings(
        enabled=config.broadcast_enabled,
        host=config.broadcast_obs_host,
        port=config.broadcast_obs_port,
        password=config.broadcast_obs_password,
        obs_path=config.broadcast_obs_path,
    )


def prepare_broadcast(dry_run=False) -> None:
    """Announce the Broadcast Switch once and open OBS when broadcasting is on.

    Called once, at startup, right after the recording line. Outside an Allowed
    Period OBS is still opened and left idle in the tray: the choice is "open
    once, when the automator starts". A rehearsal opens nothing. A fault inside
    the broadcaster is logged and the run carries on.
    """
    try:
        broadcaster = get_broadcaster(_build_broadcast_settings())
        broadcaster.prepare(dry_run)
    except Exception as error:
        logger.error(f"Unexpected problem while preparing broadcasting: {error}")


def update_broadcast(moment, schedule, dry_run=False) -> None:
    """Bring the stream in line with the clock, the Schedule, and the switch.

    Called every monitor cycle, immediately after the screen fix and before any
    Telegram work, so the picture is already settled when a stream starts. This
    is the only place a stream is started.

    A stream is wanted only when this is not a rehearsal, the Broadcast Switch
    is on, and there is no Schedule file or the moment is inside an Allowed
    Period. Nothing else influences it — not the Recording Mode, not the List
    Recorder, not the windows. A fault inside the broadcaster is logged and the
    cycle carries on.
    """
    try:
        wanted = (
            (not dry_run)
            and config.broadcast_enabled
            and (schedule is None or schedule.is_allowed(moment))
        )
        broadcaster = get_broadcaster(_build_broadcast_settings())
        broadcaster.update(moment, wanted)
    except Exception as error:
        logger.error(f"Unexpected problem while updating broadcasting: {error}")


def stop_broadcast() -> None:
    """Stop a live stream, so no stream outlives the automator's own run.

    Called where the cycle finds itself outside an Allowed Period (which also
    cleans up a stream left behind by a crashed run) and where the main loop
    notices Ctrl+C. Safe to call when no broadcaster was ever created. A fault
    inside the broadcaster is logged and the caller carries on.
    """
    if _broadcaster is None:
        return
    try:
        _broadcaster.stop()
    except Exception as error:
        logger.error(f"Unexpected problem while stopping broadcasting: {error}")


def check_obs_requested(argv) -> bool:
    """Return True when ``--check-obs`` appears on the command line.

    The argument list is passed in (not read from ``sys.argv``) for the same
    reason as :func:`dry_run_requested`: tests never touch the real command
    line.
    """
    return "--check-obs" in argv


def print_obs_check(schedule) -> int:
    """Report whether this machine is ready to broadcast, and return an exit code.

    Read-only on purpose: nothing is opened, started, or stopped, and no window,
    screen, or rotation is touched, so it is safe to run on a machine that is
    mid-shift. Six lines are written through logging, so they land on the
    console and in the log file like everything else:

    1. whether the Broadcast Switch is on,
    2. whether the OBS program file is there, whether a password is set (yes or
       no, never the value), whether OBS is running, and what OBS answers about
       the stream,
    3. whether broadcasting would be live at this moment, given the clock and
       the Schedule (no schedule file means always allowed).

    ``schedule`` is the loaded Schedule, or ``None`` when there is no schedule
    file. Returns ``0`` when OBS answered and ``1`` when it did not.
    """
    logger.info(
        f"Broadcast switch: {'on' if config.broadcast_enabled else 'off'}."
    )

    reached = False
    try:
        report_lines, reached = check_report(_build_broadcast_settings())
    except Exception as error:
        report_lines = []
        logger.error(f"Unexpected problem while checking OBS: {error}")
    for line in report_lines:
        logger.info(line)

    now = datetime.datetime.now()
    inside_period = schedule is None or schedule.is_allowed(now)
    if not config.broadcast_enabled:
        logger.info("Would be live now: no (the broadcast switch is off).")
    elif not inside_period:
        logger.info("Would be live now: no (the clock is outside every allowed period).")
    else:
        logger.info("Would be live now: yes (the switch is on and the clock is inside an allowed period).")

    return 0 if reached else 1

# ==============================================================================
# THE LIST RECORDER
# ==============================================================================
# The automator owns the List Recorder while the List Recorder Switch is on:
# it adopts a leftover copy at startup, starts one when none is running, keeps
# it alive, and stops it on the way out. Four events reach this log — a start,
# an adoption, a death with its restart, and a stop — and nothing while
# everything is steady. The helper's own chatter stays in its own log.
#
# With the switch off the automator takes no part at all: no scan, no adoption,
# no start, no restart, and no stop, not even on Ctrl+C. A rehearsal starts
# nothing either and still says which switch it is.

# The helper program sits next to the automator's own file; no setting names it.
LIST_RECORDER_PROGRAM = "record_streams.py"

# A death is followed by a fixed wait, then a restart, forever. The stop on the
# way out gives the helper the same ten seconds to close its open Clips.
LIST_RECORDER_RESTART_SECONDS = 10.0
LIST_RECORDER_STOP_GRACE_SECONDS = 10.0
LIST_RECORDER_KILL_GRACE_SECONDS = 3.0

# The one supervision object, kept at module level so it survives across monitor
# cycles. It stays ``None`` when the switch is off and in a rehearsal, so every
# helper function below is a quiet no-op there.
_list_recorder = None


def list_recorder_program_path() -> str:
    """Return the helper program file: record_streams.py next to this file."""
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), LIST_RECORDER_PROGRAM
    )


def find_list_recorder_process():
    """Return a running List Recorder, or None. One process-table scan.

    The match is the helper program's file name inside the process's command
    line, the same way Telegram and OBS are already found by name. No pid file
    and no lock file: this also finds a copy the user started by hand.
    """
    program_name = os.path.basename(list_recorder_program_path()).lower()
    try:
        processes = psutil.process_iter(["name", "cmdline"])
    except Exception as error:
        logger.debug(f"Could not read the process table: {error}")
        return None

    for proc in processes:
        try:
            parts = proc.info.get("cmdline") or []
            for part in parts:
                if part and os.path.basename(part).lower() == program_name:
                    return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return None


def launch_list_recorder_process(program_path=None, interpreter=None):
    """Start the helper: same interpreter, its own process group, no console.

    Its standard output and error are discarded, so its chatter never reaches
    the automator's console or log; its own log file is its record.
    """
    command = [
        interpreter or sys.executable,
        program_path or list_recorder_program_path(),
    ]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        command,
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def send_console_stop_signal(pid) -> bool:
    """Ask the helper's process group to stop, the way Ctrl+C would ask it.

    The helper handles that signal by running its ordinary shutdown, which
    closes every open Clip and exits with code 0. Returns False when the signal
    cannot be delivered; the caller then terminates and kills instead.
    """
    stop_signal = getattr(signal, "CTRL_BREAK_EVENT", None)
    if stop_signal is None or pid is None:
        return False
    try:
        os.kill(pid, stop_signal)
        return True
    except Exception as error:
        logger.debug(f"Could not ask the List Recorder to stop: {error}")
        return False


def job_object_protection_available() -> bool:
    """Return True when the engine ends recorder children with their program.

    The engine gained that protection after this program did, so the check is
    optional: an engine without it means the unsure line after a forced kill.
    """
    checker = getattr(recorder, "job_object_available", None)
    if checker is None:
        return False
    try:
        return bool(checker())
    except Exception:
        return False


class ListRecorderSupervisor:
    """The one List Recorder the automator owns: adopt, start, restart, stop.

    A death is noticed from the handle kept at launch — the child's ``poll()``
    or the adopted process's own running state — never from a second
    process-table scan. A child the automator itself ended is not a death: the
    stop request is remembered before any wait begins.

    The seams (how to spawn, how to ask it to stop, the clock, the sleep) are
    injected so tests never start a real child.
    """

    POLL_INTERVAL_SECONDS = 0.1

    def __init__(
        self,
        program_path=None,
        interpreter=None,
        spawn=None,
        ask=None,
        clock=None,
        sleep=None,
        restart_seconds=LIST_RECORDER_RESTART_SECONDS,
        stop_grace_seconds=LIST_RECORDER_STOP_GRACE_SECONDS,
        kill_grace_seconds=LIST_RECORDER_KILL_GRACE_SECONDS,
    ):
        self.program_path = program_path or list_recorder_program_path()
        self.interpreter = interpreter or sys.executable
        self.restart_seconds = restart_seconds
        self.stop_grace_seconds = stop_grace_seconds
        self.kill_grace_seconds = kill_grace_seconds
        self._spawn = spawn or launch_list_recorder_process
        self._ask = ask or send_console_stop_signal
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        # The handle we keep: our own child, or an adopted process. Never both.
        self._child = None
        self._adopted = None
        # Set before any wait begins, so a child we ended is never a death.
        self._stop_requested = False
        self._restart_at = 0.0
        self._started_once = False
        self._death_warned = False

    # -- what we know ---------------------------------------------------

    def is_running(self) -> bool:
        """Return True while the helper we own is still there."""
        if self._adopted is not None:
            try:
                return bool(self._adopted.is_running())
            except Exception:
                return False
        if self._child is not None:
            return self._child.poll() is None
        return False

    def _pid(self):
        """Return the helper's process id, or None when nothing is known."""
        if self._adopted is not None:
            return getattr(self._adopted, "pid", None)
        if self._child is not None:
            return getattr(self._child, "pid", None)
        return None

    def _forget(self) -> None:
        self._child = None
        self._adopted = None

    # -- the small verbs ------------------------------------------------

    def adopt(self, process) -> None:
        """Take over an outside List Recorder found at startup, once."""
        self._adopted = process
        self._child = None
        # It is ours now, so a replacement after its death reads as a restart.
        self._started_once = True
        logger.info(
            "A List Recorder was already running; watching it instead of "
            "starting a second one."
        )

    def ensure_running(self) -> bool:
        """Keep the helper alive: notice a death, wait, then launch again.

        Called at the very top of every monitor cycle, before the recording
        update and the schedule gate, so Suspend and any Recording Mode leave
        the helper running. Returns True when a helper is running at the end.
        Never raises: a failed launch takes the death path.
        """
        if self._stop_requested:
            return False

        if self.is_running():
            return True

        if self._child is not None or self._adopted is not None:
            # It died while the switch still wants one.
            self._forget()
            self._restart_at = self._clock() + self.restart_seconds
            self._log_death()
            return False

        if self._clock() < self._restart_at:
            return False

        return self.start()

    def start(self) -> bool:
        """Launch the helper once. A failed launch is the death path."""
        try:
            child = self._spawn(self.program_path, self.interpreter)
        except Exception as error:
            self._child = None
            self._restart_at = self._clock() + self.restart_seconds
            self._log_death(error)
            return False

        self._child = child
        self._adopted = None
        if self._started_once:
            logger.info("List Recorder restarted.")
        else:
            logger.info("List Recorder started.")
        self._started_once = True
        return True

    def _log_death(self, error=None) -> None:
        """Speak once per run at full volume; repeats stay at debug."""
        line = "List Recorder is no longer running; restarting it in 10 seconds."
        if self._death_warned:
            logger.debug(line)
        else:
            logger.warning(line)
            self._death_warned = True
        if error is not None:
            logger.debug(f"Could not start the List Recorder: {error}")

    # -- the way out ----------------------------------------------------

    def stop(self) -> None:
        """Ask the helper to stop, then terminate, then kill it.

        The ask is the console's own stop signal to its process group, with ten
        seconds to close every open Clip. Every step says what happened; a
        forced kill is not a lost Clip because the fragment flags leave every
        Clip playable. Never raises.
        """
        if self._stop_requested:
            return
        self._stop_requested = True

        if not self.is_running():
            self._forget()
            return

        pid = self._pid()
        asked = self._ask(pid)
        if asked and self._wait_for_exit(self.stop_grace_seconds):
            logger.info("List Recorder stopped; every open Clip is closed.")
            self._forget()
            return

        logger.info(
            "List Recorder did not stop within 10 seconds; terminating it — "
            "open Clips may be cut."
        )
        self._terminate()
        if self._wait_for_exit(self.kill_grace_seconds):
            self._forget()
            return

        logger.info("List Recorder still running; killing it.")
        self._kill()
        if not self._wait_for_exit(self.kill_grace_seconds):
            logger.debug("The List Recorder could not be killed.")
            self._forget()
            return

        if job_object_protection_available():
            logger.info("The List Recorder was killed; its recorders ended with it.")
        else:
            logger.info(
                "The List Recorder was killed; any List Recordings it owned may "
                "still be running."
            )
        self._forget()

    def _wait_for_exit(self, seconds) -> bool:
        """Wait up to *seconds* for the helper to leave; True when it left."""
        deadline = self._clock() + seconds
        while True:
            if not self.is_running():
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(self.POLL_INTERVAL_SECONDS)

    def _terminate(self) -> None:
        try:
            if self._adopted is not None:
                self._adopted.terminate()
            elif self._child is not None:
                self._child.terminate()
        except Exception as error:
            logger.debug(f"Could not terminate the List Recorder: {error}")

    def _kill(self) -> None:
        try:
            if self._adopted is not None:
                self._adopted.kill()
            elif self._child is not None:
                self._child.kill()
        except Exception as error:
            logger.debug(f"Could not kill the List Recorder: {error}")


def log_list_recorder_startup() -> None:
    """Log the List Recorder Switch once, on every launch including a rehearsal."""
    logger.info(
        f"List Recorder switch: {'on' if config.record_list_enabled else 'off'}."
    )


def prepare_list_recorder(dry_run=False) -> None:
    """Take charge of the helper at startup, once, before anything is started.

    With the switch off the automator does not take part at all, and a rehearsal
    does nothing at all. Otherwise the process table is scanned once for a
    leftover copy: one found is adopted with one line; none found means nothing
    is started here, and the first cycle starts one.
    """
    global _list_recorder

    if _list_recorder is not None:
        return
    if dry_run or not config.record_list_enabled:
        return

    supervisor = ListRecorderSupervisor()
    _list_recorder = supervisor
    try:
        leftover = find_list_recorder_process()
        if leftover is not None:
            supervisor.adopt(leftover)
    except Exception as error:
        logger.debug(f"Could not look for a running List Recorder: {error}")


def keep_list_recorder_alive() -> None:
    """Keep the helper alive; called at the very top of every monitor cycle.

    A no-op with the switch off and in a rehearsal. A fault here is logged and
    the cycle carries on: the window job never waits on the helper.
    """
    supervisor = _list_recorder
    if supervisor is None:
        return
    try:
        supervisor.ensure_running()
    except Exception as error:
        logger.error(f"Unexpected problem while keeping the List Recorder alive: {error}")


def stop_list_recorder() -> None:
    """Stop the helper on the way out, before the automator's own recording.

    A no-op with the switch off and in a rehearsal, so a copy started by hand
    keeps running and the automator's quit leaves it alone. A fault here is
    logged and the quit carries on.
    """
    supervisor = _list_recorder
    if supervisor is None:
        return
    try:
        supervisor.stop()
    except Exception as error:
        logger.error(f"Unexpected problem while stopping the List Recorder: {error}")

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

    The List Recorder is kept alive at the very top, beside the recording
    update, so Suspend, no Schedule file, and any Recording Mode leave it
    running and the window job never waits on it.

    Recording is updated next, before the schedule gate and every early return,
    so Long-living keeps recording while the automator is in Suspend. The
    recording decision is independent of the windows and of any window work
    that follows.

    Broadcasting is checked twice: the stream is stopped in the schedule gate's
    outside-the-period branch, before Telegram is shut down, and the stream is
    updated right after the screen fix, before any Telegram work. The gate ends
    the cycle, and the resolution may change in the same cycle a stream starts,
    so one call cannot sit in both places.

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

    The Telegram program file is checked just below the launch guard, before
    the process check. While the resolved path is not an existing file every
    window action is refused (the cycle returns after the screen fix and the
    stream update); the Shutdown above still runs, and recording and the List
    Recorder are untouched.

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

    # The List Recorder is kept alive at the very top of the cycle, beside the
    # recording update and before the schedule gate: Suspend, no Schedule file,
    # and any Recording Mode all leave it running.
    keep_list_recorder_alive()

    # Recording comes first, before the schedule gate and every early return:
    # Long-living must keep recording while the automator is in Suspend.
    update_recording(moment, schedule, dry_run)

    # Schedule gate: Suspend (and Shutdown when needed) outside allowed hours.
    if schedule is not None and not schedule.is_allowed(moment):
        # The stream stops before Telegram does: the period is over, and this
        # also cleans up a stream left behind by a crashed run. The gate ends
        # the cycle, so the stop has to sit here and not after the screen fix.
        stop_broadcast()
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

    # The Telegram program file is checked here, after the gate and the launch
    # guard and before the process check: while it is missing, every window
    # action is refused. The screen fix and the stream below still run, and so
    # do recording and the List Recorder — none of them reads this path.
    window_work_allowed = check_telegram_program()

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

    # The stream is checked after the screen fix and before any Telegram work:
    # the resolution may have changed in this very cycle, and a live picture
    # should not jump. This is the only place a stream is started; both early
    # returns above (the gate and the launch guard) start nothing.
    update_broadcast(moment, schedule, dry_run)

    # A missing program refuses all window work, and the cycle ends here: no
    # process check follows, and nothing above this line is undone.
    if not window_work_allowed:
        return

    if telegram_running is None:
        telegram_running = is_telegram_running()

    if not telegram_running:
        logger.warning(f"Process {telegram_process_name()} not found. Initiating setup sequence...")

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

    # Verification only: report what OBS says, then leave. Logging is already
    # configured, so the report lands on the console and in the log file like
    # everything else. Nothing else runs — no DPI setup, no screen work, no
    # recording, no broadcaster, no Telegram — and given both flags the check
    # wins over the rehearsal.
    if check_obs_requested(sys.argv[1:]):
        raise SystemExit(print_obs_check(schedule))

    dry_run = dry_run_requested(sys.argv[1:])

    set_dpi_awareness()
    log_startup_locations()
    log_recording_startup()

    # Broadcasting announces the Broadcast Switch and opens OBS; the List
    # Recorder Switch line follows it, and both sit before the rehearsal branch
    # so a rehearsal reads the same switches as a real run.
    prepare_broadcast(dry_run)
    log_list_recorder_startup()
    prepare_list_recorder(dry_run)

    if dry_run:
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
            # The helper first: it may own several recorder children and may
            # need up to ten seconds, then the automator's own recording.
            stop_list_recorder()
            stop_recording()
            stop_broadcast()
            break
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
            time.sleep(MONITOR_INTERVAL)

if __name__ == "__main__":
    main()
