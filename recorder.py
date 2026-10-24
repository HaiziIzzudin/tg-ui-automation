"""Owns the recorder child process: start, graceful stop, reconnect, disk guard.

This is the Python replacement for the manual ``ffmpeg_cmd.ps1`` loop. It knows
nothing about windows, the screen, the Schedule, or the clock: the caller
supplies the moment and decides whether recording should run right now. The
command it builds mirrors the old script argument for argument, so Clips keep
their timestamped names and their clip length.

The module never imports ``main``, ``config``, or ``schedule``, and it never
reads the clock. Exactly one child process is owned here; its own words are
read on a background thread, so nothing the recorder writes can block the
automator. Before every start a Stream Check looks at the stream's playlist
and picks the newest segment that really exists. The Clips folder alone is
watched, once per monitor cycle, to name the Clip being written in the log.
The automator's own recording uses the defaults: endless reconnection and the
timestamp-only Clip name. Two optional values let a List Recorder reuse the
same engine with its own Clip-name prefix and a bounded Retry Limit.
"""

import collections
import datetime
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Tuple

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

# Clip name pattern for a caller that leads the name with its own text. The
# four-digit year becomes two digits here, because that is the shape the List
# Recorder file names are specified with.
CLIP_NAME_PREFIXED_PATTERN = "%y%m%d_%H%M%S.mp4"

# Where the recorder program starts reading the stream's playlist: three
# entries back from the newest one. The playlist the stream publishes still
# lists about eight hours of history, but the server has already deleted those
# old entries, so every one of them answers "404 Not Found". Starting at the
# oldest entry (index 0) therefore makes the recorder grind through thousands
# of not-found errors, never reach live, and never write a Clip file at all.
# A few steps back from the newest segment reach live at once and still leave
# room for the segment being written right now; if the stream's retention ever
# changes, this one value moves.
LIVE_START_INDEX = -3

# ---------------------------------------------------------------------------
# Stream Check budget
# ---------------------------------------------------------------------------
# How far back from the newest segment the Stream Check will look for one that
# really exists, how long one look may take, and how long the whole check may
# take. The newest segment usually answers, so the normal case costs one look.
STREAM_CHECK_MAX_LOOKBACK = 12
STREAM_CHECK_REQUEST_TIMEOUT = 2.0
STREAM_CHECK_TOTAL_TIMEOUT = 10.0

# How many segments behind the working one the recorder is told to start, so a
# segment that is listed but not yet written at the very edge cannot spoil it.
STREAM_CHECK_CUSHION = 2

# How many lines of the recorder child's own words are kept in memory, so an
# unexpected exit can explain itself; only the tail is worth keeping.
CHILD_TAIL_LINES = 20

# How long the background reader is given to finish after an unexpected exit,
# before its remembered tail is logged.
CHILD_TAIL_JOIN_SECONDS = 0.5

# Lines worth seeing without turning on debug: the stream it opened, the
# stream details it reports, the output it created, and its ready line. Every
# other line — the per-segment chatter — stays at debug.
_CHILD_USEFUL_MARKERS = ("input #", "stream #", "output #", "press [q]")

STATUS_STOPPED = "stopped"
STATUS_RUNNING = "running"
STATUS_WAITING = "waiting-to-retry"
STATUS_DISK_STOP = "disk-stop"
# Reported while the Retry Limit is exhausted and no child runs. Only an
# explicit start by the caller clears it.
STATUS_GAVE_UP = "gave-up"

# The automator's own folder, used as the child's working directory exactly as
# the old script ran next to the Recordings folder.
_START_DIRECTORY = os.path.dirname(os.path.abspath(__file__))


class StreamCheckError(Exception):
    """Raised inside the Stream Check when no usable segment could be found.

    Never escapes :func:`choose_live_start_index`; it only carries the plain
    reason to the warning that falls back to the default start.
    """


# ---------------------------------------------------------------------------
# Stream Check
# ---------------------------------------------------------------------------

def fetch_playlist_text(address: str, timeout: float) -> str:
    """Fetch one playlist reply at *address* and return it as text.

    This is the single place a network request is made. Tests replace this
    function so the Stream Check can be exercised without a network.
    """
    with urllib.request.urlopen(address, timeout=timeout) as reply:
        return reply.read().decode("utf-8", errors="replace")


def _playlist_entries(text: str):
    """Yield the non-comment, non-blank lines of a playlist, in order."""
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            yield line


def _first_variant_address(master_text: str, master_address: str) -> Optional[str]:
    """Return the first variant playlist address, resolved against the master.

    Only a line that follows an ``#EXT-X-STREAM-INF`` tag counts as a variant;
    a reply that is already a segment list has no such tag and gives None.
    """
    previous = ""
    for raw in master_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            previous = line
            continue
        if previous.startswith("#EXT-X-STREAM-INF"):
            return urllib.parse.urljoin(master_address, line)
        previous = line
    return None


def _check_timeout(deadline: float) -> float:
    """Return the per-request timeout, never more than the time left."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0.0
    return min(STREAM_CHECK_REQUEST_TIMEOUT, remaining)


def clamp_start_index(chosen: int, segment_count: int) -> int:
    """Keep *chosen* off the oldest entry the playlist lists.

    A negative position counts back from the newest segment, so the oldest
    entry is ``-segment_count`` and the safe floor is ``-(segment_count - 1)``.
    """
    return max(chosen, -(segment_count - 1))


def _find_live_start_index(stream_url: str) -> Tuple[int, int, int]:
    """Look for the newest segment that really exists.

    Returns ``(start_index, position, looks)``: the chosen position for the
    recorder, how far back the working segment was (0 is the newest), and how
    many looks it took. Raises :class:`StreamCheckError` when nothing answered
    inside the budget.
    """
    deadline = time.monotonic() + STREAM_CHECK_TOTAL_TIMEOUT

    master_text = fetch_playlist_text(stream_url, _check_timeout(deadline))
    variant_address = _first_variant_address(master_text, stream_url)
    if variant_address is None:
        # Not a master playlist: the reply is already the segment list.
        variant_text = master_text
        variant_address = stream_url
    else:
        variant_text = fetch_playlist_text(variant_address, _check_timeout(deadline))

    # Segments are relative addresses; resolving against the variant
    # playlist's own address keeps the session token they carry valid.
    segments = [
        urllib.parse.urljoin(variant_address, line)
        for line in _playlist_entries(variant_text)
    ]
    if not segments:
        raise StreamCheckError("the playlist listed no segments.")

    newest = len(segments) - 1
    looks = min(STREAM_CHECK_MAX_LOOKBACK, len(segments))
    for position in range(looks):
        if deadline - time.monotonic() <= 0:
            raise StreamCheckError(
                f"time ran out after looking back {position} time(s)."
            )
        try:
            fetch_playlist_text(segments[newest - position], _check_timeout(deadline))
        except Exception as error:
            logger.debug(
                f"Stream Check: segment {position} back is not available "
                f"(look {position + 1} of {looks}): {error}"
            )
            continue

        chosen = clamp_start_index(
            -(position + STREAM_CHECK_CUSHION), len(segments)
        )
        return chosen, position, position + 1

    raise StreamCheckError(
        f"no segment answered within the budget (looked back {looks} time(s))."
    )


def choose_live_start_index(stream_url: str) -> int:
    """Return the start position the recorder should use, and explain it once.

    Looks at the stream's playlist and finds the newest segment that really
    exists, then tells the recorder to begin just behind it. One request on
    the normal path. Nothing available, or any failure at all: the existing
    default is used and a warning says what happened. This never raises and
    never holds the caller longer than the total budget.
    """
    try:
        chosen, position, looks = _find_live_start_index(stream_url)
    except StreamCheckError as error:
        logger.warning(
            f"Stream Check: {error} Recording will start at the default "
            f"position ({LIVE_START_INDEX})."
        )
        return LIVE_START_INDEX
    except Exception as error:
        logger.warning(
            f"Stream Check: could not look at the stream ({error}); "
            f"recording will start at the default position ({LIVE_START_INDEX})."
        )
        return LIVE_START_INDEX

    if position == 0:
        logger.info(
            f"Stream Check: the live edge is there; recording starts "
            f"{STREAM_CHECK_CUSHION} segments back (position {chosen}) after "
            f"{looks} look(s)."
        )
    else:
        logger.info(
            f"Stream Check: the newest {position} segment(s) were missing; "
            f"recording starts on the newest data it could find (position "
            f"{chosen}) after {looks} look(s)."
        )
    return chosen


# ---------------------------------------------------------------------------
# The recorder child's own words
# ---------------------------------------------------------------------------

def is_useful_child_line(line: str) -> bool:
    """Return True for the few recorder lines worth information level."""
    lowered = line.lower()
    return any(marker in lowered for marker in _CHILD_USEFUL_MARKERS)


class ChildOutputReader(threading.Thread):
    """Reads the recorder child's error stream on a daemon thread.

    Every line is logged at debug; the few useful lines are also logged at
    information. The last ``tail_size`` lines are remembered, so an unexpected
    exit can log that tail at warning. Being a daemon, the reader can never
    hold the automator open; a stream that misbehaves is only a debug note.
    """

    def __init__(self, stream, tail_size: int = CHILD_TAIL_LINES):
        super().__init__(daemon=True)
        self._stream = stream
        self._tail = collections.deque(maxlen=tail_size)

    def run(self) -> None:
        try:
            for raw in self._stream:
                if isinstance(raw, bytes):
                    line = raw.decode("utf-8", errors="replace")
                else:
                    line = str(raw)
                line = line.rstrip("\r\n")
                self._tail.append(line)
                self._log(line)
        except Exception as error:
            logger.debug(f"Could not read the recorder's output: {error}")

    def tail(self) -> List[str]:
        """Return the remembered lines, oldest first."""
        return list(self._tail)

    @staticmethod
    def _log(line: str) -> None:
        if is_useful_child_line(line):
            logger.info(line)
        else:
            logger.debug(line)


@dataclass(frozen=True)
class RecorderSettings:
    """Everything the recorder needs, already resolved by the caller.

    ``output_dir`` is an absolute path; making it relative to the program is
    the caller's job. ``reconnect_seconds`` is a positive wait between a drop
    and the next attempt.

    The optional values are added last so positional callers keep working.
    ``clip_name_prefix`` is file-name text, never a folder, and stays empty for
    the automator's own timestamp-only Clips. ``retry_limit`` None keeps
    today's endless reconnection; a number caps consecutive failed attempts.
    ``retry_reset_seconds`` is how long a child must have lived for its exit
    to count as a good run and start the attempt count over.
    """

    stream_url: str
    output_dir: str
    clip_seconds: int
    reconnect_seconds: float
    ffmpeg_path: str
    clip_name_prefix: str = ""
    retry_limit: Optional[int] = None
    retry_reset_seconds: float = 120.0


def _clip_name_pattern(settings: RecorderSettings) -> str:
    """Return the Clip name pattern, led by the caller's prefix when it has one.

    The prefix only leads the file name; the folder is always the output
    folder. An empty prefix keeps the automator's existing name untouched.
    """
    if settings.clip_name_prefix:
        return settings.clip_name_prefix + CLIP_NAME_PREFIXED_PATTERN
    return CLIP_NAME_PATTERN


def build_ffmpeg_command(
    settings: RecorderSettings, start_index: int = LIVE_START_INDEX
) -> List[str]:
    """Return the recorder argument list, mirroring ``ffmpeg_cmd.ps1``.

    ``start_index`` is the playlist position chosen by the Stream Check; it
    falls back to ``LIVE_START_INDEX`` so a plain call keeps working. The last
    argument is the timestamped Clip pattern: one new Clip every
    ``clip_seconds``, named by the moment it started. ``-nostats`` silences the
    continuous progress lines; ``-nostdin`` is not used, because the graceful
    quit writes the quit key to the child's input.
    """
    output_pattern = os.path.join(settings.output_dir, _clip_name_pattern(settings))
    return [
        settings.ffmpeg_path,
        "-hide_banner",
        "-nostats",
        "-live_start_index", str(start_index),
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
        self._reader: Optional[ChildOutputReader] = None
        self._started_at: Optional[datetime.datetime] = None
        self._retry_at: Optional[datetime.datetime] = None
        self._disk_stop = False
        # Consecutive failed reconnection attempts, and whether the Retry Limit
        # has been reached. Both start over on an explicit start.
        self._failed_attempts = 0
        self._gave_up = False
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

        An explicit start clears the disk-stop latch and the Retry Limit count,
        including a ``gave-up`` state: the caller is asking for recording, so
        the guard and the give-up are done suppressing restarts. The Clips
        folder is created when missing. A failed spawn is logged loudly and
        leaves the recorder ``stopped``; it never raises.
        """
        self._disk_stop = False
        self._retry_at = None
        self._gave_up = False
        self._failed_attempts = 0
        return self._start_child(moment)

    def _start_child(self, moment: datetime.datetime) -> bool:
        """Spawn the child and report whether it started.

        Reconnection uses this too, and must keep its failed-attempt count, so
        the caller owns the counting and the state around it.
        """
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

        # Stream Check: pick the newest segment that really exists, on the
        # first start and on every reconnection. It never raises and fell back
        # to the default position when it could not tell.
        start_index = choose_live_start_index(self._settings.stream_url)

        command = build_ffmpeg_command(self._settings, start_index)
        try:
            child = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
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
        self._start_output_reader(child)
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
        # A normal stop never dumps the child's last words; forget the reader.
        self._reader = None

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
        suppresses reconnection until the caller starts again. A recorder that
        has reached its Retry Limit is left alone too: it reports ``gave-up``
        and the "no more attempts" line is written only when that state is
        reached, not once per cycle.

        A child that lived at least ``retry_reset_seconds`` was a good run, so
        its exit starts the failed-attempt count over; each failed
        reconnection then adds one. This never waits on the network or the
        child, beyond a short bounded join used to collect the child's last
        words when it has just exited.

        This is also the call the monitor cycle makes every cycle while
        recording, so the Current Clip is announced from here.
        """
        self._announce_current_clip()

        if self._disk_stop:
            logger.debug("Recorder is stopped for low disk space; not reconnecting.")
            return

        if self.is_running():
            return

        if self._gave_up:
            # Silent on purpose: the one line was written when the limit was
            # reached, and this call runs every cycle.
            return

        if self._started_at is None:
            # Nothing was ever started, so there is nothing to reconnect.
            return

        wait = self._settings.reconnect_seconds

        if self._retry_at is None:
            child = self._child
            exit_code = child.poll() if child is not None else None
            self._child = None
            if child is not None:
                # The child left on its own: its last words explain why.
                self._dump_child_tail()
            if self._child_lived_long_enough(moment):
                # A good run: the next drop gets the full Retry Limit again.
                self._failed_attempts = 0
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
        if not self._start_child(moment):
            self._failed_attempts += 1
            if self._reached_retry_limit():
                # One line only, here, where the limit is reached; later
                # cycles fall out above without adding noise.
                self._gave_up = True
                logger.warning(
                    f"Reconnect failed {self._failed_attempts} time(s) in a row "
                    f"and the Retry Limit of {self._settings.retry_limit} is "
                    f"reached; no more attempts will be made."
                )
                return
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
        """Return the recorder's state in plain words.

        A live child is ``running`` even when failures were counted before; the
        disk latch is ``disk-stop``; an exhausted Retry Limit is ``gave-up``;
        a pending reconnect is ``waiting-to-retry``; anything else is
        ``stopped``.
        """
        if self.is_running():
            return STATUS_RUNNING
        if self._disk_stop:
            return STATUS_DISK_STOP
        if self._gave_up:
            return STATUS_GAVE_UP
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

    def _child_lived_long_enough(self, moment: datetime.datetime) -> bool:
        """Return True when the child that just left outlived the reset window.

        The moment it started is remembered; a run at least
        ``retry_reset_seconds`` long counts as good, so the next drop gets the
        full Retry Limit again. A recorder that never started gives False.
        """
        if self._started_at is None:
            return False
        lived = (moment - self._started_at).total_seconds()
        return lived >= self._settings.retry_reset_seconds

    def _reached_retry_limit(self) -> bool:
        """Return True when no further reconnection attempt may be made.

        No limit (the automator's case) is endless reconnection, so it is
        never reached.
        """
        limit = self._settings.retry_limit
        if limit is None:
            return False
        return self._failed_attempts >= limit

    def _start_output_reader(self, child) -> None:
        """Read the child's error stream on a daemon thread.

        A child without a readable stream (a fake, or a pipe that could not be
        made) simply gets no reader. Any failure is a debug note: reading the
        child's words must never disturb recording.
        """
        stream = getattr(child, "stderr", None)
        if stream is None:
            self._reader = None
            return
        try:
            reader = ChildOutputReader(stream)
            reader.start()
        except Exception as error:
            logger.debug(f"Could not read the recorder's output: {error}")
            self._reader = None
            return
        self._reader = reader

    def _dump_child_tail(self) -> None:
        """Log the child's remembered last lines after an unexpected exit.

        A brief, bounded join lets the reader finish the lines already in the
        pipe. Nothing is logged when no reader ran or nothing was remembered.
        """
        reader = self._reader
        self._reader = None
        if reader is None:
            return
        try:
            reader.join(timeout=CHILD_TAIL_JOIN_SECONDS)
        except Exception as error:
            logger.debug(f"Could not wait for the recorder's output reader: {error}")
        try:
            tail = reader.tail()
        except Exception as error:
            logger.debug(f"Could not read the recorder's remembered output: {error}")
            return
        if not tail:
            return
        logger.warning(
            "Recorder output before it stopped on its own "
            f"(last {len(tail)} line(s)):\n" + "\n".join(tail)
        )

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
        skipped, so only a name appearing afterwards counts. When a Clip-name
        prefix is set, only files led by it count: the Clips folder is shared
        with the automator and with other List Recordings, and announcing
        somebody else's file would name the wrong Clip. A folder that cannot be
        listed is a debug note and gives None; it never raises.
        """
        names = self._list_clip_names()
        if names is None:
            return None

        fresh = [
            name
            for name in names
            if name not in self._clips_at_start
            and name.startswith(self._settings.clip_name_prefix)
        ]
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
