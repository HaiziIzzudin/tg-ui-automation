"""Owns everything about talking to OBS: the thin conversation layer, the
password guard, and the engine that decides when the stream should be live.

This module knows nothing about windows, the screen, the Schedule, or
recording. The caller says whether a stream is wanted right now and supplies
the moment; everything else is answered here.

OBS is reached over its own WebSocket server, one fresh conversation per
question: the library connects and authenticates inside its constructor, the
question is asked, and the socket is closed again straight away. Nothing is
held between checks, so there is no stale socket to recover from and no
reconnect logic. The library writes the OBS password into an information line
and a traceback on every failed connection, so its own logger tree is silenced
at import time and this module writes the lines instead.

Every failure is funnelled into :class:`OBSUnavailable` carrying a short human
reason, and no public function here ever raises, retries, or sleeps: one
question, one answer, hand back to the caller. The Restart Budget is the only
patience this module has, and it is spent on reopening OBS after silence.
"""

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import List, Tuple

import psutil

logger = logging.getLogger(__name__)

# The library logs the OBS password at information level and a full traceback
# on every failed connection. Above CRITICAL is "off": no record from the
# library tree reaches the automator's log, so the password never lands there.
logging.getLogger("obsws_python").setLevel(logging.CRITICAL + 1)

# The OBS program on Windows, and the two launch parameters OBS documents for
# a quiet start: folded into the tray, and no missing-files dialog to click.
OBS_EXE = "obs64.exe"
OBS_LAUNCH_FLAGS = ("--minimize-to-tray", "--disable-missing-files-check")

# The two answers OBS gives about its stream output.
STATE_LIVE = "live"
STATE_IDLE = "idle"

# A child that outlives the automator must not hold its console, so it is
# started detached. POSIX has no such flag; the value stays zero there.
_DETACHED_FLAGS = getattr(subprocess, "DETACHED_PROCESS", 0)


@dataclass(frozen=True)
class BroadcastSettings:
    """Everything broadcasting needs, already resolved by the caller.

    The four tuning fields are last and defaulted, so the caller never sets
    them and tests shorten them by keyword. ``obs_path`` is used as given: a
    missing file is reported once at startup and broadcasting stays off for
    the run. ``password`` may be empty; OBS itself then refuses the connection
    and the reason is logged once.
    """

    enabled: bool
    host: str
    port: int
    password: str
    obs_path: str
    request_timeout_seconds: float = 3.0
    silence_seconds: float = 120.0
    restart_limit: int = 3
    exit_wait_seconds: float = 5.0


class OBSUnavailable(Exception):
    """The one failure type the conversation layer raises.

    The message is a short human reason ("connection refused", "wrong
    password", "request timed out") with the library's own class name and
    message in brackets, so the log says why without a traceback. The
    ``Broadcaster`` catches it; nothing above this module ever sees it.
    """


def _reason_for(error: BaseException) -> str:
    """Return a short human reason for one failure.

    The library raises a mixed bag that shares no common base, so the class
    name and the message are all that can be reported. A few well-known shapes
    get plainer words first; the original always appears in brackets. The OBS
    password is never part of any of these messages.
    """
    name = type(error).__name__
    message = str(error).strip()
    lowered = message.lower()

    if isinstance(error, ImportError):
        plain = "the OBS library is not installed"
    elif (
        "authentication" in lowered
        or "password" in lowered
        or "identify client" in lowered
        or "connection settings" in lowered
    ):
        # A wrong password and a wrong host or port answer with the same
        # library error: the client could not identify itself. OBS is
        # reachable and refusing, which is a different problem from a closed
        # OBS, so the reason says so in plain words.
        plain = "wrong password or wrong connection settings"
    elif name == "ConnectionRefusedError":
        plain = "connection refused"
    elif name in ("TimeoutError", "WebSocketTimeoutException", "OBSSDKTimeoutError"):
        plain = "request timed out"
    elif name == "WebSocketConnectionClosedException":
        plain = "the connection was closed"
    else:
        plain = "OBS could not be reached"

    if message:
        return f"{plain} ({name}: {message})"
    return f"{plain} ({name})"


# ---------------------------------------------------------------------------
# Talking to OBS: one conversation, one answer
# ---------------------------------------------------------------------------

def _open_client(settings: BroadcastSettings):
    """Connect to OBS and return the client, or raise :class:`OBSUnavailable`.

    The library is imported here, not at module import, so this module loads
    fine on a machine where it is missing; a missing library then surfaces as
    an ordinary "OBS could not be reached" reason. The constructor connects and
    authenticates, and the timeout is always passed because the library's own
    default is to wait forever.
    """
    try:
        import obsws_python
    except Exception as error:
        raise OBSUnavailable(_reason_for(error)) from error

    try:
        return obsws_python.ReqClient(
            host=settings.host,
            port=settings.port,
            password=settings.password,
            timeout=settings.request_timeout_seconds,
        )
    except Exception as error:
        raise OBSUnavailable(_reason_for(error)) from error


def read_stream_state(settings: BroadcastSettings) -> str:
    """Ask OBS about its stream output and return ``"live"`` or ``"idle"``.

    A stream that is reconnecting on its own counts as live: OBS is working on
    it and the automator must stay out of the way. Raises
    :class:`OBSUnavailable` with a readable reason when OBS does not answer.
    """
    client = _open_client(settings)
    try:
        with client:
            status = client.get_stream_status()
    except Exception as error:
        raise OBSUnavailable(_reason_for(error)) from error

    if getattr(status, "output_active", False) or getattr(
        status, "output_reconnecting", False
    ):
        return STATE_LIVE
    return STATE_IDLE


def start_stream(settings: BroadcastSettings) -> None:
    """Ask OBS to start streaming. Raises :class:`OBSUnavailable` on refusal."""
    client = _open_client(settings)
    try:
        with client:
            client.start_stream()
    except Exception as error:
        raise OBSUnavailable(_reason_for(error)) from error


def stop_stream(settings: BroadcastSettings) -> None:
    """Ask OBS to stop streaming. Raises :class:`OBSUnavailable` on failure."""
    client = _open_client(settings)
    try:
        with client:
            client.stop_stream()
    except Exception as error:
        raise OBSUnavailable(_reason_for(error)) from error


# ---------------------------------------------------------------------------
# Talking to OBS: the process on this machine
# ---------------------------------------------------------------------------

def _obs_processes() -> List[object]:
    """Return the running OBS processes, in the style of the Telegram check.

    A process that dies or cannot be inspected is skipped, so a scan never
    fails because of one process. Tests replace this function; no test looks at
    a real process list.
    """
    found = []
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info["name"]
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if name and name.lower() == OBS_EXE.lower():
            found.append(proc)
    return found


def obs_running() -> bool:
    """Return True when the OBS program is running on this machine.

    A scan that cannot be done at all is a debug note and False: the automator
    keeps its window job either way.
    """
    try:
        return bool(_obs_processes())
    except Exception as error:
        logger.debug(f"Could not look for the OBS process: {error}")
        return False


def launch_obs(settings: BroadcastSettings) -> bool:
    """Start OBS folded into the tray; return whether it was started.

    OBS must run with its own program folder as the working folder, or it
    fails to start properly. The child is detached and its output is discarded,
    so the call returns as soon as the process exists and the automator is
    never blocked by an OBS window. A missing program file or a failed launch
    is one error and False; this never raises.
    """
    path = (settings.obs_path or "").strip()
    if not path or not os.path.isfile(path):
        logger.error(
            f"Cannot open OBS: the program file '{path}' was not found. "
            "Set BROADCAST_OBS_PATH to the OBS program file."
        )
        return False

    folder = os.path.dirname(path) or "."
    try:
        subprocess.Popen(
            [path, *OBS_LAUNCH_FLAGS],
            cwd=folder,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_DETACHED_FLAGS,
        )
    except Exception as error:
        logger.error(f"Could not open OBS from '{path}': {error}")
        return False
    return True


def quit_obs(settings: BroadcastSettings) -> None:
    """Close OBS by asking, then forcing it, and never raise.

    Every running OBS process is terminated and given ``exit_wait_seconds`` to
    leave, then killed and given the same time to be really gone. One line is
    written per stage that fails, so the log says where the close got stuck.
    """
    try:
        processes = _obs_processes()
    except Exception as error:
        logger.warning(f"Could not look for OBS to close it: {error}")
        return

    if not processes:
        logger.debug("OBS is not running, so there is nothing to close.")
        return

    for proc in processes:
        _close_process(proc, settings.exit_wait_seconds)


def _close_process(proc, exit_wait_seconds: float) -> None:
    """Terminate one OBS process, then kill it if it lingers.

    Each stage that fails writes one line; a process that is already gone is
    not a failure, so the close never turns into noise on a normal exit.
    """
    try:
        proc.terminate()
    except Exception as error:
        logger.warning(f"Could not ask OBS to close: {error}")

    try:
        proc.wait(timeout=exit_wait_seconds)
        return
    except Exception as error:
        logger.warning(
            f"OBS did not close within {exit_wait_seconds} seconds ({error}); "
            "killing it."
        )

    try:
        proc.kill()
    except Exception as error:
        logger.warning(f"Could not kill OBS: {error}")

    try:
        proc.wait(timeout=exit_wait_seconds)
    except Exception as error:
        logger.warning(f"OBS is still running after a kill: {error}")


def check_report(settings: BroadcastSettings) -> Tuple[List[str], bool]:
    """Return the report lines only OBS can answer, and whether OBS answered.

    The lines say whether the program file is there, whether a password is set
    (yes or no, never the value), whether OBS is running, and what OBS answers
    about the stream or why it could not be asked. Nothing is opened, started,
    stopped, or killed here, and this never raises.
    """
    lines = []
    try:
        path = (settings.obs_path or "").strip()
        if path and os.path.isfile(path):
            lines.append(f"OBS program: found at '{path}'.")
        else:
            lines.append(f"OBS program: not found at '{path or '(no path set)'}'.")
        if settings.password.strip():
            lines.append("OBS password: set.")
        else:
            lines.append("OBS password: not set.")
        if obs_running():
            lines.append("OBS running: yes.")
        else:
            lines.append("OBS running: no.")
    except Exception as error:
        logger.error(f"Unexpected problem while reading the OBS report: {error}")
        return lines, False

    try:
        state = read_stream_state(settings)
    except OBSUnavailable as error:
        lines.append(f"OBS stream: could not be asked ({error}).")
        return lines, False
    except Exception as error:
        lines.append(f"OBS stream: could not be asked ({_reason_for(error)}).")
        return lines, False

    lines.append(f"OBS stream: {state}.")
    return lines, True


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

class Broadcaster:
    """Decides, at every monitor check, whether the stream should be live.

    All state across checks lives here: the last known live flag, the moment
    OBS went silent, the Restart Budget used and the period it belongs to, the
    exhausted flag, and the last trouble already logged so a repeated failure
    stays quiet. The stop work is remembered per episode too, so a cycle that
    runs every three seconds cannot ask OBS the same question over and over.
    The caller passes the moment and whether a stream is wanted; this object
    never reads the clock and never raises.

    Logging is on change only: one line when the stream goes live, one when it
    drops, one when trouble appears, one when it clears. A steady state writes
    nothing above debug.
    """

    def __init__(self, settings: BroadcastSettings):
        self._settings = settings
        self._live = False
        self._silent_since = None
        self._restarts = 0
        self._budget_period = None
        self._exhausted = False
        self._trouble = None
        self._disabled_for_run = False
        # Whether the stop work of this episode is already done. An episode is
        # one stretch of "no stream wanted"; a wanted check starts a new one.
        self._stop_done = False

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def prepare(self, dry_run: bool = False) -> None:
        """Say whether broadcasting is on, and open OBS once when it is.

        A rehearsal opens nothing and starts nothing. A missing program file is
        one error, and broadcasting stays off for the rest of the run. OBS that
        is already running is left alone. Never raises.
        """
        try:
            self._prepare(dry_run)
        except Exception as error:
            logger.error(f"Unexpected problem while preparing broadcasting: {error}")

    def update(self, moment, wanted: bool) -> None:
        """Do one Broadcast Check and keep the stream in the wanted state.

        *moment* is the caller's clock; *wanted* says whether a stream should
        be live right now. Everything below is a decision, not a wait: at most
        one conversation with OBS per step. Never raises.
        """
        try:
            self._update(moment, wanted)
        except Exception as error:
            logger.error(f"Unexpected problem in the Broadcast Check: {error}")

    def stop(self) -> None:
        """Stop a live stream once, then forget every state. Never raises.

        Safe to call every cycle and safe to call when nothing was ever
        prepared. With broadcasting off for this run it says one debug line and
        does not talk to OBS at all: the switch being off means untouched. When
        it is on, the leftover-stream check happens once and later calls stay
        quiet until a wanted check starts a new episode.
        """
        try:
            self._end_stream("the period ended or the automator is stopping")
        except Exception as error:
            logger.error(f"Unexpected problem while stopping the stream: {error}")
        self._forget()

    def is_live(self) -> bool:
        """Return the last known state of the stream."""
        return bool(self._live)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _prepare(self, dry_run: bool) -> None:
        """The body of :meth:`prepare`, guarded by the caller."""
        if dry_run:
            logger.debug(
                "Broadcast: rehearsal, so OBS is not opened and nothing goes live."
            )
            return

        if not self._settings.enabled:
            logger.info("Broadcast: off.")
            return

        path = (self._settings.obs_path or "").strip()
        if not path or not os.path.isfile(path):
            logger.error(
                f"Broadcast: the OBS program was not found at '{path}'. "
                "Broadcasting is off for this run; set BROADCAST_OBS_PATH to "
                "the OBS program file."
            )
            self._disabled_for_run = True
            return

        logger.info(
            f"Broadcast: on, OBS at {self._settings.host}:{self._settings.port}."
        )

        if obs_running():
            logger.debug("Broadcast: OBS is already running; leaving it alone.")
            return

        if launch_obs(self._settings):
            logger.info("Broadcast: opened OBS and left it in the tray.")
        else:
            # launch_obs already said why; the next wanted check tries again.
            logger.debug("Broadcast: OBS could not be opened at startup.")

    # ------------------------------------------------------------------
    # The per-check decision
    # ------------------------------------------------------------------

    def _update(self, moment, wanted: bool) -> None:
        """The body of :meth:`update`, guarded by the caller.

        The order is the specification: not wanted, then the new period, then
        the process, then what OBS says about the stream.
        """
        if not self._is_wanted(wanted):
            self._handle_not_wanted(wanted)
            return

        self._begin_period_if_needed(moment)

        if not obs_running():
            self._open_obs()
            return

        try:
            state = read_stream_state(self._settings)
        except OBSUnavailable as error:
            self._handle_silence(moment, str(error))
            return
        except Exception as error:
            self._handle_silence(moment, _reason_for(error))
            return

        if state == STATE_LIVE:
            self._handle_live()
            return

        self._handle_idle()

    def _is_wanted(self, wanted: bool) -> bool:
        """Return True only when this check may keep a stream live.

        The caller's flag already carries the clock and the Broadcast Switch;
        the switch and a program missing at startup are checked again here so
        the engine is safe on its own.
        """
        if self._disabled_for_run:
            return False
        if not self._settings.enabled:
            return False
        return bool(wanted)

    def _handle_not_wanted(self, wanted: bool) -> None:
        """Look after a live stream once, then forget the rest of the state.

        The stop work is the same as :meth:`stop` uses, so a long stretch
        outside a period looks at OBS once and then writes nothing at all.
        Nothing is opened and nothing is killed: a stream is only ever stopped.
        """
        self._end_stream(self._not_wanted_reason(wanted))
        self._live = False
        self._silent_since = None
        self._trouble = None
        self._budget_period = None

    def _not_wanted_reason(self, wanted: bool) -> str:
        """Name, in plain words, why no stream is wanted right now."""
        if self._disabled_for_run:
            return "the OBS program was not found at startup"
        if not self._settings.enabled:
            return "the broadcast switch is off"
        if not wanted:
            return "it is a rehearsal or not an allowed period"
        return "broadcasting is not wanted"

    def _begin_period_if_needed(self, moment) -> None:
        """Start a fresh Restart Budget on the first wanted check of a period."""
        if self._budget_period is not None:
            return
        self._budget_period = moment
        self._restarts = 0
        self._exhausted = False
        # A new period ends the quiet of the last one: its end gets its own
        # stop, and its own leftover check.
        self._stop_done = False
        logger.debug("Broadcast: an allowed period starts; the Restart Budget is fresh.")

    def _open_obs(self) -> None:
        """Open a closed OBS, spending one use of the Restart Budget."""
        limit = self._settings.restart_limit
        if self._restarts >= limit:
            self._note_exhausted(
                "OBS keeps failing; not reopening it until the next allowed "
                f"period ({self._restarts} of {limit} reopenings used)."
            )
            return

        self._restarts += 1
        if launch_obs(self._settings):
            logger.info(
                "Broadcast: OBS was not running; opened it "
                f"({self._restarts} of {limit})."
            )
        else:
            logger.debug(
                f"Broadcast: opening OBS failed ({self._restarts} of {limit} used)."
            )

    def _handle_live(self) -> None:
        """OBS says the stream is live, or reconnecting on its own.

        Either way the automator stays out of the way. The silence clock and
        the trouble memory are cleared, and one line is written only when the
        state really changed.
        """
        changed = not self._live
        self._live = True
        self._silent_since = None
        self._trouble = None
        if changed:
            logger.info("Broadcast is live.")
        else:
            logger.debug("Broadcast: OBS still reports the stream as live.")

    def _handle_idle(self) -> None:
        """OBS answered but no stream runs: start it, then read once to confirm."""
        self._silent_since = None

        if self._live:
            logger.warning("Broadcast dropped; starting it again.")
        self._live = False

        try:
            start_stream(self._settings)
        except OBSUnavailable as error:
            self._note_trouble(f"the stream did not start ({error})")
            return
        except Exception as error:
            self._note_trouble(f"the stream did not start ({_reason_for(error)})")
            return

        try:
            state = read_stream_state(self._settings)
        except OBSUnavailable as error:
            self._note_trouble(f"OBS did not confirm the start ({error})")
            return
        except Exception as error:
            self._note_trouble(
                f"OBS did not confirm the start ({_reason_for(error)})"
            )
            return

        if state != STATE_LIVE:
            self._note_trouble(
                "OBS accepted the start but still reports the stream as idle"
            )
            return

        self._live = True
        self._trouble = None
        logger.info("Broadcast started.")

    def _handle_silence(self, moment, reason: str) -> None:
        """OBS did not answer: run the silence clock and reopen it once a while.

        The first failure starts the clock and writes one warning naming the
        reason; further failures inside ``silence_seconds`` stay at debug. At
        ``silence_seconds`` OBS is closed and opened again, one budget use, with
        a fresh clock. With no budget left there is one error and then quiet,
        but the question is still asked every check, so a healed OBS resumes by
        itself.
        """
        if self._silent_since is None:
            self._silent_since = moment

        if reason != self._trouble:
            self._trouble = reason
            logger.warning(f"Broadcast: OBS is not answering ({reason}).")
        else:
            logger.debug(f"Broadcast: OBS is still not answering ({reason}).")

        elapsed = (moment - self._silent_since).total_seconds()
        if elapsed < self._settings.silence_seconds:
            return

        limit = self._settings.restart_limit
        if self._restarts >= limit:
            self._note_exhausted(
                "OBS stopped answering and the reopenings for this period are "
                f"used up ({self._restarts} of {limit}); it will not be "
                "reopened again until the next allowed period. Still asking "
                "every check, so it resumes by itself when it answers again."
            )
            return

        quit_obs(self._settings)
        self._restarts += 1
        reopened = launch_obs(self._settings)
        # A fresh clock: the new OBS gets its own two minutes before anything
        # else is killed. The trouble memory stays, because the same reason is
        # still the same reason and the log only speaks on change.
        self._silent_since = None
        logger.warning(
            "OBS stopped answering; reopened it "
            f"(attempt {self._restarts} of {limit})."
        )
        if not reopened:
            logger.debug(
                "Broadcast: the reopened OBS did not start; the next check tries again."
            )

    def _note_trouble(self, trouble: str) -> None:
        """Log one warning when a trouble appears, and debug while it lasts."""
        self._live = False
        if trouble == self._trouble:
            logger.debug(f"Broadcast: {trouble}.")
            return
        self._trouble = trouble
        logger.warning(f"Broadcast: {trouble}; trying again at the next check.")

    def _note_exhausted(self, message: str) -> None:
        """Write the one error that says the Restart Budget is used up."""
        if self._exhausted:
            logger.debug("Broadcast: the Restart Budget is still used up.")
            return
        self._exhausted = True
        logger.error(f"Broadcast: {message}")

    # ------------------------------------------------------------------
    # Stopping and forgetting
    # ------------------------------------------------------------------

    def _end_stream(self, reason: str) -> None:
        """Stop a live stream once per episode, never once per check.

        With broadcasting off for this run nothing is asked of OBS at all: the
        switch being off means OBS is untouched, and a missing program means
        there is nothing to ask. Otherwise the first call of an episode looks at
        the stream — this is what still finds a stream left behind by an
        earlier run — stops it when it is live, and marks the episode done, so
        the next cycle writes nothing. A wanted check starts a new episode.
        """
        if not self._settings.enabled:
            logger.debug("Broadcast: off, so no stream is looked for or stopped.")
            self._stop_done = True
            return

        if self._disabled_for_run:
            logger.debug(
                "Broadcast: the OBS program was missing at startup, so no "
                "stream is looked for or stopped."
            )
            self._stop_done = True
            return

        if self._stop_done:
            logger.debug("Broadcast: the stop of this episode is already done.")
            return

        self._stop_done = True
        self._stop_stream_if_live(reason)

    def _stop_stream_if_live(self, reason: str) -> None:
        """Stop the stream when OBS says one is running. Never raises.

        Reading first is what catches a stream left behind by an earlier run:
        nothing else in the automator can know it is there. OBS that cannot be
        reached is one warning, and the caller carries on.
        """
        try:
            state = read_stream_state(self._settings)
        except OBSUnavailable as error:
            logger.warning(f"Could not reach OBS to stop the stream ({error}).")
            return
        except Exception as error:
            logger.warning(
                f"Could not reach OBS to stop the stream ({_reason_for(error)})."
            )
            return

        if state != STATE_LIVE:
            logger.debug("Broadcast: no stream is running, so nothing is stopped.")
            return

        try:
            stop_stream(self._settings)
        except OBSUnavailable as error:
            logger.warning(f"Could not stop the stream ({error}).")
            return
        except Exception as error:
            logger.warning(f"Could not stop the stream ({_reason_for(error)}).")
            return

        self._live = False
        logger.info(f"Broadcast: the stream is stopped ({reason}).")

    def _forget(self) -> None:
        """Drop every remembered state, so a later check starts from scratch.

        The stop-done flag is kept on purpose: forgetting it would make the very
        next cycle ask OBS the same question again, every three seconds, which
        is exactly what must not happen outside an allowed period.
        """
        self._live = False
        self._silent_since = None
        self._restarts = 0
        self._budget_period = None
        self._exhausted = False
        self._trouble = None
