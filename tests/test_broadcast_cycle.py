"""Tests for broadcasting inside the automator.

Broadcasting is driven from two places in the monitor cycle and two in the
entry point: ``prepare_broadcast`` once at startup, ``update_broadcast`` right
after the screen fix, ``stop_broadcast`` in the schedule gate's
outside-the-period branch and on Ctrl+C. The read-only ``--check-obs`` path
reports on the machine and leaves without doing anything else.

Every test fakes the broadcaster, the OBS check report, and the window /
process / screen / rotation boundaries, so no OBS is opened, no process is
started, no socket is opened, and no real OBS file is read.
"""
import datetime
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import main
from schedule import parse_schedule


# 2024-01-01 is a Monday.
_MONDAY = datetime.datetime(2024, 1, 1)

# A password that would be obvious if it ever reached the log.
_PASSWORD = "correct-horse-battery-staple"


def at(hour: int, minute: int = 0, second: int = 0) -> datetime.datetime:
    """A fixed Monday moment (local clock) used to drive the cycle."""
    return _MONDAY + datetime.timedelta(hours=hour, minutes=minute, seconds=second)


# Allowed 09:00-17:00 every day: 12:00 is inside, 20:00 is outside.
_OPEN = parse_schedule('{"all": ["09:00-17:00"]}')
# Allowed 09:00-10:00: 09:59:55 ends within the 7s launch window.
_NARROW = parse_schedule('{"all": ["09:00-10:00"]}')

# The report lines the module produces, faked here so no OBS is asked.
_OBS_LINES = [
    "OBS program: found at 'C:\\Program Files\\obs-studio\\bin\\64bit\\obs64.exe'.",
    "OBS password: set.",
    "OBS running: no.",
    "OBS stream: idle.",
]


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------

class FakeBroadcaster:
    """A broadcaster stand-in that records what the program asks of it.

    ``live`` imitates the engine: an update that wants a stream leaves one
    live, an update that does not want one leaves it stopped, so a rehearsal
    that finds a live stream ends with it stopped. ``events`` is an optional
    shared list, so a test can assert the order against the screen fake and the
    Telegram calls. ``raises`` makes every call blow up, to prove the
    never-raise guard.
    """

    def __init__(self, live=False, raises=False, events=None):
        self.live = live
        self.raises = raises
        self.events = [] if events is None else events
        self.prepares = []
        self.updates = []
        self.stops = 0

    def prepare(self, dry_run=False):
        self._touch("broadcast.prepare")
        self.prepares.append(dry_run)

    def update(self, moment, wanted):
        self._touch("broadcast.update")
        self.updates.append((moment, wanted))
        self.live = bool(wanted)

    def stop(self):
        self._touch("broadcast.stop")
        self.stops += 1
        self.live = False

    def is_live(self):
        return self.live

    def _touch(self, name):
        if self.raises:
            raise RuntimeError("OBS exploded on purpose")
        self.events.append(name)


def _make_config(
    broadcast_enabled=True,
    obs_host="127.0.0.1",
    obs_port=4455,
    obs_password=_PASSWORD,
    obs_path=r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
    recording_mode="off",
):
    """A Config-like MagicMock holding the broadcast and cycle values."""
    cfg = MagicMock()
    cfg.broadcast_enabled = broadcast_enabled
    cfg.broadcast_obs_host = obs_host
    cfg.broadcast_obs_port = obs_port
    cfg.broadcast_obs_password = obs_password
    cfg.broadcast_obs_path = obs_path
    # Values the rest of the cycle reads; concrete so no MagicMock comparison
    # or arithmetic surprise can happen behind the mocked boundaries.
    cfg.recording_mode = recording_mode
    cfg.record_stream_url = ""
    cfg.recordings_dir = "Recordings"
    cfg.record_clip_seconds = 7200
    cfg.record_reconnect_seconds = 5.0
    cfg.record_ffmpeg_path = "ffmpeg"
    cfg.record_disk_warn_gb = 10.0
    cfg.record_disk_stop_gb = 2.0
    cfg.scale_factor = 1.0
    cfg.refresh_scale_factor = MagicMock(return_value=1.0)
    cfg.screen_resolution = (1920, 1080)
    cfg.screen_rotation = 0
    cfg.second_window_title = "TargetWindow"
    cfg.second_window_retry_limit = 1
    cfg.click_retry_interval = 0.0
    cfg.kill_cooldown_seconds = 0
    return cfg


def _patched(*, config=None, broadcaster=None, installed=None, events=None,
             **overrides):
    """Return ``(mocks, patch_context)`` for every boundary the program reaches.

    One mapper covers the monitor cycle, the entry point, and the check path:
    the process, window, screen, rotation, recorder, and broadcaster
    boundaries are all replaced, so nothing on the machine is touched.
    ``installed`` is what the module-level ``_broadcaster`` already holds when
    the cycle starts; ``broadcaster`` is what the accessor returns.
    """
    if config is None:
        config = _make_config()
    if broadcaster is None:
        broadcaster = FakeBroadcaster(events=events)

    values = dict(
        config=config,
        # Broadcasting
        _broadcaster=installed,
        get_broadcaster=MagicMock(return_value=broadcaster),
        check_report=MagicMock(return_value=(list(_OBS_LINES), True)),
        # Recording
        _recorder=None,
        get_recorder=MagicMock(),
        _free_space_bytes=MagicMock(return_value=40 * 1024 ** 3),
        log_recording_startup=MagicMock(),
        # Windows and processes
        is_telegram_running=MagicMock(return_value=False),
        kill_telegram=MagicMock(),
        launch_telegram=MagicMock(return_value=True),
        perform_telegram_actions=MagicMock(return_value=True),
        setup_second_window=MagicMock(return_value=True),
        perform_click_sequence=MagicMock(),
        gw=MagicMock(),
        # The screen and the rotation
        ensure_correct_screen=MagicMock(),
        set_screen_size=MagicMock(),
        read_screen_size=MagicMock(return_value=(1920, 1080)),
        rotatescreen=MagicMock(),
        set_dpi_awareness=MagicMock(),
        # The entry point
        sys=MagicMock(argv=["main.py"]),
        validate_schedule_at_startup=MagicMock(return_value=_OPEN),
        load_live_schedule=MagicMock(return_value=_OPEN),
        run_dry_run=MagicMock(),
        time=MagicMock(),
    )
    values.update(overrides)
    return values, patch.multiple("main", **values)


def _clock(moment):
    """A stand-in for the ``datetime`` module whose ``now()`` is *moment*."""
    fake = MagicMock()
    fake.datetime.now.return_value = moment
    return fake


def _main_messages(caplog):
    """Return the messages the automator's own logger wrote, in order."""
    return [record.getMessage() for record in caplog.records if record.name == "main"]


# ---------------------------------------------------------------------------
# The additions: slot, accessor, settings builder, the three guarded functions
# ---------------------------------------------------------------------------

class TestAdditions:
    """The names fixed by the spec exist and behave in the recording's style."""

    def test_the_slot_lives_at_module_level(self):
        assert hasattr(main, "_broadcaster")
        with patch.multiple("main", _broadcaster=None):
            assert main._broadcaster is None

    def test_the_accessor_creates_once_and_reuses_it(self):
        made = MagicMock(return_value="the-broadcaster")
        with patch.multiple("main", _broadcaster=None, Broadcaster=made):
            first = main.get_broadcaster("settings")
            second = main.get_broadcaster("other-settings")

        assert first == second == "the-broadcaster"
        assert made.call_count == 1

    def test_the_settings_builder_carries_the_five_values(self):
        config = _make_config(
            broadcast_enabled=True,
            obs_host="10.0.0.5",
            obs_port=5000,
            obs_password="secret-value",
            obs_path=r"D:\OBS\obs64.exe",
        )
        with patch.object(main, "config", config):
            settings = main._build_broadcast_settings()

        assert settings.enabled is True
        assert settings.host == "10.0.0.5"
        assert settings.port == 5000
        assert settings.password == "secret-value"
        assert settings.obs_path == r"D:\OBS\obs64.exe"

    def test_the_builder_leaves_the_tuning_fields_at_their_defaults(self):
        with patch.object(main, "config", _make_config()):
            settings = main._build_broadcast_settings()

        assert settings.request_timeout_seconds == 3.0
        assert settings.silence_seconds == 120.0
        assert settings.restart_limit == 3
        assert settings.exit_wait_seconds == 5.0

    def test_prepare_passes_the_rehearsal_flag(self):
        fake = FakeBroadcaster()
        mocks, ctx = _patched(broadcaster=fake)
        with ctx:
            main.prepare_broadcast(True)

        assert fake.prepares == [True]

    def test_prepare_without_the_flag_is_a_real_start(self):
        fake = FakeBroadcaster()
        mocks, ctx = _patched(broadcaster=fake)
        with ctx:
            main.prepare_broadcast()

        assert fake.prepares == [False]

    def test_stop_does_nothing_without_a_broadcaster(self):
        mocks, ctx = _patched(installed=None)
        with ctx:
            main.stop_broadcast()  # must not raise, must not create one

        mocks["get_broadcaster"].assert_not_called()

    def test_stop_uses_the_installed_broadcaster(self):
        fake = FakeBroadcaster(live=True)
        mocks, ctx = _patched(installed=fake)
        with ctx:
            main.stop_broadcast()

        assert fake.stops == 1
        assert fake.live is False

    def test_prepare_never_raises(self):
        fake = FakeBroadcaster(raises=True)
        mocks, ctx = _patched(broadcaster=fake)
        with ctx:
            main.prepare_broadcast()  # must return normally

    def test_update_never_raises(self):
        fake = FakeBroadcaster(raises=True)
        mocks, ctx = _patched(broadcaster=fake)
        with ctx:
            main.update_broadcast(at(12), _OPEN)  # must return normally

    def test_stop_never_raises(self):
        fake = FakeBroadcaster(raises=True)
        mocks, ctx = _patched(installed=fake)
        with ctx:
            main.stop_broadcast()  # must return normally

    def test_a_raising_broadcaster_is_logged_not_swallowed_silently(self, caplog):
        fake = FakeBroadcaster(raises=True)
        mocks, ctx = _patched(broadcaster=fake)
        with ctx, caplog.at_level(logging.ERROR):
            main.update_broadcast(at(12), _OPEN)

        assert any("broadcasting" in message for message in _main_messages(caplog))


# ---------------------------------------------------------------------------
# The wanted rule
# ---------------------------------------------------------------------------

class TestWantedRule:
    """Wanted is the switch and the clock — nothing else."""

    def test_inside_the_period_wants_a_stream(self):
        fake = FakeBroadcaster()
        mocks, ctx = _patched(broadcaster=fake)
        with ctx:
            main.update_broadcast(at(12), _OPEN)

        assert fake.updates == [(at(12), True)]

    def test_outside_the_period_does_not_want_a_stream(self):
        fake = FakeBroadcaster()
        mocks, ctx = _patched(broadcaster=fake)
        with ctx:
            main.update_broadcast(at(20), _OPEN)

        assert fake.updates == [(at(20), False)]

    def test_no_schedule_file_wants_a_stream_all_day(self):
        fake = FakeBroadcaster()
        mocks, ctx = _patched(broadcaster=fake)
        with ctx:
            main.update_broadcast(at(3), None)

        assert fake.updates == [(at(3), True)]

    def test_the_switch_off_is_never_wanted(self):
        fake = FakeBroadcaster()
        mocks, ctx = _patched(config=_make_config(broadcast_enabled=False),
                              broadcaster=fake)
        with ctx:
            main.update_broadcast(at(12), _OPEN)
            main.update_broadcast(at(3), None)

        assert fake.updates == [(at(12), False), (at(3), False)]

    def test_a_rehearsal_is_never_wanted(self):
        fake = FakeBroadcaster()
        mocks, ctx = _patched(broadcaster=fake)
        with ctx:
            main.update_broadcast(at(12), _OPEN, dry_run=True)

        assert fake.updates == [(at(12), False)]

    def test_a_rehearsal_stops_a_live_stream_it_finds(self):
        fake = FakeBroadcaster(live=True)
        mocks, ctx = _patched(broadcaster=fake, installed=fake)
        with ctx:
            main.run_monitor_cycle(_OPEN, moment=at(12), dry_run=True)

        assert fake.updates == [(at(12), False)]
        assert fake.live is False

    def test_the_recording_mode_does_not_influence_it(self):
        wanted = []
        for mode in ("off", "long-living", "follow-schedule"):
            fake = FakeBroadcaster()
            mocks, ctx = _patched(
                config=_make_config(recording_mode=mode), broadcaster=fake
            )
            with ctx:
                main.run_monitor_cycle(_OPEN, moment=at(12))
            wanted.append(fake.updates)

        assert wanted == [[(at(12), True)], [(at(12), True)], [(at(12), True)]]


# ---------------------------------------------------------------------------
# Placement inside the monitor cycle
# ---------------------------------------------------------------------------

class TestMonitorCyclePlacement:
    """Stop before the gate ends the cycle; update after the screen fix."""

    def test_inside_the_period_calls_update_with_wanted(self):
        fake = FakeBroadcaster()
        mocks, ctx = _patched(broadcaster=fake)
        with ctx:
            main.run_monitor_cycle(_OPEN, moment=at(12))

        assert fake.updates == [(at(12), True)]
        assert fake.stops == 0

    def test_outside_the_period_stops_and_never_updates(self):
        fake = FakeBroadcaster(live=True)
        mocks, ctx = _patched(broadcaster=fake, installed=fake,
                              is_telegram_running=MagicMock(return_value=True))
        with ctx:
            main.run_monitor_cycle(_OPEN, moment=at(20))

        assert fake.updates == []
        assert fake.stops == 1

    def test_the_stop_runs_before_telegram_is_shut_down(self):
        events = []
        fake = FakeBroadcaster(live=True, events=events)
        kill = MagicMock(side_effect=lambda: events.append("kill_telegram"))
        mocks, ctx = _patched(broadcaster=fake, installed=fake,
                              is_telegram_running=MagicMock(return_value=True),
                              kill_telegram=kill)
        with ctx:
            main.run_monitor_cycle(_OPEN, moment=at(20))

        assert events == ["broadcast.stop", "kill_telegram"]

    def test_the_period_end_stops_the_stream_once(self):
        fake = FakeBroadcaster()
        mocks, ctx = _patched(broadcaster=fake, installed=fake)
        with ctx:
            main.run_monitor_cycle(_OPEN, moment=at(12))   # inside -> wanted
            main.run_monitor_cycle(_OPEN, moment=at(20))   # outside -> stopped

        assert fake.stops == 1

    def test_the_update_runs_after_the_screen_fix(self):
        events = []
        fake = FakeBroadcaster(events=events)
        screen = MagicMock(side_effect=lambda *args, **kwargs: events.append("screen"))
        mocks, ctx = _patched(broadcaster=fake, ensure_correct_screen=screen)
        with ctx:
            main.run_monitor_cycle(_OPEN, moment=at(12))

        assert events == ["screen", "broadcast.update"]

    def test_the_update_never_runs_before_the_gate(self):
        fake = FakeBroadcaster()
        mocks, ctx = _patched(broadcaster=fake, installed=fake)
        with ctx:
            main.run_monitor_cycle(_OPEN, moment=at(20))  # outside the period

        assert fake.updates == []
        mocks["ensure_correct_screen"].assert_not_called()

    def test_the_launch_guard_starts_nothing(self):
        fake = FakeBroadcaster()
        mocks, ctx = _patched(broadcaster=fake, installed=fake)
        with ctx:
            main.run_monitor_cycle(_NARROW, moment=at(9, 59, 55))

        assert fake.updates == []
        assert fake.stops == 0
        mocks["launch_telegram"].assert_not_called()

    def test_the_launch_guard_with_telegram_running_still_updates(self):
        fake = FakeBroadcaster()
        mocks, ctx = _patched(broadcaster=fake, installed=fake,
                              is_telegram_running=MagicMock(return_value=True))
        with ctx:
            main.run_monitor_cycle(_NARROW, moment=at(9, 59, 55))

        assert fake.updates == [(at(9, 59, 55), True)]

    def test_click_retry_leaves_the_stream_alone(self):
        fake = FakeBroadcaster()
        mocks, ctx = _patched(broadcaster=fake, installed=fake,
                              is_telegram_running=MagicMock(return_value=True))
        # Telegram running, Target Window missing at first, then found by the
        # Click Retry: no Recovery, so the stream must be untouched.
        mocks["gw"].getWindowsWithTitle.side_effect = [[], ["TargetWindow"]]
        with ctx:
            main.run_monitor_cycle(None, moment=at(12))

        mocks["perform_click_sequence"].assert_called_once()
        mocks["kill_telegram"].assert_not_called()
        assert fake.stops == 0
        assert len(fake.updates) == 1

    def test_recovery_neither_stops_nor_starts_anything_extra(self):
        fake = FakeBroadcaster()
        mocks, ctx = _patched(broadcaster=fake, installed=fake,
                              is_telegram_running=MagicMock(return_value=True))
        mocks["gw"].getWindowsWithTitle.return_value = []
        with ctx:
            main.run_monitor_cycle(None, moment=at(12))

        mocks["kill_telegram"].assert_called_once()
        assert fake.stops == 0
        # One update, from the cycle's own check — Recovery added nothing.
        assert fake.updates == [(at(12), True)]


# ---------------------------------------------------------------------------
# Startup and shutdown in the entry point
# ---------------------------------------------------------------------------

class TestStartupAndShutdown:
    """prepare once at startup, nothing started, and one stop on Ctrl+C."""

    def test_startup_prepares_once_and_starts_nothing(self):
        fake = FakeBroadcaster()
        mocks, ctx = _patched(broadcaster=fake,
                              run_monitor_cycle=MagicMock(side_effect=KeyboardInterrupt))
        with ctx:
            main.main()

        assert fake.prepares == [False]
        assert fake.updates == []

    def test_startup_prepares_a_rehearsal_with_the_flag(self):
        fake = FakeBroadcaster()
        mocks, ctx = _patched(broadcaster=fake,
                              sys=MagicMock(argv=["main.py", "--dry-run"]))
        with ctx:
            main.main()

        assert fake.prepares == [True]
        mocks["run_dry_run"].assert_called_once()

    def test_startup_prepares_and_starts_no_stream(self):
        # OBS is opened once, at startup, whatever the clock says; the stream
        # itself is left alone until a cycle asks for it.
        fake = FakeBroadcaster()
        mocks, ctx = _patched(
            broadcaster=fake,
            run_monitor_cycle=MagicMock(side_effect=KeyboardInterrupt),
        )
        with ctx:
            main.main()

        assert fake.prepares == [False]
        assert fake.updates == []

    def test_ctrl_c_stops_the_broadcaster_exactly_once(self):
        fake = FakeBroadcaster(live=True)
        mocks, ctx = _patched(
            broadcaster=fake,
            installed=fake,
            run_monitor_cycle=MagicMock(side_effect=KeyboardInterrupt),
        )
        with ctx:
            main.main()

        assert fake.stops == 1

    def test_a_raising_broadcaster_leaves_the_cycle_launching_telegram(self):
        fake = FakeBroadcaster(raises=True)
        mocks, ctx = _patched(broadcaster=fake, installed=fake)
        with ctx:
            main.run_monitor_cycle(_OPEN, moment=at(12))

        mocks["launch_telegram"].assert_called_once()

    def test_a_raising_broadcaster_leaves_the_gate_cycle_passing(self):
        fake = FakeBroadcaster(raises=True)
        mocks, ctx = _patched(broadcaster=fake, installed=fake,
                              is_telegram_running=MagicMock(return_value=True))
        with ctx:
            main.run_monitor_cycle(_OPEN, moment=at(20))

        mocks["kill_telegram"].assert_called_once()


# ---------------------------------------------------------------------------
# The read-only check path
# ---------------------------------------------------------------------------

class TestCheckObsFlag:
    """``--check-obs`` is recognised like ``--dry-run``."""

    def test_flag_present(self):
        assert main.check_obs_requested(["--check-obs"]) is True

    def test_flag_among_others(self):
        assert main.check_obs_requested(["something", "--check-obs"]) is True

    def test_flag_absent(self):
        assert main.check_obs_requested([]) is False
        assert main.check_obs_requested(["--dry-run"]) is False


class TestCheckObsReport:
    """Six lines through logging, the password as yes/no, exit 0 or 1."""

    def test_six_lines_are_reported_in_order(self, caplog):
        mocks, ctx = _patched(datetime=_clock(at(12)))
        with ctx, caplog.at_level(logging.INFO):
            code = main.print_obs_check(_OPEN)

        messages = _main_messages(caplog)
        assert code == 0
        assert len(messages) == 6
        assert messages[0].startswith("Broadcast switch: on")
        assert messages[1:5] == _OBS_LINES
        assert messages[5].startswith("Would be live now: yes")

    def test_the_password_is_never_printed(self, caplog):
        mocks, ctx = _patched()
        with ctx, caplog.at_level(logging.DEBUG):
            main.print_obs_check(_OPEN)

        assert _PASSWORD not in caplog.text
        assert "OBS password: set." in caplog.text

    def test_an_unset_password_is_reported_as_not_set(self, caplog):
        lines = list(_OBS_LINES)
        lines[1] = "OBS password: not set."
        mocks, ctx = _patched(
            config=_make_config(obs_password=""),
            check_report=MagicMock(return_value=(lines, True)),
        )
        with ctx, caplog.at_level(logging.INFO):
            main.print_obs_check(_OPEN)

        assert "OBS password: not set." in caplog.text

    def test_exit_zero_when_obs_answered(self):
        mocks, ctx = _patched()
        with ctx:
            assert main.print_obs_check(_OPEN) == 0

    def test_exit_one_when_obs_did_not_answer(self, caplog):
        lines = list(_OBS_LINES)
        lines[3] = "OBS stream: could not be asked (connection refused)."
        mocks, ctx = _patched(
            check_report=MagicMock(return_value=(lines, False))
        )
        with ctx, caplog.at_level(logging.INFO):
            code = main.print_obs_check(_OPEN)

        assert code == 1
        assert "connection refused" in caplog.text

    def test_the_switch_off_is_reported(self, caplog):
        mocks, ctx = _patched(config=_make_config(broadcast_enabled=False))
        with ctx, caplog.at_level(logging.INFO):
            code = main.print_obs_check(_OPEN)

        assert code == 0
        assert "Broadcast switch: off." in caplog.text
        assert "Would be live now: no (the broadcast switch is off)." in caplog.text

    def test_would_be_live_says_yes_inside_the_period(self, caplog):
        mocks, ctx = _patched(datetime=_clock(at(12)))
        with ctx, caplog.at_level(logging.INFO):
            main.print_obs_check(_OPEN)

        assert "Would be live now: yes" in caplog.text

    def test_would_be_live_says_no_outside_the_period(self, caplog):
        mocks, ctx = _patched(datetime=_clock(at(20)))
        with ctx, caplog.at_level(logging.INFO):
            main.print_obs_check(_OPEN)

        assert "outside every allowed period" in caplog.text

    def test_no_schedule_file_counts_as_always_allowed(self, caplog):
        mocks, ctx = _patched(datetime=_clock(at(3)))
        with ctx, caplog.at_level(logging.INFO):
            main.print_obs_check(None)

        assert "Would be live now: yes" in caplog.text

    def test_an_unknown_report_fault_still_writes_the_other_lines(self, caplog):
        mocks, ctx = _patched(
            datetime=_clock(at(12)),
            check_report=MagicMock(side_effect=RuntimeError("report exploded")),
        )
        with ctx, caplog.at_level(logging.INFO):
            code = main.print_obs_check(_OPEN)

        assert code == 1
        assert "Broadcast switch: on" in caplog.text
        assert "Would be live now: yes" in caplog.text


class TestCheckObsEntryPoint:
    """When ``--check-obs`` is present nothing else runs at all."""

    def test_it_exits_zero_when_obs_answered(self):
        mocks, ctx = _patched(sys=MagicMock(argv=["main.py", "--check-obs"]))
        with ctx:
            with pytest.raises(SystemExit) as stopped:
                main.main()

        assert stopped.value.code == 0

    def test_it_exits_one_when_obs_did_not_answer(self):
        mocks, ctx = _patched(
            sys=MagicMock(argv=["main.py", "--check-obs"]),
            check_report=MagicMock(return_value=(list(_OBS_LINES), False)),
        )
        with ctx:
            with pytest.raises(SystemExit) as stopped:
                main.main()

        assert stopped.value.code == 1

    def test_it_touches_no_window_screen_rotation_recording_or_telegram(self):
        fake = FakeBroadcaster()
        mocks, ctx = _patched(
            broadcaster=fake,
            sys=MagicMock(argv=["main.py", "--check-obs"]),
            run_monitor_cycle=MagicMock(),
        )
        with ctx:
            with pytest.raises(SystemExit):
                main.main()

        # Nothing of the working program ran.
        mocks["set_dpi_awareness"].assert_not_called()
        mocks["ensure_correct_screen"].assert_not_called()
        mocks["set_screen_size"].assert_not_called()
        mocks["read_screen_size"].assert_not_called()
        mocks["rotatescreen"].assert_not_called()
        mocks["log_recording_startup"].assert_not_called()
        mocks["get_recorder"].assert_not_called()
        mocks["get_broadcaster"].assert_not_called()
        mocks["is_telegram_running"].assert_not_called()
        mocks["kill_telegram"].assert_not_called()
        mocks["launch_telegram"].assert_not_called()
        mocks["perform_telegram_actions"].assert_not_called()
        mocks["setup_second_window"].assert_not_called()
        mocks["perform_click_sequence"].assert_not_called()
        mocks["gw"].getWindowsWithTitle.assert_not_called()
        mocks["run_monitor_cycle"].assert_not_called()
        mocks["run_dry_run"].assert_not_called()
        assert mocks["time"].sleep.call_count == 0
        assert fake.prepares == []
        assert fake.updates == []
        assert fake.stops == 0

    def test_it_wins_over_the_rehearsal(self):
        mocks, ctx = _patched(
            sys=MagicMock(argv=["main.py", "--check-obs", "--dry-run"])
        )
        with ctx:
            with pytest.raises(SystemExit):
                main.main()

        mocks["run_dry_run"].assert_not_called()
        mocks["get_broadcaster"].assert_not_called()
        mocks["set_dpi_awareness"].assert_not_called()

    def test_a_broken_schedule_file_is_still_refused(self):
        mocks, ctx = _patched(
            sys=MagicMock(argv=["main.py", "--check-obs"]),
            validate_schedule_at_startup=MagicMock(side_effect=SystemExit(1)),
        )
        with ctx:
            with pytest.raises(SystemExit) as stopped:
                main.main()

        assert stopped.value.code == 1
        mocks["get_broadcaster"].assert_not_called()
