"""Settings and Clean Names for the List Recorder.

The List Recorder is a separate program (see ADR-0005). It reads the same
``.env`` file as the automator, but only two values are shared with it: the
Clips folder and the log level. Every other value here has its own setting and
its own default.

This module is pure: text in, values out. It never records, never opens a
socket, and never reads the clock. ``.env`` is loaded by the program before the
settings are built, not here.
"""
import logging
import math
import os
import re
from dataclasses import dataclass
from typing import Mapping, Optional

import config

logger = logging.getLogger(__name__)

DEFAULT_LIST_URL = "http://127.0.0.1:8479/"
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_CLIP_SECONDS = 900
DEFAULT_RECONNECT_SECONDS = 5.0
DEFAULT_RETRY_LIMIT = 2
DEFAULT_RETRY_RESET_SECONDS = 120.0
DEFAULT_FFMPEG_PATH = "ffmpeg"
DEFAULT_DISK_WARN_GB = 10.0
DEFAULT_DISK_STOP_GB = 2.0
DEFAULT_LOG_LEVEL = "INFO"

# The log level names the automator already accepts; kept in step with config.
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# A Clean Name longer than this is cut, so a Clip name stays readable.
NAME_MAX_LENGTH = 40

# A display name with nothing usable left becomes this.
FALLBACK_NAME = "user"

# The characters Windows refuses in a file name.
_FORBIDDEN_CHARACTERS = '\\/:*?"<>|'

# Names Windows keeps for its own devices; a file may not use them as is.
_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{number}" for number in range(1, 10)]
    + [f"LPT{number}" for number in range(1, 10)]
)

_SPACE_RUN = re.compile(r"[ \t]+")
_UNDERSCORE_RUN = re.compile(r"_+")
# Trailing separators (underscores and dots) make Windows unhappy.
_TRAILING_SEPARATORS = re.compile(r"[_.]+$")


@dataclass(frozen=True)
class StreamsSettings:
    """One value object holding everything the List Recorder is told."""

    list_url: str
    poll_seconds: float
    output_dir: str
    clip_seconds: int
    reconnect_seconds: float
    retry_limit: int
    retry_reset_seconds: float
    ffmpeg_path: str
    disk_warn_gb: float
    disk_stop_gb: float
    log_level: str


def load_streams_settings(env: Optional[Mapping[str, str]] = None) -> StreamsSettings:
    """Build the List Recorder's settings from *env*.

    *env* is a mapping of setting names to text. ``None`` means the process
    environment, which is what the program passes in; the tests pass a plain
    dictionary instead, so a real ``.env`` is never needed to exercise them.
    Every value that is missing, blank, or unusable falls back to its default
    with one warning naming the setting.
    """
    if env is None:
        env = os.environ

    disk_warn_gb = _read_float(env, "STREAMS_DISK_WARN_GB", DEFAULT_DISK_WARN_GB)
    disk_stop_gb = _read_float(env, "STREAMS_DISK_STOP_GB", DEFAULT_DISK_STOP_GB)
    if disk_stop_gb >= disk_warn_gb:
        logger.warning(
            f"STREAMS_DISK_STOP_GB ({disk_stop_gb}) is not below "
            f"STREAMS_DISK_WARN_GB ({disk_warn_gb}); using the defaults "
            f"{DEFAULT_DISK_WARN_GB:g} and {DEFAULT_DISK_STOP_GB:g}."
        )
        disk_warn_gb = DEFAULT_DISK_WARN_GB
        disk_stop_gb = DEFAULT_DISK_STOP_GB

    return StreamsSettings(
        list_url=_read_text(env, "STREAMS_URL", DEFAULT_LIST_URL),
        poll_seconds=_read_float(env, "STREAMS_POLL_SECONDS", DEFAULT_POLL_SECONDS),
        output_dir=_read_recordings_dir(env),
        clip_seconds=_read_int(env, "STREAMS_CLIP_SECONDS", DEFAULT_CLIP_SECONDS),
        reconnect_seconds=_read_float(
            env, "STREAMS_RECONNECT_SECONDS", DEFAULT_RECONNECT_SECONDS
        ),
        retry_limit=_read_int(env, "STREAMS_RETRY_LIMIT", DEFAULT_RETRY_LIMIT),
        retry_reset_seconds=_read_float(
            env, "STREAMS_RETRY_RESET_SECONDS", DEFAULT_RETRY_RESET_SECONDS
        ),
        ffmpeg_path=_read_text(env, "STREAMS_FFMPEG_PATH", DEFAULT_FFMPEG_PATH),
        disk_warn_gb=disk_warn_gb,
        disk_stop_gb=disk_stop_gb,
        log_level=_read_log_level(env),
    )


def clean_name(raw: str, taken: set[str]) -> str:
    """Return *raw* as a name Windows accepts as a file name.

    Spaces become underscores, characters Windows refuses are replaced, and
    the result is cut to :data:`NAME_MAX_LENGTH` characters. A name with
    nothing usable left becomes ``user``. A name Windows reserves for one of
    its devices gets a trailing underscore.

    *taken* holds the lowercase Clean Names already given out in this run;
    when *raw* would clash with one of them, ``_2``, ``_3``, and so on are
    appended until it does not. The returned name keeps the person's own
    letter case; the caller adds its lowercase form to *taken*.
    """
    text = raw.strip()
    text = _SPACE_RUN.sub("_", text)
    text = _replace_forbidden(text)
    text = _UNDERSCORE_RUN.sub("_", text)
    text = _TRAILING_SEPARATORS.sub("", text).lstrip("_")
    text = text[:NAME_MAX_LENGTH]
    # The cut can end on a separator; removing it here keeps a second cleaning
    # of an already clean name from changing anything.
    text = _TRAILING_SEPARATORS.sub("", text)

    if not text:
        text = FALLBACK_NAME

    name = _device_safe(text)
    number = 2
    while name.lower() in taken:
        name = f"{text}_{number}"
        number += 1
    return name


def _read_text(env: Mapping[str, str], key: str, default: str) -> str:
    """Return the text stored under *key*, or *default* when it is blank."""
    raw = _read(env, key)
    if raw is None:
        return default
    if not raw:
        logger.warning(f"Empty {key}. Using default '{default}'.")
        return default
    return config.parse_text(raw, default)


def _read_float(env: Mapping[str, str], key: str, default: float) -> float:
    """Return the positive number stored under *key*, or *default*."""
    raw = _read(env, key)
    if raw is None:
        return default
    try:
        number = float(raw)
    except ValueError:
        number = 0.0
    if not math.isfinite(number) or number <= 0:
        logger.warning(
            f"Invalid {key} '{raw}': must be a positive number. "
            f"Using default {default}."
        )
        return default
    return config.parse_float(raw, default)


def _read_int(env: Mapping[str, str], key: str, default: int) -> int:
    """Return the positive whole number stored under *key*, or *default*."""
    raw = _read(env, key)
    if raw is None:
        return default
    try:
        number = int(raw)
    except ValueError:
        number = 0
    if number <= 0:
        logger.warning(
            f"Invalid {key} '{raw}': must be a positive whole number. "
            f"Using default {default}."
        )
        return default
    return config.parse_int(raw, default)


def _read_log_level(env: Mapping[str, str]) -> str:
    """Return the LOG_LEVEL name, or INFO when it is not one of the four."""
    raw = _read(env, "LOG_LEVEL")
    if raw is None:
        return DEFAULT_LOG_LEVEL
    if raw.upper() not in _LOG_LEVELS:
        accepted = ", ".join(_LOG_LEVELS)
        logger.warning(
            f"Invalid LOG_LEVEL '{raw}': accepted values are {accepted}. "
            f"Using default {DEFAULT_LOG_LEVEL}."
        )
        return DEFAULT_LOG_LEVEL
    return config.parse_log_level(raw, DEFAULT_LOG_LEVEL)


def _read_recordings_dir(env: Mapping[str, str]) -> str:
    """Return the absolute Clips folder, shared with the automator."""
    raw = env.get("RECORDINGS_DIR")
    if raw is None:
        return config.DEFAULT_RECORDINGS_DIR
    return config.parse_recordings_dir(raw, config.DEFAULT_RECORDINGS_DIR)


def _read(env: Mapping[str, str], key: str) -> Optional[str]:
    """Return the text stored under *key* in *env*, trimmed, or None if absent."""
    raw = env.get(key)
    if raw is None:
        return None
    return str(raw).strip()


def _replace_forbidden(text: str) -> str:
    """Return *text* with every character Windows refuses replaced by '_'."""
    characters = []
    for character in text:
        code = ord(character)
        if character in _FORBIDDEN_CHARACTERS or code < 32 or code == 127:
            characters.append("_")
        else:
            characters.append(character)
    return "".join(characters)


def _device_safe(name: str) -> str:
    """Return *name* with a trailing underscore when Windows reserves it."""
    if name.upper() in _DEVICE_NAMES:
        return name + "_"
    return name
