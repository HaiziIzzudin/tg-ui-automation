"""Owns the recorder child process: start, graceful stop, reconnect, disk guard.

This is the Python replacement for the manual ``ffmpeg_cmd.ps1`` loop. It knows
nothing about windows, the screen, the Schedule, or the clock: the caller
supplies the moment and decides whether recording should run right now. The
command it builds mirrors the old script argument for argument, so Clips keep
their timestamped names and their clip length.

The module never imports ``main``, ``config``, or ``schedule``, and it never
reads the clock. Exactly one child process is owned here; its output goes
nowhere the caller must drain, so nothing the recorder does can block the
automator. The Clips folder alone is watched, once per monitor cycle, to name
the Clip being written in the log.
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
        # The Current Clip name already announced, and the file names that were
        # in the Clips folder when this run started. The second set is what
        # keeps leftovers from an earlier run out of the log.
        self._last_clip: Optional[str] = None
        self._clips_at_start = set()

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

        # Note what is already in the folder: those files belong to an earlier
        # run and are never announced.
        self._clips_at_start = set(self._list_clip_names() or [])

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
        stop (Recording Stop); it leaves the disk-stop latch alone. The
        Current Clip name is forgotten, so the next start announces its own
        file once more.
        """
        self._retry_at = None
        self._last_clip = None
        self._clips_at_start = set()

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

        This is also the call the monitor cycle makes every cycle while
        recording, so the Current Clip is announced from here.
        """
        self._announce_current_clip()

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

    def current_clip(self) -> Optional[str]:
        """Return the name of the Clip announced most recently, or None.

        Nothing is returned while recording is stopped, so a caller can tell
        "no Current Clip" from "the same Clip as last time".
        """
        if not self.is_running():
            return None
        return self._last_clip

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _announce_current_clip(self) -> None:
        """Log the newest Clip name once each time it changes.

        Called from ``supervise``, i.e. once per monitor cycle while
        recording. Only a live child is considered, so a stopped or
        reconnecting recorder announces nothing. Any failure here is a debug
        note: watching the Clips folder must never disturb recording.
        """
        try:
            if not self.is_running():
                return

            newest = self._newest_clip_name()
            if newest is None or newest == self._last_clip:
                return

            self._last_clip = newest
            logger.info(f"Recording the current Clip: '{newest}'.")
        except Exception as error:
            logger.debug(f"Could not check for the current Clip: {error}")

    def _newest_clip_name(self) -> Optional[str]:
        """Return the newest Clip name written by this run, or None.

        Files that were already in the folder when the run started are
        skipped, so only a name appearing afterwards counts. A folder that
        cannot be listed is a debug note and gives None; it never raises.
        """
        names = self._list_clip_names()
        if names is None:
            return None

        fresh = [name for name in names if name not in self._clips_at_start]
        if not fresh:
            return None
        # Names are timestamps, so the largest name is the newest file. Any
        # failure while picking it out is the caller's to swallow.
        return max(fresh)

    def _list_clip_names(self) -> Optional[List[str]]:
        """Return the file names in the Clips folder, or None when unlistable.

        A missing folder, a folder that is really a file, or a denied read is
        a quiet debug note: the recorder keeps running either way.
        """
        try:
            return os.listdir(self._settings.output_dir)
        except OSError as error:
            logger.debug(
                f"Could not read the Clips folder "
                f"'{self._settings.output_dir}': {error}"
            )
            return None

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
