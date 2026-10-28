"""Tests for the broadcaster module.

The module alone, with the OBS conversation layer, the library, and the process
helpers all replaced by fakes. Nothing real is touched: no socket is opened, no
process is started, no real process list is read, and no OBS file is opened.
The password is only ever a test string, and one test proves it never reaches
the log.
"""
import dataclasses
import datetime
import logging
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import broadcaster
from broadcaster import (
    BroadcastSettings,
    Broadcaster,
    OBSUnavailable,
    check_report,
    launch_obs,
    obs_running,
    quit_obs,
    read_stream_state,
    start_stream,
    stop_stream,
)

# A password that would be obvious if it ever reached the log.
_PASSWORD = "correct-horse-battery-staple"

# 2024-01-01 is a Monday; every moment here is measured from it.
_MONDAY = datetime.datetime(2024, 1, 1)


def at(seconds: float) -> datetime.datetime:
    """Return a fixed moment *seconds* after the start of the test run."""
    return _MONDAY + datetime.timedelta(seconds=seconds)


def make_settings(**overrides) -> BroadcastSettings:
    """Return usable settings, with keywords shortening the tuning fields."""
    values = dict(
        enabled=True,
        host="127.0.0.1",
        port=4455,
        password=_PASSWORD,
        obs_path=r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
        request_timeout_seconds=0.5,
        silence_seconds=120.0,
        restart_limit=3,
        exit_wait_seconds=0.2,
    )
    values.update(overrides)
    return BroadcastSettings(**values)


@pytest.fixture(autouse=True)
def capture_everything(caplog):
    """Capture every line the module writes, at debug and above."""
    caplog.set_level(logging.DEBUG)
    return caplog


def lines(caplog, level: int = 0) -> list:
    """Return the captured messages at *level* or above."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= level
    ]


class FakeOBS:
    """Stands in for the wrapper functions and the process helpers.

    ``state`` is what OBS reports. Starting and stopping change it exactly as a
    real OBS would, so a second call can never stop the same stream twice.
    Every conversation is counted, so a test can prove what was never asked.
    """

    def __init__(self, state=broadcaster.STATE_IDLE, running=True, launch_result=True):
        self.state = state
        self.running = running
        self.launch_result = launch_result
        self.reads = 0
        self.starts = 0
        self.stops = 0
        self.launches = 0
        self.quits = 0
        self.read_error = None
        self.start_error = None
        self.stop_error = None
        # What OBS reports after the start was asked for; None means "live".
        self.state_after_start = None

    def read(self, settings):
        self.reads += 1
        if self.read_error is not None:
            raise self.read_error
        return self.state

    def start(self, settings):
        self.starts += 1
        if self.start_error is not None:
            raise self.start_error
        if self.state_after_start is None:
            self.state = broadcaster.STATE_LIVE
        else:
            self.state = self.state_after_start

    def stop(self, settings):
        self.stops += 1
        if self.stop_error is not None:
            raise self.stop_error
        self.state = broadcaster.STATE_IDLE

    def is_running(self):
        return self.running

    def launch(self, settings):
        self.launches += 1
        if self.launch_result:
            self.running = True
        return self.launch_result

    def quit(self, settings):
        self.quits += 1
        self.running = False

    def install(self, monkeypatch):
        """Put this fake in place of every wrapper and process helper."""
        monkeypatch.setattr(broadcaster, "read_stream_state", self.read)
        monkeypatch.setattr(broadcaster, "start_stream", self.start)
        monkeypatch.setattr(broadcaster, "stop_stream", self.stop)
        monkeypatch.setattr(broadcaster, "obs_running", self.is_running)
        monkeypatch.setattr(broadcaster, "launch_obs", self.launch)
        monkeypatch.setattr(broadcaster, "quit_obs", self.quit)
        return self


class FakeStatus:
    """The few fields of the library's stream status that are used."""

    def __init__(self, output_active=False, output_reconnecting=False):
        self.output_active = output_active
        self.output_reconnecting = output_reconnecting


class FakeClient:
    """A stand-in for the library's request client.

    It connects in the constructor and pretends to log the password, exactly as
    the real library does, so the silencing can be proven. Class-level switches
    let one test make the constructor, a request, or a start fail.
    """

    last_arguments = None
    last_instance = None
    fail_on_connect = None
    status_error = None
    start_error = None
    stop_error = None
    status = FakeStatus()

    def __init__(self, **kwargs):
        FakeClient.last_arguments = kwargs
        FakeClient.last_instance = self
        logging.getLogger("obsws_python.baseclient").info(
            "Connecting with parameters: host='{host}' port={port} "
            "password='{password}' timeout={timeout}".format(**{
                "host": kwargs.get("host"),
                "port": kwargs.get("port"),
                "password": kwargs.get("password"),
                "timeout": kwargs.get("timeout"),
            })
        )
        if FakeClient.fail_on_connect is not None:
            raise FakeClient.fail_on_connect
        self.closed = False
        self.started = 0
        self.stopped = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.closed = True
        return False

    def disconnect(self):
        self.closed = True

    def get_stream_status(self):
        if FakeClient.status_error is not None:
            raise FakeClient.status_error
        return FakeClient.status

    def start_stream(self):
        if FakeClient.start_error is not None:
            raise FakeClient.start_error
        self.started += 1

    def stop_stream(self):
        if FakeClient.stop_error is not None:
            raise FakeClient.stop_error
        self.stopped += 1


@pytest.fixture
def fake_library(monkeypatch):
    """Install the stand-in library and reset its switches for one test."""
    FakeClient.last_arguments = None
    FakeClient.last_instance = None
    FakeClient.fail_on_connect = None
    FakeClient.status_error = None
    FakeClient.start_error = None
    FakeClient.stop_error = None
    FakeClient.status = FakeStatus()
    module = types.SimpleNamespace(ReqClient=FakeClient)
    monkeypatch.setitem(sys.modules, "obsws_python", module)
    return FakeClient


class FakeProcess:
    """A psutil-like process that either closes politely or must be killed."""

    def __init__(self, name="obs64.exe", closes_politely=True):
        self.info = {"name": name}
        self.terminated = 0
        self.killed = 0
        self.waits = []
        self.closes_politely = closes_politely
        self.alive = True

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1
        self.alive = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.terminated and self.closes_politely:
            self.alive = False
            return 0
        if self.killed:
            self.alive = False
            return 0
        raise TimeoutError("still running")


def install_processes(monkeypatch, processes):
    """Make the module's process scan see *processes*.

    Only the OBS ones are returned, exactly as the real scan does, so a fake
    that also holds a Telegram process proves the name filter.
    """
    calls = {"scans": 0}

    def scan():
        calls["scans"] += 1
        return [proc for proc in processes if proc.info["name"].lower() == "obs64.exe"]

    monkeypatch.setattr(broadcaster, "_obs_processes", scan)
    return calls


@pytest.fixture
def no_program(monkeypatch):
    """Make every program-file question answer "not there"."""
    monkeypatch.setattr(broadcaster.os.path, "isfile", lambda path: False)


@pytest.fixture
def program_there(monkeypatch):
    """Make only the configured OBS program answer "there"."""
    monkeypatch.setattr(
        broadcaster.os.path, "isfile", lambda path: path == make_settings().obs_path
    )


# ---------------------------------------------------------------------------
# Settings and surface
# ---------------------------------------------------------------------------

class TestSettings:
    def test_tuning_fields_are_last_with_their_defaults(self):
        names = [field.name for field in dataclasses.fields(BroadcastSettings)]
        assert names == [
            "enabled",
            "host",
            "port",
            "password",
            "obs_path",
            "request_timeout_seconds",
            "silence_seconds",
            "restart_limit",
            "exit_wait_seconds",
        ]
        settings = BroadcastSettings(
            enabled=True,
            host="127.0.0.1",
            port=4455,
            password="",
            obs_path="x",
        )
        assert settings.request_timeout_seconds == 3.0
        assert settings.silence_seconds == 120.0
        assert settings.restart_limit == 3
        assert settings.exit_wait_seconds == 5.0

    def test_defaults_need_only_the_five_settings(self):
        settings = BroadcastSettings(
            enabled=True, host="127.0.0.1", port=4455, password="", obs_path="x"
        )
        assert settings.request_timeout_seconds == 3.0
        assert settings.silence_seconds == 120.0
        assert settings.restart_limit == 3
        assert settings.exit_wait_seconds == 5.0

    def test_settings_cannot_be_changed(self):
        settings = make_settings()
        with pytest.raises(dataclasses.FrozenInstanceError):
            settings.enabled = False

    def test_a_new_broadcaster_is_not_live(self):
        assert Broadcaster(make_settings()).is_live() is False

    def test_only_obsunavailable_carries_a_readable_reason(self):
        error = OBSUnavailable("connection refused")
        assert str(error) == "connection refused"


# ---------------------------------------------------------------------------
# Silencing the library
# ---------------------------------------------------------------------------

class TestLibraryIsSilenced:
    def test_the_library_logger_is_off(self):
        assert logging.getLogger("obsws_python").level > logging.CRITICAL

    def test_a_failing_conversation_never_logs_the_password(self, caplog, fake_library):
        fake_library.fail_on_connect = ConnectionRefusedError(10061, "refused")
        with pytest.raises(OBSUnavailable):
            read_stream_state(make_settings())

        # The stand-in wrote the library's own password line; the silencing
        # above must have dropped it.
        assert _PASSWORD not in caplog.text
        assert "Connecting with parameters" not in caplog.text

    def test_a_whole_failing_run_never_logs_the_password(
        self, caplog, monkeypatch
    ):
        fake = FakeOBS(running=True)
        fake.read_error = OBSUnavailable("connection refused")
        fake.install(monkeypatch)
        instance = Broadcaster(make_settings())

        instance.prepare()
        instance.update(at(0), True)
        instance.stop()

        assert _PASSWORD not in caplog.text
        assert _PASSWORD not in " ".join(lines(caplog, logging.INFO))


# ---------------------------------------------------------------------------
# Talking to OBS: the conversation layer
# ---------------------------------------------------------------------------

class TestReadStreamState:
    def test_active_output_is_live(self, fake_library):
        fake_library.status = FakeStatus(output_active=True)
        assert read_stream_state(make_settings()) == broadcaster.STATE_LIVE

    def test_reconnecting_output_is_live(self, fake_library):
        fake_library.status = FakeStatus(output_active=False, output_reconnecting=True)
        assert read_stream_state(make_settings()) == broadcaster.STATE_LIVE

    def test_neither_is_idle(self, fake_library):
        fake_library.status = FakeStatus()
        assert read_stream_state(make_settings()) == broadcaster.STATE_IDLE

    def test_the_settings_reach_the_library(self, fake_library):
        read_stream_state(make_settings(host="10.0.0.5", port=4456))
        arguments = fake_library.last_arguments
        assert arguments["host"] == "10.0.0.5"
        assert arguments["port"] == 4456
        assert arguments["password"] == _PASSWORD
        assert arguments["timeout"] == 0.5

    def test_the_socket_is_closed_after_one_question(self, fake_library):
        read_stream_state(make_settings())
        assert fake_library.last_instance.closed is True

    def test_a_refused_connection_is_obsunavailable(self, fake_library):
        fake_library.fail_on_connect = ConnectionRefusedError(10061, "refused")
        with pytest.raises(OBSUnavailable) as raised:
            read_stream_state(make_settings())
        assert "connection refused" in str(raised.value)
        assert "ConnectionRefusedError" in str(raised.value)

    def test_a_timeout_is_obsunavailable(self, fake_library):
        fake_library.fail_on_connect = TimeoutError("timed out")
        with pytest.raises(OBSUnavailable) as raised:
            read_stream_state(make_settings())
        assert "request timed out" in str(raised.value)

    def test_a_wrong_password_is_obsunavailable(self, fake_library):
        fake_library.fail_on_connect = RuntimeError("authentication failed")
        with pytest.raises(OBSUnavailable) as raised:
            read_stream_state(make_settings())
        assert "wrong password" in str(raised.value)

    def test_obs_refusing_the_client_is_a_wrong_password_reason(self, fake_library):
        # What OBS 1.8 answers to a wrong password: no friendly words at all,
        # so the reason must still say OBS was reached and refused.
        fake_library.fail_on_connect = RuntimeError(
            "failed to identify client with the server, please check connection settings"
        )
        with pytest.raises(OBSUnavailable) as raised:
            read_stream_state(make_settings())
        assert "wrong password or wrong connection settings" in str(raised.value)
        assert "could not be reached" not in str(raised.value)

    def test_a_failed_request_is_obsunavailable(self, fake_library):
        fake_library.status_error = RuntimeError("bad answer")
        with pytest.raises(OBSUnavailable) as raised:
            read_stream_state(make_settings())
        assert "bad answer" in str(raised.value)

    def test_a_missing_library_is_a_normal_reason(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "obsws_python", None)
        with pytest.raises(OBSUnavailable) as raised:
            read_stream_state(make_settings())
        assert "the OBS library is not installed" in str(raised.value)

    def test_the_password_is_never_in_the_reason(self, fake_library):
        fake_library.fail_on_connect = ConnectionRefusedError(10061, "refused")
        with pytest.raises(OBSUnavailable) as raised:
            read_stream_state(make_settings())
        assert _PASSWORD not in str(raised.value)


class TestStartAndStopStream:
    def test_start_asks_obs_once(self, fake_library):
        start_stream(make_settings())
        assert fake_library.last_instance.started == 1
        assert fake_library.last_instance.closed is True

    def test_stop_asks_obs_once(self, fake_library):
        stop_stream(make_settings())
        assert fake_library.last_instance.stopped == 1
        assert fake_library.last_instance.closed is True

    def test_a_refused_start_is_obsunavailable(self, fake_library):
        fake_library.start_error = RuntimeError("stream key rejected")
        with pytest.raises(OBSUnavailable) as raised:
            start_stream(make_settings())
        assert "stream key rejected" in str(raised.value)

    def test_a_failed_stop_is_obsunavailable(self, fake_library):
        fake_library.stop_error = RuntimeError("output not active")
        with pytest.raises(OBSUnavailable) as raised:
            stop_stream(make_settings())
        assert "output not active" in str(raised.value)

    def test_a_missing_library_is_a_normal_reason(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "obsws_python", None)
        with pytest.raises(OBSUnavailable):
            start_stream(make_settings())
        with pytest.raises(OBSUnavailable):
            stop_stream(make_settings())


# ---------------------------------------------------------------------------
# Talking to OBS: the process on this machine
# ---------------------------------------------------------------------------

class TestObsRunning:
    def test_obs_running_is_found_by_name(self, monkeypatch):
        install_processes(monkeypatch, [FakeProcess("Telegram.exe"), FakeProcess()])
        assert obs_running() is True

    def test_no_obs_process_means_not_running(self, monkeypatch):
        install_processes(monkeypatch, [FakeProcess("Telegram.exe")])
        assert obs_running() is False

    def test_the_name_check_ignores_case(self, monkeypatch):
        install_processes(monkeypatch, [FakeProcess("OBS64.EXE")])
        assert obs_running() is True

    def test_a_process_that_cannot_be_inspected_is_skipped(self, monkeypatch):
        class RudeProcess:
            @property
            def info(self):
                raise broadcaster.psutil.AccessDenied()

        class GoodProcess:
            info = {"name": "obs64.exe"}

        monkeypatch.setattr(
            broadcaster.psutil, "process_iter", lambda fields: [RudeProcess(), GoodProcess()]
        )
        assert obs_running() is True

    def test_a_scan_that_fails_entirely_is_false(self, monkeypatch):
        def boom():
            raise RuntimeError("no process list")

        monkeypatch.setattr(broadcaster, "_obs_processes", boom)
        assert obs_running() is False


class TestLaunchObs:
    def test_a_missing_program_is_one_error_and_false(
        self, caplog, monkeypatch, no_program
    ):
        calls = []
        monkeypatch.setattr(
            broadcaster.subprocess, "Popen", lambda *a, **k: calls.append(a)
        )
        assert launch_obs(make_settings()) is False
        assert calls == []
        errors = lines(caplog, logging.ERROR)
        assert len(errors) == 1
        assert "BROADCAST_OBS_PATH" in errors[0]

    def test_obs_is_started_in_the_tray_from_its_own_folder(
        self, monkeypatch, program_there
    ):
        calls = []

        def spy(command, **kwargs):
            calls.append((command, kwargs))

        monkeypatch.setattr(broadcaster.subprocess, "Popen", spy)
        settings = make_settings()
        assert launch_obs(settings) is True

        command, kwargs = calls[0]
        assert command[0] == settings.obs_path
        assert "--minimize-to-tray" in command
        assert "--disable-missing-files-check" in command
        assert kwargs["cwd"] == str(Path(settings.obs_path).parent)
        assert kwargs["stdout"] is not None
        assert kwargs["stderr"] is not None

    def test_a_failed_launch_is_false_and_never_raises(
        self, caplog, monkeypatch, program_there
    ):
        def boom(*args, **kwargs):
            raise OSError("access denied")

        monkeypatch.setattr(broadcaster.subprocess, "Popen", boom)
        assert launch_obs(make_settings()) is False
        assert "access denied" in lines(caplog, logging.ERROR)[0]

    def test_an_empty_path_is_not_launched(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            broadcaster.subprocess, "Popen", lambda *a, **k: calls.append(a)
        )
        assert launch_obs(make_settings(obs_path="")) is False
        assert calls == []


class TestQuitObs:
    def test_a_polite_close_terminates_and_waits(self, monkeypatch):
        process = FakeProcess()
        install_processes(monkeypatch, [process])
        quit_obs(make_settings())
        assert process.terminated == 1
        assert process.killed == 0
        assert process.waits == [0.2]

    def test_a_stubborn_obs_is_killed(self, monkeypatch):
        process = FakeProcess(closes_politely=False)
        install_processes(monkeypatch, [process])
        quit_obs(make_settings())
        assert process.terminated == 1
        assert process.killed == 1
        assert len(process.waits) == 2

    def test_every_obs_process_is_closed(self, monkeypatch):
        first = FakeProcess()
        second = FakeProcess()
        install_processes(monkeypatch, [first, second])
        quit_obs(make_settings())
        assert first.terminated == 1
        assert second.terminated == 1

    def test_nothing_running_is_harmless(self, monkeypatch):
        install_processes(monkeypatch, [])
        quit_obs(make_settings())

    def test_a_failed_scan_never_raises(self, caplog, monkeypatch):
        def boom():
            raise RuntimeError("no process list")

        monkeypatch.setattr(broadcaster, "_obs_processes", boom)
        quit_obs(make_settings())
        assert "Could not look for OBS to close it" in caplog.text

    def test_each_failing_stage_writes_one_line(self, caplog, monkeypatch):
        class RudeProcess:
            info = {"name": "obs64.exe"}

            def terminate(self):
                raise RuntimeError("cannot terminate")

            def kill(self):
                raise RuntimeError("cannot kill")

            def wait(self, timeout=None):
                raise TimeoutError("still here")

        install_processes(monkeypatch, [RudeProcess()])
        quit_obs(make_settings())
        warnings = lines(caplog, logging.WARNING)
        assert any("Could not ask OBS to close" in line for line in warnings)
        assert any("did not close within" in line for line in warnings)
        assert any("Could not kill OBS" in line for line in warnings)
        assert any("still running after a kill" in line for line in warnings)


# ---------------------------------------------------------------------------
# The check report
# ---------------------------------------------------------------------------

class TestCheckReport:
    def test_an_idle_obs_is_reported_in_full(self, monkeypatch, program_there):
        fake = FakeOBS(state=broadcaster.STATE_IDLE).install(monkeypatch)
        report, reachable = check_report(make_settings())

        assert reachable is True
        assert any("OBS program: found" in line for line in report)
        assert any("OBS password: set" in line for line in report)
        assert any("OBS running: yes" in line for line in report)
        assert any("OBS stream: idle" in line for line in report)
        assert fake.reads == 1

    def test_a_live_stream_is_reported(self, monkeypatch, program_there):
        FakeOBS(state=broadcaster.STATE_LIVE).install(monkeypatch)
        report, reachable = check_report(make_settings())
        assert reachable is True
        assert any("OBS stream: live" in line for line in report)

    def test_an_unreachable_obs_is_reported_with_its_reason(
        self, monkeypatch, program_there
    ):
        fake = FakeOBS().install(monkeypatch)
        fake.read_error = OBSUnavailable("connection refused (ConnectionRefusedError)")
        report, reachable = check_report(make_settings())

        assert reachable is False
        assert any("connection refused" in line for line in report)

    def test_a_missing_program_and_no_password_are_stated(
        self, monkeypatch, no_program
    ):
        FakeOBS().install(monkeypatch)
        report, reachable = check_report(make_settings(password=""))
        assert reachable is True
        assert any("OBS program: not found" in line for line in report)
        assert any("OBS password: not set" in line for line in report)

    def test_obs_not_running_is_reported(self, monkeypatch, program_there):
        FakeOBS(running=False).install(monkeypatch)
        report, _ = check_report(make_settings())
        assert any("OBS running: no" in line for line in report)

    def test_the_password_never_appears(self, monkeypatch, program_there):
        FakeOBS().install(monkeypatch)
        report, _ = check_report(make_settings())
        assert _PASSWORD not in " ".join(report)

    def test_nothing_is_opened_started_or_stopped(self, monkeypatch, program_there):
        fake = FakeOBS(state=broadcaster.STATE_IDLE).install(monkeypatch)
        check_report(make_settings())
        assert fake.launches == 0
        assert fake.starts == 0
        assert fake.stops == 0
        assert fake.quits == 0

    def test_a_raising_scan_still_gives_a_report(self, monkeypatch, program_there):
        def boom():
            raise RuntimeError("no process list")

        monkeypatch.setattr(broadcaster, "obs_running", boom)
        report, reachable = check_report(make_settings())
        assert reachable is False
        assert report


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

class TestPrepare:
    def test_disabled_says_one_line_and_opens_nothing(
        self, caplog, monkeypatch, program_there
    ):
        fake = FakeOBS(running=False).install(monkeypatch)
        Broadcaster(make_settings(enabled=False)).prepare()

        assert lines(caplog, logging.INFO) == ["Broadcast: off."]
        assert fake.launches == 0
        assert fake.reads == 0

    def test_enabled_names_the_target_and_opens_obs(
        self, caplog, monkeypatch, program_there
    ):
        fake = FakeOBS(running=False).install(monkeypatch)
        Broadcaster(make_settings()).prepare()

        assert any(
            "Broadcast: on, OBS at 127.0.0.1:4455." in line
            for line in lines(caplog, logging.INFO)
        )
        assert fake.launches == 1

    def test_obs_already_running_is_left_alone(self, caplog, monkeypatch, program_there):
        fake = FakeOBS(running=True).install(monkeypatch)
        Broadcaster(make_settings()).prepare()
        assert fake.launches == 0

    def test_a_rehearsal_does_nothing_at_all(self, caplog, monkeypatch, program_there):
        fake = FakeOBS(running=False).install(monkeypatch)
        Broadcaster(make_settings()).prepare(dry_run=True)

        assert lines(caplog, logging.INFO) == []
        assert fake.launches == 0
        assert fake.reads == 0
        assert fake.starts == 0

    def test_a_missing_program_is_one_error_and_no_later_launch(
        self, caplog, monkeypatch, no_program
    ):
        fake = FakeOBS(running=False).install(monkeypatch)
        instance = Broadcaster(make_settings())
        instance.prepare()

        errors = lines(caplog, logging.ERROR)
        assert len(errors) == 1
        assert "BROADCAST_OBS_PATH" in errors[0]
        assert fake.launches == 0

        # Wanted checks for the rest of the run neither launch nor complain.
        instance.update(at(0), True)
        instance.update(at(10), True)
        assert fake.launches == 0
        assert len(lines(caplog, logging.ERROR)) == 1

    def test_a_failed_startup_launch_does_not_raise(self, monkeypatch, program_there):
        fake = FakeOBS(running=False, launch_result=False).install(monkeypatch)
        Broadcaster(make_settings()).prepare()
        assert fake.launches == 1


# ---------------------------------------------------------------------------
# update: not wanted
# ---------------------------------------------------------------------------

class TestUpdateNotWanted:
    def test_a_believed_live_stream_is_stopped_once_with_the_reason(
        self, caplog, monkeypatch
    ):
        fake = FakeOBS(state=broadcaster.STATE_LIVE).install(monkeypatch)
        instance = Broadcaster(make_settings())

        instance.update(at(0), True)
        assert instance.is_live() is True

        instance.update(at(10), False)
        assert fake.stops == 1
        assert instance.is_live() is False
        assert any(
            "the stream is stopped" in line and "rehearsal" in line
            for line in lines(caplog, logging.INFO)
        )

    def test_repeated_not_wanted_checks_stay_quiet(self, caplog, monkeypatch):
        fake = FakeOBS(state=broadcaster.STATE_LIVE).install(monkeypatch)
        instance = Broadcaster(make_settings())

        instance.update(at(0), True)
        instance.update(at(10), False)
        stops_after_first = fake.stops
        reads_after_first = fake.reads
        caplog.clear()

        instance.update(at(20), False)
        instance.update(at(30), False)

        assert fake.stops == stops_after_first
        assert fake.reads == reads_after_first
        assert lines(caplog, logging.INFO) == []
        assert lines(caplog, logging.WARNING) == []

    def test_one_look_only_when_no_stream_is_believed_live(
        self, caplog, monkeypatch
    ):
        fake = FakeOBS(state=broadcaster.STATE_IDLE).install(monkeypatch)
        instance = Broadcaster(make_settings())

        instance.update(at(0), False)
        instance.update(at(3), False)

        # One look for a leftover stream, then quiet: nothing else is touched.
        assert fake.reads == 1
        assert fake.stops == 0
        assert fake.starts == 0
        assert fake.launches == 0
        assert fake.quits == 0
        assert lines(caplog, logging.INFO) == []
        assert lines(caplog, logging.WARNING) == []

    def test_an_unreachable_obs_warns_once_across_many_checks(
        self, caplog, monkeypatch
    ):
        fake = FakeOBS(state=broadcaster.STATE_IDLE).install(monkeypatch)
        fake.read_error = OBSUnavailable("connection refused")
        instance = Broadcaster(make_settings())

        for second in range(0, 15, 3):
            instance.update(at(second), False)

        warnings = [
            line for line in lines(caplog, logging.WARNING)
            if "Could not reach OBS" in line
        ]
        assert len(warnings) == 1
        assert fake.reads == 1

    def test_the_switch_being_off_touches_nothing(self, caplog, monkeypatch):
        fake = FakeOBS(state=broadcaster.STATE_LIVE).install(monkeypatch)
        instance = Broadcaster(make_settings(enabled=False))
        instance._live = True

        instance.update(at(0), True)
        instance.update(at(3), False)

        assert fake.reads == 0
        assert fake.stops == 0
        assert fake.starts == 0
        assert fake.launches == 0
        assert fake.quits == 0
        assert lines(caplog, logging.INFO) == []
        assert lines(caplog, logging.WARNING) == []
        assert lines(caplog, logging.ERROR) == []

    def test_a_stop_that_fails_is_a_warning_and_not_a_raise(
        self, caplog, monkeypatch
    ):
        fake = FakeOBS(state=broadcaster.STATE_LIVE).install(monkeypatch)
        instance = Broadcaster(make_settings())
        instance.update(at(0), True)

        fake.stop_error = OBSUnavailable("connection refused")
        instance.update(at(10), False)

        assert instance.is_live() is False
        assert "Could not stop the stream" in caplog.text


# ---------------------------------------------------------------------------
# update: wanted
# ---------------------------------------------------------------------------

class TestUpdateWanted:
    def test_a_closed_obs_is_opened_and_counts_one_budget_use(self, monkeypatch):
        fake = FakeOBS(state=broadcaster.STATE_IDLE, running=False).install(monkeypatch)
        instance = Broadcaster(make_settings())

        instance.update(at(0), True)

        assert fake.launches == 1
        assert fake.starts == 0
        assert fake.reads == 0
        assert instance._restarts == 1

    def test_the_next_check_reads_the_state_after_a_launch(self, monkeypatch):
        fake = FakeOBS(state=broadcaster.STATE_IDLE, running=False).install(monkeypatch)
        instance = Broadcaster(make_settings())

        instance.update(at(0), True)
        instance.update(at(3), True)

        assert fake.launches == 1
        assert fake.reads == 2
        assert fake.starts == 1
        assert instance.is_live() is True

    def test_an_idle_obs_is_started_and_confirmed(self, caplog, monkeypatch):
        fake = FakeOBS(state=broadcaster.STATE_IDLE).install(monkeypatch)
        instance = Broadcaster(make_settings())

        instance.update(at(0), True)

        assert fake.starts == 1
        assert fake.reads == 2
        assert instance.is_live() is True
        assert "Broadcast started." in lines(caplog, logging.INFO)

    def test_a_live_obs_is_left_alone(self, caplog, monkeypatch):
        fake = FakeOBS(state=broadcaster.STATE_LIVE).install(monkeypatch)
        instance = Broadcaster(make_settings())

        instance.update(at(0), True)
        caplog.clear()
        instance.update(at(3), True)

        assert fake.starts == 0
        assert instance.is_live() is True
        assert lines(caplog, logging.INFO) == []

    def test_going_live_is_one_information_line_only_when_it_changed(
        self, caplog, monkeypatch
    ):
        FakeOBS(state=broadcaster.STATE_LIVE).install(monkeypatch)
        instance = Broadcaster(make_settings())

        instance.update(at(0), True)
        assert lines(caplog, logging.INFO) == ["Broadcast is live."]

        caplog.clear()
        instance.update(at(3), True)
        assert lines(caplog, logging.INFO) == []

    def test_a_steady_live_stream_writes_nothing_at_information(
        self, caplog, monkeypatch
    ):
        FakeOBS(state=broadcaster.STATE_LIVE).install(monkeypatch)
        instance = Broadcaster(make_settings())
        instance.update(at(0), True)
        caplog.clear()

        for second in range(3, 60, 3):
            instance.update(at(second), True)

        assert lines(caplog, logging.INFO) == []
        assert lines(caplog, logging.WARNING) == []
        assert lines(caplog, logging.ERROR) == []


# ---------------------------------------------------------------------------
# update: a dropped stream and a refused start
# ---------------------------------------------------------------------------

class TestDropAndRefusal:
    def test_a_dropped_stream_is_one_warning_then_one_start(
        self, caplog, monkeypatch
    ):
        fake = FakeOBS(state=broadcaster.STATE_LIVE).install(monkeypatch)
        instance = Broadcaster(make_settings())
        instance.update(at(0), True)
        caplog.clear()

        fake.state = broadcaster.STATE_IDLE
        instance.update(at(3), True)

        assert "Broadcast dropped; starting it again." in lines(caplog, logging.WARNING)
        assert fake.starts == 1
        assert instance.is_live() is True

    def test_a_refused_start_is_one_warning_and_repeats_are_silent(
        self, caplog, monkeypatch
    ):
        fake = FakeOBS(state=broadcaster.STATE_IDLE).install(monkeypatch)
        fake.start_error = OBSUnavailable("stream key rejected")
        instance = Broadcaster(make_settings())

        instance.update(at(0), True)
        refused = [
            line for line in lines(caplog, logging.WARNING)
            if "did not start" in line
        ]
        assert len(refused) == 1
        assert "stream key rejected" in refused[0]
        assert instance.is_live() is False

        caplog.clear()
        instance.update(at(3), True)
        assert [
            line for line in lines(caplog, logging.WARNING)
            if "did not start" in line
        ] == []

    def test_a_later_success_is_one_information_line(self, caplog, monkeypatch):
        fake = FakeOBS(state=broadcaster.STATE_IDLE).install(monkeypatch)
        fake.start_error = OBSUnavailable("stream key rejected")
        instance = Broadcaster(make_settings())

        instance.update(at(0), True)
        fake.start_error = None
        caplog.clear()
        instance.update(at(3), True)

        assert lines(caplog, logging.INFO) == ["Broadcast started."]
        assert lines(caplog, logging.WARNING) == []
        assert instance.is_live() is True

    def test_a_start_that_obs_does_not_confirm_is_a_warning(
        self, caplog, monkeypatch
    ):
        fake = FakeOBS(state=broadcaster.STATE_IDLE).install(monkeypatch)
        fake.state_after_start = broadcaster.STATE_IDLE
        instance = Broadcaster(make_settings())

        instance.update(at(0), True)

        assert any(
            "still reports the stream as idle" in line
            for line in lines(caplog, logging.WARNING)
        )
        assert instance.is_live() is False

    def test_an_unconfirmed_start_is_retried_at_the_next_check(
        self, caplog, monkeypatch
    ):
        fake = FakeOBS(state=broadcaster.STATE_IDLE).install(monkeypatch)
        fake.state_after_start = broadcaster.STATE_IDLE
        instance = Broadcaster(make_settings())

        instance.update(at(0), True)
        instance.update(at(3), True)

        assert fake.starts == 2


# ---------------------------------------------------------------------------
# update: silence and the Restart Budget
# ---------------------------------------------------------------------------

def silent_obs(monkeypatch):
    """Install a running OBS that answers nothing, and a fresh broadcaster."""
    fake = FakeOBS(state=broadcaster.STATE_IDLE, running=True).install(monkeypatch)
    fake.read_error = OBSUnavailable("connection refused")
    return fake, Broadcaster(make_settings())


class TestSilence:
    def test_the_first_failure_is_one_warning(self, caplog, monkeypatch):
        fake, instance = silent_obs(monkeypatch)
        instance.update(at(0), True)

        warnings = [
            line for line in lines(caplog, logging.WARNING) if "not answering" in line
        ]
        assert len(warnings) == 1
        assert "connection refused" in warnings[0]
        assert fake.quits == 0
        assert fake.launches == 0

    def test_a_failure_inside_the_window_stays_at_debug(self, caplog, monkeypatch):
        _, instance = silent_obs(monkeypatch)
        instance.update(at(0), True)
        caplog.clear()

        instance.update(at(119), True)

        assert lines(caplog, logging.WARNING) == []
        assert lines(caplog, logging.INFO) == []

    def test_at_silence_seconds_obs_is_reopened_once(self, caplog, monkeypatch):
        fake, instance = silent_obs(monkeypatch)
        instance.update(at(0), True)
        instance.update(at(120), True)

        assert fake.quits == 1
        assert fake.launches == 1
        assert instance._restarts == 1
        assert any(
            "reopened it (attempt 1 of 3)" in line
            for line in lines(caplog, logging.WARNING)
        )

    def test_the_new_obs_gets_a_fresh_clock(self, caplog, monkeypatch):
        fake, instance = silent_obs(monkeypatch)
        instance.update(at(0), True)
        instance.update(at(120), True)
        caplog.clear()

        instance.update(at(239), True)

        assert fake.quits == 1
        assert lines(caplog, logging.WARNING) == []

    def test_three_reopenings_then_one_error_and_no_more_kills(
        self, caplog, monkeypatch
    ):
        fake, instance = silent_obs(monkeypatch)

        for moment in (0, 120, 240, 360, 480, 600, 720, 840):
            instance.update(at(moment), True)

        assert fake.quits == 3
        assert fake.launches == 3
        assert instance._restarts == 3

        errors = [line for line in lines(caplog, logging.ERROR) if "used up" in line]
        assert len(errors) == 1

        # Later checks keep asking but never kill again, and say nothing more.
        caplog.clear()
        instance.update(at(960), True)
        instance.update(at(1080), True)
        assert fake.quits == 3
        assert lines(caplog, logging.ERROR) == []

    def test_a_successful_read_clears_the_silence_clock(self, caplog, monkeypatch):
        fake, instance = silent_obs(monkeypatch)
        instance.update(at(0), True)

        fake.read_error = None
        fake.state = broadcaster.STATE_LIVE
        instance.update(at(60), True)
        assert instance._silent_since is None

        fake.read_error = OBSUnavailable("connection refused")
        instance.update(at(61), True)
        instance.update(at(120), True)

        assert fake.quits == 0

    def test_a_new_period_resets_the_budget(self, caplog, monkeypatch):
        fake, instance = silent_obs(monkeypatch)
        for moment in (0, 120, 240, 360, 480, 600):
            instance.update(at(moment), True)
        assert instance._restarts == 3

        instance.update(at(610), False)
        instance.update(at(620), True)
        assert instance._restarts == 0

        instance.update(at(740), True)
        assert fake.quits == 4


class TestBudgetExhaustion:
    def test_a_closed_obs_is_opened_three_times_then_left_closed(
        self, caplog, monkeypatch
    ):
        fake = FakeOBS(state=broadcaster.STATE_IDLE, running=False, launch_result=False)
        fake.install(monkeypatch)
        instance = Broadcaster(make_settings())

        for moment in (0, 3, 6, 9, 12):
            instance.update(at(moment), True)

        assert fake.launches == 3
        errors = [
            line for line in lines(caplog, logging.ERROR) if "keeps failing" in line
        ]
        assert len(errors) == 1

        caplog.clear()
        instance.update(at(15), True)
        assert fake.launches == 3
        assert lines(caplog, logging.ERROR) == []

    def test_stream_starts_are_still_tried_after_the_budget_is_gone(
        self, caplog, monkeypatch
    ):
        fake, instance = silent_obs(monkeypatch)
        for moment in (0, 120, 240, 360, 480, 600):
            instance.update(at(moment), True)
        assert instance._restarts == 3

        fake.read_error = None
        fake.state = broadcaster.STATE_IDLE
        fake.state_after_start = broadcaster.STATE_IDLE
        caplog.clear()

        instance.update(at(700), True)
        instance.update(at(703), True)

        assert fake.starts == 2

    def test_a_healed_obs_resumes_by_itself(self, caplog, monkeypatch):
        fake, instance = silent_obs(monkeypatch)
        for moment in (0, 120, 240, 360, 480, 600):
            instance.update(at(moment), True)
        caplog.clear()

        fake.read_error = None
        fake.state = broadcaster.STATE_LIVE
        instance.update(at(700), True)

        assert instance.is_live() is True
        assert lines(caplog, logging.INFO) == ["Broadcast is live."]


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

class TestStop:
    def test_a_live_stream_is_stopped_once(self, caplog, monkeypatch):
        fake = FakeOBS(state=broadcaster.STATE_LIVE).install(monkeypatch)
        instance = Broadcaster(make_settings())
        instance.update(at(0), True)
        caplog.clear()

        instance.stop()

        assert fake.stops == 1
        assert instance.is_live() is False
        assert "the stream is stopped" in caplog.text

    def test_a_second_stop_does_nothing(self, caplog, monkeypatch):
        fake = FakeOBS(state=broadcaster.STATE_LIVE).install(monkeypatch)
        instance = Broadcaster(make_settings())
        instance.update(at(0), True)
        caplog.clear()

        instance.stop()
        instance.stop()

        assert fake.stops == 1
        stopped = [line for line in lines(caplog, logging.INFO) if "the stream is stopped" in line]
        assert len(stopped) == 1

    def test_an_idle_obs_is_not_stopped(self, monkeypatch):
        fake = FakeOBS(state=broadcaster.STATE_IDLE).install(monkeypatch)
        Broadcaster(make_settings()).stop()
        assert fake.stops == 0

    def test_the_switch_being_off_never_touches_obs(self, caplog, monkeypatch):
        fake = FakeOBS(state=broadcaster.STATE_LIVE).install(monkeypatch)
        instance = Broadcaster(make_settings(enabled=False))
        instance._live = True

        instance.stop()

        assert fake.reads == 0
        assert fake.stops == 0
        assert fake.starts == 0
        assert fake.launches == 0
        assert fake.quits == 0
        assert instance.is_live() is False
        assert lines(caplog, logging.INFO) == []
        assert lines(caplog, logging.WARNING) == []
        assert lines(caplog, logging.ERROR) == []

    def test_five_stops_with_an_idle_obs_write_nothing(self, caplog, monkeypatch):
        fake = FakeOBS(state=broadcaster.STATE_IDLE).install(monkeypatch)
        instance = Broadcaster(make_settings())

        for _ in range(5):
            instance.stop()

        assert fake.reads == 1
        assert fake.stops == 0
        assert lines(caplog, logging.INFO) == []
        assert lines(caplog, logging.WARNING) == []
        assert lines(caplog, logging.ERROR) == []

    def test_five_stops_with_an_unreachable_obs_warn_once(
        self, caplog, monkeypatch
    ):
        fake = FakeOBS(state=broadcaster.STATE_IDLE).install(monkeypatch)
        fake.read_error = OBSUnavailable("connection refused")
        instance = Broadcaster(make_settings())

        for _ in range(5):
            instance.stop()

        warnings = [
            line for line in lines(caplog, logging.WARNING)
            if "Could not reach OBS" in line
        ]
        assert len(warnings) == 1
        assert fake.reads == 1

    def test_a_new_wanted_check_resets_the_stop_episode(self, caplog, monkeypatch):
        fake = FakeOBS(state=broadcaster.STATE_LIVE).install(monkeypatch)
        instance = Broadcaster(make_settings())

        instance.stop()
        assert fake.stops == 1

        instance.update(at(0), True)
        assert instance.is_live() is True

        instance.stop()
        assert fake.stops == 2

    def test_a_missing_startup_program_is_never_looked_for(
        self, caplog, monkeypatch, no_program
    ):
        fake = FakeOBS(state=broadcaster.STATE_LIVE).install(monkeypatch)
        instance = Broadcaster(make_settings())
        instance.prepare()
        caplog.clear()

        instance.stop()
        instance.update(at(0), False)

        assert fake.reads == 0
        assert fake.stops == 0
        assert lines(caplog, logging.INFO) == []
        assert lines(caplog, logging.WARNING) == []

    def test_an_unreachable_obs_is_one_warning_and_no_raise(
        self, caplog, monkeypatch
    ):
        fake = FakeOBS(state=broadcaster.STATE_LIVE).install(monkeypatch)
        instance = Broadcaster(make_settings())
        instance.update(at(0), True)

        fake.read_error = OBSUnavailable("connection refused")
        caplog.clear()
        instance.stop()

        warnings = [line for line in lines(caplog, logging.WARNING) if "Could not reach OBS" in line]
        assert len(warnings) == 1
        assert instance.is_live() is False

    def test_a_stop_that_obs_refuses_is_one_warning(self, caplog, monkeypatch):
        fake = FakeOBS(state=broadcaster.STATE_LIVE).install(monkeypatch)
        instance = Broadcaster(make_settings())
        instance.update(at(0), True)

        fake.stop_error = OBSUnavailable("output in transition")
        caplog.clear()
        instance.stop()

        assert any(
            "Could not stop the stream" in line
            for line in lines(caplog, logging.WARNING)
        )

    def test_stop_forgets_the_state_so_a_later_check_starts_clean(
        self, caplog, monkeypatch
    ):
        fake = FakeOBS(state=broadcaster.STATE_LIVE).install(monkeypatch)
        instance = Broadcaster(make_settings())
        instance.update(at(0), True)
        instance.stop()
        caplog.clear()

        fake.state = broadcaster.STATE_LIVE
        instance.update(at(10), True)

        assert instance.is_live() is True
        assert lines(caplog, logging.INFO) == ["Broadcast is live."]


# ---------------------------------------------------------------------------
# Never raise
# ---------------------------------------------------------------------------

class TestNeverRaises:
    def test_every_failure_leaves_prepare_update_and_stop_normal(
        self, caplog, monkeypatch, program_there
    ):
        def boom(*args, **kwargs):
            raise RuntimeError("everything is broken")

        for name in (
            "read_stream_state",
            "start_stream",
            "stop_stream",
            "obs_running",
            "launch_obs",
            "quit_obs",
        ):
            monkeypatch.setattr(broadcaster, name, boom)

        instance = Broadcaster(make_settings())
        assert instance.prepare() is None
        assert instance.update(at(0), True) is None
        assert instance.update(at(120), True) is None
        assert instance.stop() is None
        assert instance.is_live() is False

        assert lines(caplog, logging.ERROR) != []

    def test_a_raising_obs_running_is_swallowed(
        self, caplog, monkeypatch, program_there
    ):
        def boom():
            raise RuntimeError("no process list")

        monkeypatch.setattr(broadcaster, "obs_running", boom)
        instance = Broadcaster(make_settings())
        instance.prepare()
        instance.update(at(0), True)
        assert lines(caplog, logging.ERROR) != []


# ---------------------------------------------------------------------------
# Nothing real is ever touched
# ---------------------------------------------------------------------------

class TestNothingRealIsTouched:
    def test_a_whole_run_starts_no_process(self, monkeypatch, program_there):
        def no_process_allowed(*args, **kwargs):
            raise AssertionError("no process may be started by a test")

        monkeypatch.setattr(broadcaster.subprocess, "Popen", no_process_allowed)
        fake = FakeOBS(state=broadcaster.STATE_IDLE, running=True).install(monkeypatch)

        instance = Broadcaster(make_settings())
        instance.prepare()
        instance.update(at(0), True)
        instance.update(at(3), True)
        instance.stop()

        assert fake.launches == 0
        assert fake.starts == 1
        assert fake.stops == 1
