import os
import logging
import ctypes
from typing import Tuple, List, Optional

logger = logging.getLogger(__name__)

_VALID_ROTATIONS = (0, 90, 180, 270)


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
