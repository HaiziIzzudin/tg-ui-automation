import os
import logging
import ctypes
from typing import Tuple, List, Optional

logger = logging.getLogger(__name__)

_VALID_ROTATIONS = (0, 90, 180, 270)

_VALID_RECORD_MODES = ("off", "long-living", "follow-schedule")

_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

_VALID_TRUE_WORDS = ("true", "1", "yes", "on")

_VALID_FALSE_WORDS = ("false", "0", "no", "off")

_MIN_PORT = 1

_MAX_PORT = 65535

# Default Clips folder: the absolute path to "Recordings" next to the program.
# Computed once at load time so the default is unambiguous and never joined
# onto anything later.
DEFAULT_RECORDINGS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "Recordings"
)

# Default OBS program: the usual install folder on this machine. A copy of the
# settings file on another computer points BROADCAST_OBS_PATH at its own copy.
DEFAULT_OBS_PATH = r"C:\Program Files\obs-studio\bin\64bit\obs64.exe"


def clamp_rotation(value: int) -> int:
    """Return *value* if it is a valid rotation, otherwise 0."""
    return value if value in _VALID_ROTATIONS else 0


def parse_resolution(value: str, default: Tuple[int, int]) -> Tuple[int, int]:
    """Parse a resolution string like '1920x1080' into (width, height)."""
    try:
        parts = value.lower().split('x')
        if len(parts) != 2:
            raise ValueError("Invalid format")
        width, height = int(parts[0]), int(parts[1])
        if width <= 0 or height <= 0:
            raise ValueError("Non-positive value")
        return (width, height)
    except (ValueError, AttributeError) as e:
        logger.warning(f"Invalid resolution '{value}': {e}. Using default {default}.")
        return default


def parse_position(value: str, default: Tuple[int, int]) -> Tuple[int, int]:
    """Parse a position string like '10,20' into (x, y)."""
    try:
        parts = value.split(',')
        if len(parts) != 2:
            raise ValueError("Invalid format")
        x, y = int(parts[0]), int(parts[1])
        return (x, y)
    except (ValueError, AttributeError) as e:
        logger.warning(f"Invalid position '{value}': {e}. Using default {default}.")
        return default


def parse_click_points(value: str, default: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Parse a click points string like '44,240;207,119;755,113' into a list of tuples."""
    if not value or not value.strip():
        logger.warning("Empty click points. Using default.")
        return default
    try:
        points = []
        for point_str in value.split(';'):
            point_str = point_str.strip()
            if not point_str:
                continue
            parts = point_str.split(',')
            if len(parts) != 2:
                raise ValueError(f"Invalid point format: '{point_str}'")
            x, y = int(parts[0]), int(parts[1])
            points.append((x, y))
        if not points:
            raise ValueError("No valid points")
        return points
    except (ValueError, AttributeError) as e:
        logger.warning(f"Invalid click points '{value}': {e}. Using default.")
        return default


def parse_int(value: str, default: int) -> int:
    """Parse an integer string, returning default if invalid or non-positive."""
    try:
        i = int(value)
        if i <= 0:
            raise ValueError("Non-positive value")
        return i
    except (ValueError, AttributeError) as e:
        logger.warning(f"Invalid integer '{value}': {e}. Using default {default}.")
        return default


def parse_float(value: str, default: float) -> float:
    """Parse a float string, returning default if invalid or non-positive."""
    try:
        f = float(value)
        if f <= 0:
            raise ValueError("Non-positive value")
        return f
    except (ValueError, AttributeError) as e:
        logger.warning(f"Invalid float '{value}': {e}. Using default {default}.")
        return default


def parse_record_mode(value: str, default: str = "off") -> str:
    """Parse a recording mode, returning default if it is not a known mode."""
    try:
        mode = value.strip().lower()
    except AttributeError as e:
        logger.warning(f"Invalid recording mode '{value}': {e}. Using default {default}.")
        return default
    if mode not in _VALID_RECORD_MODES:
        accepted = ", ".join(f"'{m}'" for m in _VALID_RECORD_MODES)
        logger.warning(
            f"Invalid recording mode '{value}': accepted values are {accepted}. "
            f"Using default {default}."
        )
        return default
    return mode


def parse_text(value: str, default: str) -> str:
    """Trim *value*, returning default if it is empty after trimming."""
    text = value.strip()
    if not text:
        logger.warning(f"Empty value. Using default '{default}'.")
        return default
    return text


def parse_log_level(value: str, default: str = "INFO") -> str:
    """Parse a logging level name, case-insensitively.

    Accepts ``DEBUG``, ``INFO``, ``WARNING``, and ``ERROR``. Anything else
    warns once naming the setting and falls back to *default*.
    """
    try:
        level = value.strip().upper()
    except AttributeError as e:
        logger.warning(f"Invalid log level '{value}': {e}. Using default {default}.")
        return default
    if level not in _VALID_LOG_LEVELS:
        accepted = ", ".join(_VALID_LOG_LEVELS)
        logger.warning(
            f"Invalid log level '{value}': accepted values are {accepted}. "
            f"Using default {default}."
        )
        return default
    return level


def parse_recordings_dir(value: str, default: str) -> str:
    """Return the Clips folder, which must be an absolute path.

    An absolute value — a drive path such as ``D:\\Clips`` or a UNC path such
    as ``\\\\server\\share\\clips`` — is kept exactly as given. A relative
    value, or one that is empty after trimming, warns once naming the setting
    and falls back to *default* (itself an absolute path).
    """
    try:
        folder = value.strip()
    except AttributeError:
        folder = ""
    if folder and os.path.isabs(folder):
        return folder
    logger.warning(
        f"RECORDINGS_DIR must be an absolute path (got '{value}'). "
        f"Using default '{default}'."
    )
    return default


def parse_bool(value: str, default: bool) -> bool:
    """Parse an on/off switch, returning default if the word is not known.

    Accepts ``true``/``false``, ``1``/``0``, ``yes``/``no``, and ``on``/``off``,
    case-insensitively and with surrounding spaces. Anything else warns once
    naming the bad value and the accepted words, then falls back to *default*.
    """
    try:
        word = value.strip().lower()
    except AttributeError as e:
        logger.warning(f"Invalid switch value '{value}': {e}. Using default {default}.")
        return default
    if word in _VALID_TRUE_WORDS:
        return True
    if word in _VALID_FALSE_WORDS:
        return False
    accepted = ", ".join(f"'{w}'" for w in _VALID_TRUE_WORDS + _VALID_FALSE_WORDS)
    logger.warning(
        f"Invalid switch value '{value}': accepted values are {accepted}. "
        f"Using default {default}."
    )
    return default


def parse_port(value: str, default: int) -> int:
    """Parse a network port, returning default if it is not 1 to 65535.

    Accepts an integer from 1 to 65535 inclusive. Text, a number outside that
    range, or a missing value warns once naming the bad value and the default,
    then falls back to *default*.
    """
    try:
        port = int(value)
    except (ValueError, TypeError, AttributeError) as e:
        logger.warning(f"Invalid port '{value}': {e}. Using default {default}.")
        return default
    if port < _MIN_PORT or port > _MAX_PORT:
        logger.warning(
            f"Invalid port '{value}': must be between {_MIN_PORT} and "
            f"{_MAX_PORT}. Using default {default}."
        )
        return default
    return port


def get_scale_factor() -> float:
    """Read the current system DPI scale factor (1.0 = 96 DPI, 1.5 = 144 DPI, etc.).

    Uses GetDpiForSystem() which returns the raw DPI value; dividing by 96
    yields the standard scale factor (1.0, 1.25, 1.5, 2.0, ...).

    Returns 1.0 on failure with a logged warning.
    """
    try:
        user32 = ctypes.windll.user32
        dpi = user32.GetDpiForSystem()
        return dpi / 96.0
    except Exception as e:
        logger.warning(f"Failed to read DPI scale factor: {e}. Assuming 1.0.")
        return 1.0


def parse_maximized(value: str, default: Tuple[int, int]) -> Optional[Tuple[int, int]]:
    """Parse a window size string; returns None if 'maximized', else parses as resolution."""
    if value.strip().lower() == "maximized":
        return None
    return parse_resolution(value, default)





class Config:
    """Centralized configuration loaded from environment variables with parsing and defaults."""

    def __init__(self):
        self.second_window_title = os.getenv("SECOND_WINDOW_TITLE", "secondWindowTitle")
        self.screen_resolution = parse_resolution(
            os.getenv("SCREEN_RESOLUTION", "1920x1080"),
            (1920, 1080),
        )
        self.screen_rotation = clamp_rotation(
            parse_int(
                os.getenv("SCREEN_ROTATION", "0"),
                0,
            )
        )
        self.click_points = parse_click_points(
            os.getenv("CLICK_POINTS", "44,240;207,119;755,113"),
            [(44, 240), (207, 119), (755, 113)],
        )
        self.second_window_size = parse_maximized(
            os.getenv("SECOND_WINDOW_SIZE", "1280x1032"),
            (1280, 1032),
        )
        self.second_window_pos = parse_position(
            os.getenv("SECOND_WINDOW_POS", "0,0"),
            (0, 0),
        )
        self.kill_cooldown_seconds = parse_int(
            os.getenv("KILL_COOLDOWN_SECONDS", "60"),
            60,
        )
        self.second_window_retry_limit = parse_int(
            os.getenv("SECOND_WINDOW_RETRY_LIMIT", "3"),
            3,
        )
        self.click_retry_interval = parse_float(
            os.getenv("CLICK_RETRY_INTERVAL", "3.0"),
            3.0,
        )
        self.log_level = parse_log_level(
            os.getenv("LOG_LEVEL", "INFO"),
            "INFO",
        )
        self.recording_mode = parse_record_mode(
            os.getenv("RECORD_MODE", "off"),
            "off",
        )
        self.record_stream_url = os.getenv("RECORD_STREAM_URL", "").strip()
        self.recordings_dir = parse_recordings_dir(
            os.getenv("RECORDINGS_DIR", DEFAULT_RECORDINGS_DIR),
            DEFAULT_RECORDINGS_DIR,
        )
        self.record_clip_seconds = parse_int(
            os.getenv("RECORD_CLIP_SECONDS", "7200"),
            7200,
        )
        self.record_reconnect_seconds = parse_float(
            os.getenv("RECORD_RECONNECT_SECONDS", "5"),
            5.0,
        )
        self.record_ffmpeg_path = parse_text(
            os.getenv("RECORD_FFMPEG_PATH", "ffmpeg"),
            "ffmpeg",
        )
        self.record_disk_warn_gb = parse_float(
            os.getenv("RECORD_DISK_WARN_GB", "10"),
            10.0,
        )
        self.record_disk_stop_gb = parse_float(
            os.getenv("RECORD_DISK_STOP_GB", "2"),
            2.0,
        )
        if self.record_disk_stop_gb >= self.record_disk_warn_gb:
            logger.warning(
                f"RECORD_DISK_STOP_GB ({self.record_disk_stop_gb}) is not below "
                f"RECORD_DISK_WARN_GB ({self.record_disk_warn_gb}); using the "
                f"defaults 10 and 2."
            )
            self.record_disk_warn_gb = 10.0
            self.record_disk_stop_gb = 2.0
        self.broadcast_enabled = parse_bool(
            os.getenv("BROADCAST_ENABLED", "false"),
            False,
        )
        self.broadcast_obs_host = parse_text(
            os.getenv("BROADCAST_OBS_HOST", "127.0.0.1"),
            "127.0.0.1",
        )
        self.broadcast_obs_port = parse_port(
            os.getenv("BROADCAST_OBS_PORT", "4455"),
            4455,
        )
        # The OBS password is optional: an empty value is accepted without a
        # warning, because OBS itself then refuses the connection.
        self.broadcast_obs_password = os.getenv("BROADCAST_OBS_PASSWORD", "").strip()
        self.broadcast_obs_path = parse_text(
            os.getenv("BROADCAST_OBS_PATH", DEFAULT_OBS_PATH),
            DEFAULT_OBS_PATH,
        )
        self.scale_factor: float = get_scale_factor()

    def refresh_scale_factor(self) -> float:
        """Re-read the DPI scale factor and store it. Returns the new value."""
        self.scale_factor = get_scale_factor()
        return self.scale_factor

    def get_screen_bounds(self) -> Tuple[int, int]:
        user32 = ctypes.windll.user32
        raw_w = user32.GetSystemMetrics(0)
        raw_h = user32.GetSystemMetrics(1)
        if self.screen_rotation in (90, 270):
            return (raw_h, raw_w)
        return (raw_w, raw_h)

    def get_expected_resolution(self) -> Tuple[int, int]:
        """Return the display size expected by the OS, accounting for rotation.

        When rotation is 90 or 270 the width/height are swapped relative to
        the configured (portrait) values so that the comparison against the
        actual GetSystemMetrics values is correct.
        """
        width, height = self.screen_resolution
        if self.screen_rotation in (90, 270):
            return (height, width)
        return (width, height)
