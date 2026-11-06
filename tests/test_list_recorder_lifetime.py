"""Tests for the List Recorder's lifetime inside the automator.

While the List Recorder Switch is on the automator looks for a leftover copy at
startup (and adopts it), starts one on the first cycle when none is running,
restarts it forever after a death (ten seconds later, warning only on the first
death of the run), and stops it on the way out: the console's own stop signal,
ten seconds, terminate, kill.

With the switch off nothing is scanned, adopted, started, restarted, or
stopped, and a rehearsal does none of it either.

Nothing here starts a real child, scans the real process table, or touches a
real Telegram: the supervisor takes its launch, its stop signal, its clock, and
its sleep as seams, and the module-level pieces are patched.
"""
import datetime
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import main
from schedule import parse_schedule

_MONDAY = datetime.datetime(2024, 1, 1)


def at(hour: int, minute: int = 0) -> datetime.datetime:
    return _MONDAY + datetime.timedelta(hours=hour, minutes=minute)


_OPEN = parse_schedule('{"all": ["09:00-17:00"]}')


ADOPTED = "A List Recorder was already running; watching it instead of starting a second one."
STARTED = "List Recorder started."
RESTARTED = "List Recorder restarted."
DEATH = "List Recorder is no longer running; restarting it in 10 seconds."
STOPPED = "List Recorder stopped; every open Clip is closed."
NOT_STOPPED = (
    "List Recorder did not stop within 10 seconds; terminating it — open Clips "
    "may be cut."
)
STILL_RUNNING = "List Recorder still running; killing it."
KILLED = "The List Recorder was killed; its recorders ended with it."
KILLED_UNPROTECTED = (
    "The List Recorder was killed; any List Recordings it owned may still be "
    "running."
)


@pytest.fixture(autouse=True)
def clean_state():
    """Forget the one supervisor and the refusal memory between tests."""
    main._list_recorder = None
    main._telegram_program_missing = False
    yield
    main._list_recorder = None
    main._telegram_program_missing = False


# ---------------------------------------------------------------------------
# Fakes and seams
# ---------------------------------------------------------------------------

class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeChild:
    """A launched helper: alive until the test (or the test's ask) ends it."""

    def __init__(self, pid=4242, dies_on_terminate=True, dies_on_kill=True):
        self.pid = pid
        self.alive = True
        self.dies_on_terminate = dies_on_terminate
        self.dies_on_kill = dies_on_kill
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.terminated += 1
        if self.dies_on_terminate:
            self.alive = False

    def kill(self):
        self.killed += 1
        if self.dies_on_kill:
            self.alive = False


class FakeAdopted:
    """An outside List Recorder taken over at startup."""

    def __init__(self, pid=99):
        self.pid = pid
        self.alive = True

    def is_running(self):
        return self.alive


def _supervisor(*, child=None, spawn=None, ask=None):
    """Return ``(supervisor, clock, spawn, ask)`` with hand-driven seams.

    The first launch returns *child* when one is given; every later launch
    returns a fresh, alive :class:`FakeChild`, recorded in ``spawn.children``.
    """
    clock = FakeClock()
    children = []

    def make(*args, **kwargs):
        if not children and child is not None:
            children.append(child)
            return child
        made = FakeChild()
        children.append(made)
        return made

    if spawn is None:
        spawn = MagicMock(side_effect=make)
    spawn.children = children

    if ask is None:
        ask = MagicMock(return_value=True)
    supervisor = main.ListRecorderSupervisor(
        program_path=r"C:\App\record_streams.py",
        interpreter=r"C:\Python\python.exe",
        spawn=spawn,
        ask=ask,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    return supervisor, clock, spawn, ask


def _messages(caplog):
    return [record.getMessage() for record in caplog.records if record.name == "main"]


# ---------------------------------------------------------------------------
# Starting and adopting
# ---------------------------------------------------------------------------

class TestStart:
    """One helper, started by the automator, in its own group and quiet."""

    def test_a_first_launch_says_started(self, caplog):
        supervisor, clock, spawn, ask = _supervisor()
        with caplog.at_level(logging.INFO):
            assert supervisor.ensure_running() is True

        assert spawn.call_count == 1
        assert _messages(caplog) == [STARTED]
        assert supervisor.is_running() is True

    def test_nothing_is_written_while_it_stays_up(self, caplog):
        supervisor, clock, spawn, ask = _supervisor()
        with caplog.at_level(logging.DEBUG):
            supervisor.ensure_running()
            caplog.clear()
            for _ in range(3):
                supervisor.ensure_running()

        assert spawn.call_count == 1
        assert _messages(caplog) == []

    def test_the_launch_is_the_interpreter_plus_the_program(self):
        with patch("main.subprocess.Popen") as popen:
            main.launch_list_recorder_process(
                program_path=r"C:\App\record_streams.py",
                interpreter=r"C:\Python\python.exe",
            )

        command = popen.call_args.args[0]
        assert command == [r"C:\Python\python.exe", r"C:\App\record_streams.py"]

    def test_the_launch_has_its_own_group_and_no_output(self):
        with patch("main.subprocess.Popen") as popen:
            main.launch_list_recorder_process(
                program_path=r"C:\App\record_streams.py",
                interpreter=r"C:\Python\python.exe",
            )

        flags = popen.call_args.kwargs["creationflags"]
        assert flags & main.subprocess.CREATE_NEW_PROCESS_GROUP
        # A new console would hide it from the stop signal sent on the way out.
        assert not flags & main.subprocess.CREATE_NO_WINDOW
        assert popen.call_args.kwargs["stdout"] is main.subprocess.DEVNULL
        assert popen.call_args.kwargs["stderr"] is main.subprocess.DEVNULL

    def test_the_program_lives_next_to_the_automator(self):
        assert main.list_recorder_program_path() == str(
            Path(main.__file__).parent / "record_streams.py"
        )

    def test_an_adopted_copy_is_watched_not_doubled(self, caplog):
        adopted = FakeAdopted()
        supervisor, clock, spawn, ask = _supervisor()
        with caplog.at_level(logging.INFO):
            supervisor.adopt(adopted)
            assert supervisor.ensure_running() is True

        assert _messages(caplog) == [ADOPTED]
        spawn.assert_not_called()

    def test_a_leftover_is_found_by_its_file_name_in_the_command_line(self):
        class Proc:
            def __init__(self, cmdline):
                self.info = {"name": "python.exe", "cmdline": cmdline}

        wanted = Proc([r"C:\Python\python.exe", r"C:\App\record_streams.py"])
        other = Proc([r"C:\Python\python.exe", r"C:\App\main.py"])

        with patch("main.psutil.process_iter", return_value=[other, wanted]):
            found = main.find_list_recorder_process()

        assert found is wanted

    def test_no_leftover_means_none_found(self):
        class Proc:
            info = {"name": "python.exe", "cmdline": [r"C:\App\main.py"]}

        with patch("main.psutil.process_iter", return_value=[Proc()]):
            assert main.find_list_recorder_process() is None


# ---------------------------------------------------------------------------
# Death, and the fixed ten-second restart
# ---------------------------------------------------------------------------

class TestDeathAndRestart:
    """A death is noticed from the handle, warned once, and restarted forever."""

    def test_the_death_warns_and_waits_ten_seconds(self, caplog):
        child = FakeChild()
        supervisor, clock, spawn, ask = _supervisor(child=child)
        with caplog.at_level(logging.INFO):
            supervisor.ensure_running()
            caplog.clear()

            child.alive = False
            assert supervisor.ensure_running() is False
            assert _messages(caplog) == [DEATH]

            clock.now = 9.0
            assert supervisor.ensure_running() is False
            assert spawn.call_count == 1

            clock.now = 10.1
            assert supervisor.ensure_running() is True

    def test_the_restart_says_restarted(self, caplog):
        child = FakeChild()
        supervisor, clock, spawn, ask = _supervisor(child=child)
        supervisor.ensure_running()
        caplog.clear()

        child.alive = False
        with caplog.at_level(logging.INFO):
            supervisor.ensure_running()
            clock.now = 10.0
            supervisor.ensure_running()

        assert _messages(caplog) == [DEATH, RESTARTED]

    def test_only_the_first_death_warns_at_full_volume(self, caplog):
        supervisor, clock, spawn, ask = _supervisor()
        supervisor.ensure_running()
        clock.now = 100.0

        with caplog.at_level(logging.DEBUG):
            for _ in range(2):
                spawn.children[-1].alive = False
                supervisor.ensure_running()
                clock.now += 10.0
                supervisor.ensure_running()

        death_records = [r for r in caplog.records if r.getMessage() == DEATH]
        assert [r.levelno for r in death_records] == [logging.WARNING, logging.DEBUG]

    def test_an_adopted_copy_that_dies_is_restarted_by_the_automator(self, caplog):
        adopted = FakeAdopted()
        supervisor, clock, spawn, ask = _supervisor()
        with caplog.at_level(logging.INFO):
            supervisor.adopt(adopted)
            supervisor.ensure_running()
            adopted.alive = False

            supervisor.ensure_running()
            clock.now = 10.0
            supervisor.ensure_running()

        assert _messages(caplog) == [ADOPTED, DEATH, RESTARTED]

    def test_a_failed_launch_is_a_death_and_never_raises(self, caplog):
        spawn = MagicMock(side_effect=[OSError("no interpreter"), FakeChild()])
        supervisor, clock, spawn, ask = _supervisor(spawn=spawn)

        with caplog.at_level(logging.INFO):
            assert supervisor.ensure_running() is False
            assert _messages(caplog) == [DEATH]

            clock.now = 9.5
            assert supervisor.ensure_running() is False

            clock.now = 10.0
            assert supervisor.ensure_running() is True

        assert _messages(caplog) == [DEATH, STARTED]

    def test_a_death_while_the_wait_is_pending_is_not_noticed_twice(self, caplog):
        child = FakeChild()
        supervisor, clock, spawn, ask = _supervisor(child=child)
        supervisor.ensure_running()
        child.alive = False
        caplog.clear()

        with caplog.at_level(logging.DEBUG):
            supervisor.ensure_running()
            supervisor.ensure_running()
            supervisor.ensure_running()

        death_lines = [r for r in caplog.records if r.getMessage() == DEATH]
        assert len(death_lines) == 1


# ---------------------------------------------------------------------------
# The way out
# ---------------------------------------------------------------------------

class TestStop:
    """The console's own stop signal, ten seconds, terminate, kill."""

    def test_a_clean_stop_says_every_clip_is_closed(self, caplog):
        child = FakeChild()
        supervisor, clock, spawn, ask = _supervisor(child=child)

        def ask_and_leave(pid):
            child.alive = False
            return True

        ask.side_effect = ask_and_leave
        supervisor.ensure_running()
        caplog.clear()

        with caplog.at_level(logging.INFO):
            supervisor.stop()

        assert _messages(caplog) == [STOPPED]
        assert ask.call_args == call(child.pid)

    def test_a_slow_helper_is_terminated_after_ten_seconds(self, caplog):
        child = FakeChild()
        supervisor, clock, spawn, ask = _supervisor(child=child)
        supervisor.ensure_running()
        caplog.clear()

        with caplog.at_level(logging.INFO):
            supervisor.stop()

        assert _messages(caplog) == [NOT_STOPPED]
        assert child.terminated == 1
        assert clock.now >= 10.0

    def test_a_lingering_helper_is_killed_and_says_so(self, caplog):
        child = FakeChild(dies_on_terminate=False)
        supervisor, clock, spawn, ask = _supervisor(child=child)
        supervisor.ensure_running()
        caplog.clear()

        with patch.object(main, "job_object_protection_available",
                          MagicMock(return_value=True)):
            with caplog.at_level(logging.INFO):
                supervisor.stop()

        assert _messages(caplog) == [NOT_STOPPED, STILL_RUNNING, KILLED]
        assert child.killed == 1

    def test_a_kill_without_the_job_object_says_the_unsure_line(self, caplog):
        child = FakeChild(dies_on_terminate=False)
        supervisor, clock, spawn, ask = _supervisor(child=child)
        supervisor.ensure_running()
        caplog.clear()

        with patch.object(main, "job_object_protection_available",
                          MagicMock(return_value=False)):
            with caplog.at_level(logging.INFO):
                supervisor.stop()

        assert _messages(caplog) == [NOT_STOPPED, STILL_RUNNING, KILLED_UNPROTECTED]

    def test_an_undeliverable_signal_goes_straight_to_terminate(self, caplog):
        child = FakeChild()
        supervisor, clock, spawn, ask = _supervisor(child=child, ask=MagicMock(return_value=False))
        supervisor.ensure_running()
        caplog.clear()

        with caplog.at_level(logging.INFO):
            supervisor.stop()

        assert _messages(caplog) == [NOT_STOPPED]
        assert child.terminated == 1
        assert clock.now == 0.0

    def test_a_stopped_helper_is_never_read_as_a_death(self, caplog):
        child = FakeChild()
        supervisor, clock, spawn, ask = _supervisor(child=child)

        def ask_and_leave(pid):
            child.alive = False
            return True

        ask.side_effect = ask_and_leave
        supervisor.ensure_running()
        supervisor.stop()
        caplog.clear()

        with caplog.at_level(logging.DEBUG):
            assert supervisor.ensure_running() is False

        assert _messages(caplog) == []
        assert spawn.call_count == 1

    def test_stopping_twice_says_nothing_more(self, caplog):
        child = FakeChild()
        supervisor, clock, spawn, ask = _supervisor(child=child)

        def ask_and_leave(pid):
            child.alive = False
            return True

        ask.side_effect = ask_and_leave
        supervisor.ensure_running()
        with caplog.at_level(logging.INFO):
            supervisor.stop()
            caplog.clear()
            supervisor.stop()

        assert _messages(caplog) == []

    def test_the_killed_line_follows_the_job_object_when_it_is_there(self):
        with patch("recorder.job_object_available", MagicMock(return_value=True), create=True):
            assert main.job_object_protection_available() is True
        with patch.object(main.recorder, "job_object_available",
                          MagicMock(return_value=False), create=True):
            assert main.job_object_protection_available() is False

    def test_the_engine_without_the_check_means_no_protection(self):
        with patch.object(main, "recorder", MagicMock(spec=[])):
            assert main.job_object_protection_available() is False


# ---------------------------------------------------------------------------
# The switch: on, off, and the rehearsal
# ---------------------------------------------------------------------------

def _fake_config(record_list_enabled=True):
    cfg = MagicMock()
    cfg.record_list_enabled = record_list_enabled
    cfg.recordings_dir = r"D:\Recordings"
    cfg.telegram_path = r"D:\Apps\Telegram.exe"
    cfg.recording_mode = "off"
    cfg.scale_factor = 1.0
    cfg.refresh_scale_factor = MagicMock(return_value=1.0)
    cfg.screen_resolution = (1920, 1080)
    cfg.screen_rotation = 0
    cfg.second_window_title = "TargetWindow"
    cfg.second_window_retry_limit = 1
    cfg.click_retry_interval = 0.0
    cfg.kill_cooldown_seconds = 0
    return cfg


class TestTheSwitch:
    """On at startup: adopt a leftover, otherwise start nothing."""

    def test_a_leftover_is_adopted_and_watched(self, caplog):
        leftover = FakeAdopted()
        cfg = _fake_config(True)
        with patch.object(main, "config", cfg), \
             patch("main.find_list_recorder_process", return_value=leftover), \
             patch("main.launch_list_recorder_process") as spawn:
            with caplog.at_level(logging.INFO):
                main.prepare_list_recorder()
                main.keep_list_recorder_alive()

        assert _messages(caplog) == [ADOPTED]
        assert main._list_recorder.is_running() is True
        spawn.assert_not_called()

    def test_nothing_found_started_nothing(self, caplog):
        cfg = _fake_config(True)
        with patch.object(main, "config", cfg), \
             patch("main.find_list_recorder_process", return_value=None), \
             patch("main.launch_list_recorder_process") as spawn:
            with caplog.at_level(logging.INFO):
                main.prepare_list_recorder()

        assert _messages(caplog) == []
        spawn.assert_not_called()
        assert main._list_recorder is not None

    def test_the_first_cycle_starts_the_helper(self, caplog):
        cfg = _fake_config(True)
        with patch.object(main, "config", cfg), \
             patch("main.find_list_recorder_process", return_value=None), \
             patch("main.launch_list_recorder_process",
                   MagicMock(return_value=FakeChild())):
            main.prepare_list_recorder()
            caplog.clear()
            with caplog.at_level(logging.INFO):
                main.keep_list_recorder_alive()

        assert _messages(caplog) == [STARTED]

    def test_a_scan_fault_never_raises(self):
        cfg = _fake_config(True)
        with patch.object(main, "config", cfg), \
             patch("main.find_list_recorder_process",
                   MagicMock(side_effect=RuntimeError("no process table"))):
            main.prepare_list_recorder()  # must not raise

        assert main._list_recorder is not None

    def test_the_switch_off_touches_nothing(self, caplog):
        cfg = _fake_config(False)
        with patch.object(main, "config", cfg), \
             patch("main.find_list_recorder_process") as find, \
             patch("main.launch_list_recorder_process") as spawn:
            with caplog.at_level(logging.DEBUG):
                main.prepare_list_recorder()
                main.keep_list_recorder_alive()
                main.stop_list_recorder()

        assert main._list_recorder is None
        find.assert_not_called()
        spawn.assert_not_called()
        assert _messages(caplog) == []

    def test_a_rehearsal_touches_nothing_and_closes_nothing(self, caplog):
        cfg = _fake_config(True)
        with patch.object(main, "config", cfg), \
             patch("main.find_list_recorder_process") as find, \
             patch("main.launch_list_recorder_process") as spawn:
            with caplog.at_level(logging.DEBUG):
                main.prepare_list_recorder(dry_run=True)
                main.keep_list_recorder_alive()
                main.stop_list_recorder()

        assert main._list_recorder is None
        find.assert_not_called()
        spawn.assert_not_called()
        assert _messages(caplog) == []

    def test_preparing_twice_scans_once(self):
        cfg = _fake_config(True)
        with patch.object(main, "config", cfg), \
             patch("main.find_list_recorder_process", return_value=None) as find:
            main.prepare_list_recorder()
            main.prepare_list_recorder()

        assert find.call_count == 1

    def test_a_fault_while_keeping_alive_never_raises(self):
        broken = MagicMock()
        broken.ensure_running.side_effect = RuntimeError("boom")
        main._list_recorder = broken

        main.keep_list_recorder_alive()  # must not raise

    def test_a_fault_while_stopping_never_raises(self):
        broken = MagicMock()
        broken.stop.side_effect = RuntimeError("boom")
        main._list_recorder = broken

        main.stop_list_recorder()  # must not raise


# ---------------------------------------------------------------------------
# Where the helper sits in the cycle and on the way out
# ---------------------------------------------------------------------------

def _cycle_mocks(config=None, **overrides):
    cfg = config or _fake_config(False)
    values = dict(
        config=cfg,
        is_telegram_running=MagicMock(return_value=True),
        kill_telegram=MagicMock(),
        launch_telegram=MagicMock(return_value=True),
        perform_telegram_actions=MagicMock(return_value=True),
        setup_second_window=MagicMock(return_value=True),
        perform_click_sequence=MagicMock(),
        gw=MagicMock(),
        ensure_correct_screen=MagicMock(),
        update_broadcast=MagicMock(),
        update_recording=MagicMock(),
        keep_list_recorder_alive=MagicMock(),
        telegram_program_present=MagicMock(return_value=True),
        _last_scale_factor=1.0,
    )
    values.update(overrides)
    return values, patch.multiple("main", **values)


class TestPlacement:
    """At the very top of the cycle, before the recording update and the gate."""

    def test_the_helper_is_kept_alive_before_the_recording_and_the_gate(self):
        order = []
        mocks, ctx = _cycle_mocks(
            keep_list_recorder_alive=MagicMock(side_effect=lambda: order.append("helper")),
            update_recording=MagicMock(side_effect=lambda *a: order.append("recording")),
        )
        with ctx:
            main.run_monitor_cycle(_OPEN, moment=at(20))  # outside the period

        assert order == ["helper", "recording"]

    def test_suspend_still_keeps_the_helper_alive(self):
        mocks, ctx = _cycle_mocks()
        with ctx:
            main.run_monitor_cycle(_OPEN, moment=at(20))

        mocks["keep_list_recorder_alive"].assert_called_once()

    def test_the_way_out_stops_the_helper_then_the_recording_then_the_stream(self):
        order = []
        mocks, ctx = _cycle_mocks(
            run_monitor_cycle=MagicMock(side_effect=KeyboardInterrupt),
            stop_list_recorder=MagicMock(side_effect=lambda: order.append("helper")),
            stop_recording=MagicMock(side_effect=lambda: order.append("recording")),
            stop_broadcast=MagicMock(side_effect=lambda: order.append("broadcast")),
        )
        with ctx:
            main.main()

        assert order == ["helper", "recording", "broadcast"]
