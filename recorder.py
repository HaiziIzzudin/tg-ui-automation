"""Owns the recorder child process: start, graceful stop, reconnect, disk guard.

This is the Python replacement for the manual ``ffmpeg_cmd.ps1`` loop. It knows
nothing about windows, the screen, the Schedule, or the clock: the caller
supplies the moment and decides whether recording should run right now. The
command it builds mirrors the old script argument for argument, so Clips keep
their timestamped names and their clip length.

The module never imports ``main``, ``config``, or ``schedule``, and it never
reads the clock. Exactly one child process is owned here; its output goes
nowhere the caller must drain, so nothing the recorder does can block the
automator.
"""

import datetime
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

# Disk thresholds, in bytes, used when the caller supplies none. The live
# values now come from the settings and are passed to ``classify_free_space``;
# these stay as the in-code defaults so a plain call keeps working.
MIN_FREE_BYTES_WARN = 10 * 1024 ** 3
MIN_FREE_BYTES_STOP = 2 * 1024 ** 3

# How long the recorder is given to close its Clip after the quit key, then to
# leave after a terminate. Short and bounded: the caller is the monitor loop.
QUIT_TIMEOUT_SECONDS = 5.0
TERMINATE_TIMEOUT_SECONDS = 3.0

# Clip name pattern, unchanged from the old script.
CLIP_NAME_PATTERN = "%Y%m%d_%H%M%S.mp4"

STATUS_STOPPED = "stopped"
STATUS_RUNNING = "running"
STATUS_WAITING = "waiting-to-retry"
STATUS_DISK_STOP = "disk-stop"

# The automator's own folder, used as the child's working directory exactly as
# the old script ran next to the Recordings folder.
_START_DIRECTORY = os.path.dirname(os.path.abspath(__file__))


@dataclass(frozen=True)
class RecorderSettings:
    """Everything the recorder needs, already resolved by the caller.

    ``output_dir`` is an absolute path; making it relative to the program is
    the caller's job. ``reconnect_seconds`` is a positive wait between a drop
    and the next attempt.
    """

    stream_url: str
    output_dir: str
    clip_seconds: int
    reconnect_seconds: float
    ffmpeg_path: str


def build_ffmpeg_command(settings: RecorderSettings) -> List[str]:
    """Return the recorder argument list, mirroring ``ffmpeg_cmd.ps1`` exactly.

    The last argument is the timestamped Clip pattern: one new Clip every
    ``clip_seconds``, named by the moment it started.
    """
    output_pattern = os.path.join(settings.output_dir, CLIP_NAME_PATTERN)
    return [
        settings.ffmpeg_path,
        "-hide_banner",
        "-live_start_index", "0",
        "-i", settings.stream_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-f", "segment",
        "-segment_time", str(settings.clip_seconds),
        "-reset_timestamps", "1",
        "-movflags", "frag_keyframe+empty_moov",
        "-strftime", "1",
        output_pattern,
    ]


def ffmpeg_available(settings: RecorderSettings) -> bool:
    """Return True when the recorder program can be found.

    A bare name is looked up on the system path (``ffmpeg`` finds
    ``ffmpeg.exe``); a path with a folder part is used as given and only has
    to exist.
    """
    path = (settings.ffmpeg_path or "").strip()
    if not path:
        return False
    if os.path.isabs(path) or os.sep in path or (os.altsep and os.altsep in path):
        return os.path.isfile(path)
    return shutil.which(path) is not None


def classify_free_space(
    free_bytes: int,
    warn_bytes: int = MIN_FREE_BYTES_WARN,
    stop_bytes: int = MIN_FREE_BYTES_STOP,
) -> str:
    """Map free space in bytes to ``"ok"``, ``"warn"``, or ``"stop"``.

    At or above the warning level is ``ok``; at or above the stop level but
    below the warning level is ``warn``; anything lower is ``stop``. Both
    levels default to the module constants, so a call with only the free space
    keeps working; the caller supplies its own byte levels otherwise.
    """
    if free_bytes >= warn_bytes:
        return "ok"
    if free_bytes >= stop_bytes:
        return "warn"
    return "stop"


class Recorder:
    """Owns one recorder child process and nothing else.

    The caller drives it: it says when to start, when to stop, and passes the
    moment every time. State is kept here so the caller can ask ``status`` and
    let ``supervise`` reconnect a dropped stream after the configured wait.
    """

    def __init__(self, settings: RecorderSettings):
        self._settings = settings
        self._child = None
        self._started_at: Optional[datetime.datetime] = None
        self._retry_at: Optional[datetime.datetime] = None
        self._disk_stop = False

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------

    def start(self, moment: datetime.datetime) -> bool:
        """Start a fresh recorder child, or return False when it cannot run.

        An explicit start clears the disk-stop latch: the caller is asking for
        recording, so the disk guard is done suppressing restarts. The Clips
        folder is created when missing. A failed spawn is logged loudly and
        leaves the recorder ``stopped``; it never raises.
        """
        self._disk_stop = False
        self._retry_at = None

        if not self._settings.stream_url:
            logger.error(
                "Cannot start recording: the stream address is empty. "
                "Set RECORD_STREAM_URL to the stream address."
            )
            return False

        if self.is_running():
            logger.debug("Recorder is already running; not starting a second one.")
            return True

        try:
            os.makedirs(self._settings.output_dir, exist_ok=True)
        except OSError as error:
            logger.error(
                f"Could not create the Clips folder "
                f"'{self._settings.output_dir}': {error}"
            )
            return False

        command = build_ffmpeg_command(self._settings)
        try:
            child = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=_START_DIRECTORY,
            )
        except Exception as error:
            logger.error(
                f"Could not start the recorder program "
                f"'{self._settings.ffmpeg_path}': {error}"
            )
            self._child = None
            return False

        self._child = child
        self._started_at = moment
        logger.info(
            f"Recording started: a new Clip every {self._settings.clip_seconds} "
            f"seconds in '{self._settings.output_dir}'."
        )
        return True

    def stop(self) -> bool:
        """Stop the recorder gracefully and never raise.

        The child is first asked to quit (the quit key on its input) so the
        open Clip is closed and playable, then terminated if it lingers, then
        killed. Always ends with no child. Returns whether a child was closed;
        a recorder that was never running returns False. This is the plain
        stop (Recording Stop); it leaves the disk-stop latch alone.
        """
        self._retry_at = None

        child = self._child
        self._child = None
        if child is None:
            return False

        try:
            return self._shut_down(child)
        except Exception as error:
            logger.error(f"Unexpected problem while stopping the recorder: {error}")
            return False

    def stop_for_disk(self) -> bool:
        """Stop for critically low disk space and block reconnection.

        Sets the disk-stop latch, then stops gracefully. ``supervise`` will
        not reconnect and ``status`` reports ``disk-stop`` until the caller
        explicitly starts again, which clears the latch.
        """
        self._disk_stop = True
        return self.stop()

    def restart(self, moment: datetime.datetime) -> bool:
        """Stop the current child, then start a fresh Clip. Returns the start."""
        self.stop()
        return self.start(moment)

    def supervise(self, moment: datetime.datetime) -> None:
        """Reconnect a child that exited on its own, after the configured wait.

        The wait starts when the exit is first noticed, measured on the
        supplied *moment*. Before it elapses the recorder reports
        ``waiting-to-retry``. When it elapses a fresh Clip is started. A
        recorder that was never started is left alone, and a disk stop
        suppresses reconnection until the caller starts again. Never blocks.
        """
        if self._disk_stop:
            logger.debug("Recorder is stopped for low disk space; not reconnecting.")
            return

        if self.is_running():
            return

        if self._started_at is None:
            # Nothing was ever started, so there is nothing to reconnect.
            return

        wait = self._settings.reconnect_seconds

        if self._retry_at is None:
            exit_code = self._child.poll() if self._child is not None else None
            self._child = None
            self._retry_at = moment + datetime.timedelta(seconds=wait)
            if exit_code is None:
                logger.warning(
                    f"Recorder is no longer running; reconnecting in {wait} seconds."
                )
            else:
                logger.warning(
                    f"Recorder stopped (exit code {exit_code}); "
                    f"reconnecting in {wait} seconds."
                )
            return

        if moment < self._retry_at:
            return

        self._retry_at = None
        if not self.start(moment):
            self._retry_at = moment + datetime.timedelta(seconds=wait)
            logger.warning(
                f"Reconnect failed; trying again in {wait} seconds."
            )

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        """Return True only while a live child exists right now.

        A child that exited counts as not running, even when its handle has
        not been cleaned up yet.
        """
        if self._child is None:
            return False
        return self._child.poll() is None

    def status(self) -> str:
        """Return ``stopped``, ``running``, ``waiting-to-retry``, or ``disk-stop``."""
        if self.is_running():
            return STATUS_RUNNING
        if self._disk_stop:
            return STATUS_DISK_STOP
        if self._retry_at is not None:
            return STATUS_WAITING
        return STATUS_STOPPED

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _shut_down(self, child) -> bool:
        """Quit, then terminate, then kill *child*. Each step is guarded."""
        if self._request_quit(child):
            logger.info("Recorder stopped; the current Clip is closed.")
            return True

        logger.warning("Recorder did not quit in time; terminating it.")
        try:
            child.terminate()
            child.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
            return True
        except Exception as error:
            logger.debug(f"Terminate did not end the recorder: {error}")

        logger.warning("Recorder still running after terminate; killing it.")
        try:
            child.kill()
            child.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
        except Exception as error:
            logger.debug(f"Kill did not end the recorder: {error}")
        return True

    @staticmethod
    def _request_quit(child) -> bool:
        """Ask the child to quit and wait briefly. True once it has left."""
        try:
            if child.stdin is not None:
                child.stdin.write(b"q")
                child.stdin.flush()
        except Exception as error:
            logger.debug(f"Could not ask the recorder to quit: {error}")

        try:
            child.wait(timeout=QUIT_TIMEOUT_SECONDS)
            return True
        except Exception:
            return False
