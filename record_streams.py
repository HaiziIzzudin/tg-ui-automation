"""Run the List Recorder: one List Recording per person on the Stream List.

This is the second program described in ADR-0005. It is not a mode of the
automator and shares nothing with it but the Clips folder and the log level. On
a timer it reads the Stream List and, for every Listed Person on it, keeps one
recorder engine running. A person appearing starts a recording, a person
disappearing stops theirs (after the Absence Grace), and a stream that cannot be
started or reconnected is given up on after the Retry Limit. Several people
record at once and none of them can disturb another.

The cycle is the only interesting part and it is deliberately pure-ish: the
caller supplies the moment and the parsed reply, so nothing here reads the
clock, opens a socket, or spawns a child on its own. ``main`` owns those
boundaries. The only help the cycle takes from the machine is the Clips folder's
free space, and a test replaces that too.

Recording faults never end the program: a failed start, a broken reply, an
engine that throws are all logged and the next cycle carries on. The one fatal
case is a missing recorder program at startup, which ``main`` checks once.
Ctrl+C closes every open Clip so it stays playable, then the program exits.
"""

import datetime
import json
import logging
import os
import shutil
import sys
import time
from typing import List, Optional

from dotenv import load_dotenv

from recorder import (
    STATUS_DISK_STOP,
    STATUS_GAVE_UP,
    STATUS_STOPPED,
    Recorder,
    RecorderSettings,
    classify_free_space,
    ffmpeg_available,
)
from stream_list import ListedStream, fetch_stream_list_text, parse_stream_list
from streams_config import StreamsSettings, clean_name, load_streams_settings

logger = logging.getLogger(__name__)

# How many replies in a row a Listed Person may be missing before their
# recording stops. One missing reply is not proof that they left.
ABSENCE_GRACE = 2

# A run of unreadable replies warns on the first one and then every tenth, so a
# long outage stays visible in the log without a warning line on every poll.
LIST_FAILURE_WARNING_EVERY = 10

BYTES_PER_GB = 1024 ** 3

# The program's own log file, next to this file. The automator's log is never
# touched.
LOG_FILENAME = "record_streams.log"

# One Stream List request is never given more than ten seconds, and never less
# than one, whatever the poll interval says.
LIST_REQUEST_TIMEOUT_MAX = 10.0
LIST_REQUEST_TIMEOUT_MIN = 1.0


def build_engine_settings(
    settings: StreamsSettings, stream_url: str, person_name: str = ""
) -> RecorderSettings:
    """Return the engine settings for one Listed Person.

    *person_name* is that person's Clean Name; it leads every Clip name as
    ``<Clean Name>_``, so the engine writes
    ``<Clean Name>_YYMMDD_HHMMSS.mp4``. An empty name keeps the plain timestamp
    name, which is what the startup check needs and nothing else.
    """
    prefix = person_name + "_" if person_name else ""
    return RecorderSettings(
        stream_url=stream_url,
        output_dir=settings.output_dir,
        clip_seconds=settings.clip_seconds,
        reconnect_seconds=settings.reconnect_seconds,
        ffmpeg_path=settings.ffmpeg_path,
        clip_name_prefix=prefix,
        retry_limit=settings.retry_limit,
        retry_reset_seconds=settings.retry_reset_seconds,
    )


def nearest_existing_folder(folder: str) -> str:
    """Return *folder*, or its nearest ancestor that exists right now."""
    current = os.path.abspath(folder)
    while not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current


def free_bytes_on_disk(folder: str) -> int:
    """Return the free bytes on the drive that holds *folder*.

    The Clips folder may not exist yet (the engine creates it on the first
    start), so its nearest existing parent is measured instead. A failed read
    is left to the caller's guard: it must not end the program.
    """
    return shutil.disk_usage(nearest_existing_folder(folder)).free


def list_request_timeout(poll_seconds: float) -> float:
    """Return how long one Stream List request may take.

    The poll interval caps the wait so one slow request cannot push the next
    cycle far behind; ten seconds is the ceiling and one second the floor.
    """
    return max(LIST_REQUEST_TIMEOUT_MIN, min(poll_seconds, LIST_REQUEST_TIMEOUT_MAX))


class _Person:
    """One Listed Person and the state the farm keeps for them.

    ``next_attempt`` is the earliest moment a first start may be retried;
    ``None`` means "due now". A person is identified by their display name,
    compared without case, but everything logged and written uses the Clean
    Name computed once when they first appeared.
    """

    def __init__(self, person_name: str, stream_url: str, recorder) -> None:
        self.clean_name = person_name
        self.stream_url = stream_url
        self.recorder = recorder
        self.misses = 0
        self.failed_starts = 0
        self.next_attempt: Optional[datetime.datetime] = None
        self.given_up = False


class ListRecorderFarm:
    """Keeps one List Recording per Listed Person, driven by the Stream List.

    The caller feeds one reply per cycle through :meth:`cycle`; the farm
    reconciles its people against that reply, supervises the running engines,
    retries a first start that failed, and watches free space. Every heavy
    boundary is injected: *recorder_factory* builds an engine from a
    :class:`RecorderSettings`, and *free_space_bytes* answers the free bytes of
    the Clips folder. The defaults are the real engine and the real disk, so the
    program needs no arguments; tests pass fakes and never spawn a child or
    touch a disk.
    """

    def __init__(self, settings, recorder_factory=None, free_space_bytes=None):
        self._settings: StreamsSettings = settings
        if recorder_factory is None:
            self._recorder_factory = Recorder
        else:
            self._recorder_factory = recorder_factory
        if free_space_bytes is None:
            self._free_space_bytes = free_bytes_on_disk
        else:
            self._free_space_bytes = free_space_bytes

        # People keyed by their display name without case, so a rename that
        # only changes letter case is still the same person.
        self._people = {}
        # Lowercase Clean Names in use, so a clash gets its _2, _3, ...
        self._taken_names = set()
        # Replies in a row that could not be read, for the log noise control.
        self._list_failures = 0
        # Disk guard memory: the latch that blocks starts, and the last level
        # seen, so the low-space warning is written once per crossing.
        self._disk_latched = False
        self._disk_level: Optional[str] = None
        # The last announced number of running recordings.
        self._running_count = 0

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def cycle(
        self, moment: datetime.datetime, entries: Optional[List[ListedStream]]
    ) -> None:
        """Bring every List Recording in line with one Stream List reply.

        *entries* is the parsed reply; ``None`` means the reply could not be
        read, which stops nothing and is not a missed reply. Every fault is
        logged and swallowed: no recording problem ends the program.
        """
        try:
            self._run_cycle(moment, entries)
        except Exception as error:
            logger.error(f"Unexpected problem in this Stream List cycle: {error}")

    def stop_all(self) -> None:
        """Stop every List Recording so every open Clip is closed and plays.

        This is the way out of the program (Ctrl+C, or the tests' end). Each
        stop is guarded on its own, so one stubborn child cannot keep the
        others open.
        """
        for person in self._people.values():
            self._stop_person(person)

    # ------------------------------------------------------------------
    # The cycle
    # ------------------------------------------------------------------

    def _run_cycle(self, moment, entries) -> None:
        """Run the steps of one cycle in the order the ticket names them."""
        if self._guard_disk():
            # Latched and still below the warning level, or just entered the
            # stop level: nothing may start or reconnect.
            return

        if entries is None:
            self._log_unreadable_reply()
            self._supervise_all(moment)
            return

        self._log_reply_recovered()
        self._reconcile(moment, entries)
        self._supervise_all(moment)
        self._retry_failed_starts(moment)
        self._announce_running_count()

    # ------------------------------------------------------------------
    # Disk guard
    # ------------------------------------------------------------------

    def _guard_disk(self) -> bool:
        """Read free space and act on it; True means starts are blocked now."""
        free_bytes = self._free_space_bytes(self._settings.output_dir)
        warn_bytes = int(self._settings.disk_warn_gb * BYTES_PER_GB)
        stop_bytes = int(self._settings.disk_stop_gb * BYTES_PER_GB)
        level = classify_free_space(free_bytes, warn_bytes, stop_bytes)

        if self._disk_latched:
            if level != "ok":
                return True
            logger.info(
                "Free space is back at the warning level; resuming List Recordings."
            )
            self._disk_latched = False
            self._disk_level = level
            self._reset_after_disk_stop()
            return False

        if level == "stop":
            self._disk_level = level
            self._enter_disk_stop(free_bytes)
            return True

        if level == "warn" and self._disk_level != "warn":
            logger.warning(
                f"Low disk space: only {_gigabytes(free_bytes)} GB free on "
                f"'{self._settings.output_dir}'. List Recordings stop below "
                f"{self._settings.disk_stop_gb:g} GB."
            )

        self._disk_level = level
        return False

    def _enter_disk_stop(self, free_bytes: int) -> None:
        """Stop every recording once, latch the guard, and say why."""
        logger.error(
            f"Disk space is critically low ({_gigabytes(free_bytes)} GB free on "
            f"'{self._settings.output_dir}'); stopping every List Recording so the "
            f"disk never fills. No Clip is deleted."
        )
        self._disk_latched = True
        for person in self._people.values():
            try:
                person.recorder.stop_for_disk()
            except Exception as error:
                logger.error(
                    f"Could not stop the List Recording for "
                    f"{person.clean_name}: {error}"
                )
        self._announce_running_count()

    def _reset_after_disk_stop(self) -> None:
        """Give every person a fresh run once space is back.

        The disk was not their fault, so a Given Up person and a person still
        waiting to retry both start over. Their files and folder are untouched.
        """
        for person in self._people.values():
            person.given_up = False
            person.failed_starts = 0
            person.next_attempt = None

    # ------------------------------------------------------------------
    # The reply
    # ------------------------------------------------------------------

    def _log_unreadable_reply(self) -> None:
        """Warn about an unreadable reply, then keep quiet about the repeats.

        The first failure of a run is a warning; the rest are debug, except
        every tenth, so an outage that lasts minutes is still visible in the
        log without one warning per poll.
        """
        self._list_failures += 1
        if self._list_failures == 1:
            logger.warning(
                "The Stream List could not be read; every List Recording keeps running."
            )
            return
        if self._list_failures % LIST_FAILURE_WARNING_EVERY == 0:
            logger.warning(
                f"The Stream List still could not be read "
                f"({self._list_failures} times in a row); every List Recording "
                f"keeps running."
            )
            return
        logger.debug(
            f"The Stream List could not be read ({self._list_failures} times in a "
            f"row); every List Recording keeps running."
        )

    def _log_reply_recovered(self) -> None:
        """Say once that the Stream List answers again, after a failure."""
        if self._list_failures == 0:
            return
        self._list_failures = 0
        logger.info("The Stream List is answering again.")

    # ------------------------------------------------------------------
    # Reconciling people against the reply
    # ------------------------------------------------------------------

    def _reconcile(self, moment, entries) -> None:
        """Start, update, and forget people so they match one reply."""
        present = set()
        for entry in entries:
            key = entry.username.casefold()
            if key in present:
                # The parser drops duplicates already; a hand-built list might
                # still carry one, and the first entry is the person.
                logger.debug(
                    f"Duplicate Stream List entry for {entry.username!r} ignored."
                )
                continue
            present.add(key)

            person = self._people.get(key)
            if person is None:
                self._add_person(key, entry, moment)
            else:
                self._update_person(person, entry, moment)

        missing = [key for key in self._people if key not in present]
        for key in missing:
            self._miss_person(key)

    def _add_person(self, key: str, entry: ListedStream, moment) -> None:
        """Create one person's recorder and try to start it at once."""
        person_name = clean_name(entry.username, self._taken_names)
        engine = self._recorder_factory(
            build_engine_settings(self._settings, entry.stream, person_name)
        )
        self._taken_names.add(person_name.lower())
        person = _Person(person_name, entry.stream, engine)
        self._people[key] = person
        self._start_person(person, moment)

    def _update_person(self, person: _Person, entry: ListedStream, moment) -> None:
        """Bring one known person in line with their current entry."""
        if person.stream_url != entry.stream:
            self._replace_address(person, entry.stream, moment)
            return

        # They are on the list, so the Absence Grace starts over. A Given Up
        # person gets no action and no line: the engine already said it once.
        person.misses = 0

    def _replace_address(self, person: _Person, stream_url: str, moment) -> None:
        """Close the old child and start a fresh recorder on the new address."""
        logger.info(
            f"The stream address for {person.clean_name} changed; closing the old "
            f"Clip and starting a new recording."
        )
        self._stop_person(person)
        person.recorder = self._recorder_factory(
            build_engine_settings(self._settings, stream_url, person.clean_name)
        )
        person.stream_url = stream_url
        person.misses = 0
        person.failed_starts = 0
        person.next_attempt = None
        person.given_up = False
        self._start_person(person, moment)

    def _miss_person(self, key: str) -> None:
        """Count one missing reply and stop the person at the Absence Grace."""
        person = self._people[key]
        person.misses += 1
        if person.misses < ABSENCE_GRACE:
            logger.debug(
                f"{person.clean_name} was missing from the Stream List once; "
                f"still recording."
            )
            return

        logger.info(
            f"{person.clean_name} left the Stream List; closing their List Recording."
        )
        self._stop_person(person)
        # Forget them so a return later starts a fresh recording.
        self._people.pop(key, None)
        self._taken_names.discard(person.clean_name.lower())

    def _stop_person(self, person: _Person) -> None:
        """Stop one recording, keeping a broken engine from escaping."""
        try:
            person.recorder.stop()
        except Exception as error:
            logger.error(
                f"Could not stop the List Recording for {person.clean_name}: {error}"
            )

    # ------------------------------------------------------------------
    # Supervision and first-start retries
    # ------------------------------------------------------------------

    def _supervise_all(self, moment) -> None:
        """Let every engine reconnect, and note the ones that gave up."""
        for person in self._people.values():
            if person.given_up:
                # Nothing left to ask; the engine's line was written once.
                continue
            try:
                person.recorder.supervise(moment)
                if person.recorder.status() == STATUS_GAVE_UP:
                    person.given_up = True
                    logger.debug(
                        f"{person.clean_name} has given up after the Retry Limit; "
                        f"leaving them alone while they stay listed."
                    )
            except Exception as error:
                logger.error(
                    f"Could not supervise the List Recording for "
                    f"{person.clean_name}: {error}"
                )

    def _retry_failed_starts(self, moment) -> None:
        """Retry a first start that never took, once its wait has passed.

        Only a recorder that reports ``stopped`` or ``disk-stop`` is retried.
        A recorder that reports ``waiting-to-retry`` is already being
        reconnected by the engine after its own wait, and starting it here
        would cut that wait short.
        """
        for person in self._people.values():
            if person.given_up:
                continue
            try:
                status = person.recorder.status()
            except Exception as error:
                logger.error(
                    f"Could not read the state of the List Recording for "
                    f"{person.clean_name}: {error}"
                )
                continue
            if status not in (STATUS_STOPPED, STATUS_DISK_STOP):
                continue
            if person.next_attempt is not None and moment < person.next_attempt:
                continue
            self._start_person(person, moment)

    def _start_person(self, person: _Person, moment) -> bool:
        """Start one recording, count a failure, and report whether it ran."""
        try:
            started = person.recorder.start(moment)
        except Exception as error:
            logger.error(
                f"Could not start the List Recording for {person.clean_name}: {error}"
            )
            started = False

        if started:
            person.failed_starts = 0
            person.next_attempt = None
            logger.info(f"List Recording started for {person.clean_name}.")
            return True

        person.failed_starts += 1
        if person.failed_starts >= self._settings.retry_limit:
            person.given_up = True
            person.next_attempt = None
            # One line, here where the limit is reached: later cycles stay
            # quiet about this person.
            logger.warning(
                f"{person.clean_name} could not start recording after "
                f"{person.failed_starts} attempt(s), the Retry Limit; giving up "
                f"while they stay on the Stream List."
            )
            return False

        person.next_attempt = moment + datetime.timedelta(
            seconds=self._settings.reconnect_seconds
        )
        logger.warning(
            f"Could not start the List Recording for {person.clean_name} "
            f"(attempt {person.failed_starts} of {self._settings.retry_limit}); "
            f"trying again in {self._settings.reconnect_seconds:g} seconds."
        )
        return False

    # ------------------------------------------------------------------
    # The running count
    # ------------------------------------------------------------------

    def _announce_running_count(self) -> None:
        """Log the number of running recordings, but only when it changes."""
        running = 0
        for person in self._people.values():
            try:
                if person.recorder.is_running():
                    running += 1
            except Exception as error:
                logger.debug(
                    f"Could not tell whether {person.clean_name} is recording: {error}"
                )

        if running == self._running_count:
            return
        self._running_count = running
        if running == 0:
            logger.info("No List Recordings are running now.")
        elif running == 1:
            logger.info("One person is recording.")
        else:
            logger.info(f"{running} people are recording.")


# ---------------------------------------------------------------------------
# The program
# ---------------------------------------------------------------------------

def configure_logging(level_name: str) -> None:
    """Send log lines to the console and to ``record_streams.log``.

    Mirrors the automator's setup: the same line format, a file next to this
    program, and UTF-8 both ways, so a non-Latin Clean Name survives. The
    console is reconfigured to UTF-8 here for the same reason. The automator's
    own log file is never opened.
    """
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), LOG_FILENAME)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def read_list_entries(
    address: str, timeout: float
) -> Optional[List[ListedStream]]:
    """Read one Stream List reply and return its people, or None if unreadable.

    "Unreadable" covers both a request that could not be made (the bridge is
    down, refused the connection, or was too slow) and a reply that is not a
    JSON array. Both prove nothing about who is streaming, so both reach the
    caller as None: every recording keeps running, and the caller writes the
    one warning and the recovery line. Problems with single items are logged
    at debug, and the rest of that reply is still handled.
    """
    try:
        text = fetch_stream_list_text(address, timeout)
    except Exception as error:
        # The reason matters to whoever reads the log, but the caller owns the
        # visible wording, so this stays a debug note.
        logger.debug(f"Could not read the Stream List at '{address}': {error}")
        return None

    entries, problems = parse_stream_list(text)
    for problem in problems:
        logger.debug(problem)
    if not _is_json_array(text):
        return None
    return entries


def main() -> int:
    """Run the List Recorder until the user stops it.

    Returns the exit code: 1 when the recorder program is missing (nothing
    could ever be recorded), 0 after Ctrl+C has closed every Clip. Every other
    fault is logged and the loop carries on.
    """
    load_dotenv()
    settings = load_streams_settings()
    configure_logging(settings.log_level)
    _log_startup(settings)

    if not ffmpeg_available(build_engine_settings(settings, "")):
        logger.error(
            f"The recorder program '{settings.ffmpeg_path}' was not found, so "
            f"nothing could be recorded. Set STREAMS_FFMPEG_PATH to the recorder "
            f"program."
        )
        return 1

    farm = ListRecorderFarm(settings)
    timeout = list_request_timeout(settings.poll_seconds)

    try:
        while True:
            started = time.monotonic()
            try:
                entries = read_list_entries(settings.list_url, timeout)
                farm.cycle(datetime.datetime.now(), entries)
            except Exception as error:
                logger.error(
                    f"Unexpected problem while reading the Stream List: {error}"
                )
            _sleep_rest_of_poll(settings.poll_seconds, started)
    except KeyboardInterrupt:
        logger.info(
            "The user stopped the List Recorder; closing every List Recording."
        )
        farm.stop_all()
        return 0


def _log_startup(settings: StreamsSettings) -> None:
    """Log the values the user needs to confirm the program is set up right."""
    logger.info(f"Stream List address: {settings.list_url}")
    logger.info(f"Clips folder: {settings.output_dir}")
    logger.info(
        f"Poll interval: {settings.poll_seconds:g} seconds; Clip length: "
        f"{settings.clip_seconds} seconds; Retry Limit: {settings.retry_limit}."
    )


def _sleep_rest_of_poll(poll_seconds: float, started: float) -> None:
    """Sleep the part of the poll interval the list work did not use."""
    remaining = poll_seconds - (time.monotonic() - started)
    if remaining > 0:
        time.sleep(remaining)


def _is_json_array(text: str) -> bool:
    """Return True when *text* is a JSON array, whatever is inside it."""
    try:
        return isinstance(json.loads(text), list)
    except (ValueError, TypeError):
        return False


def _gigabytes(free_bytes: int) -> str:
    """Format a byte count as gigabytes for a log line."""
    return f"{free_bytes / BYTES_PER_GB:.1f}"


if __name__ == "__main__":
    sys.exit(main())
