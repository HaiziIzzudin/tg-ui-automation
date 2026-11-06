"""Tests for ``TELEGRAM_PATH`` and the window-work refusal.

The settings give a program file. While that file is not an existing file there
is no window work at all: no launch and no Setup Sequence, no Click Sequence or
Click Retry, no resize/restore/activation/minimization, no Target Window work,
and no Recovery kill. The screen fix, broadcasting, recording, and the List
Recorder carry on, and the outside-hours Shutdown still runs.

The log speaks on change only: one error when the file goes missing, silence
while it stays missing, one line when it comes back.

No real Telegram is touched: every window/process helper is a recorder of
calls and the program file lives in the test's own temporary folder.
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

_MONDAY = datetime.datetime(2024, 1, 1)


def at(hour: int, minute: int = 0) -> datetime.datetime:
    """A fixed Monday moment (local clock)."""
    return _MONDAY + datetime.timedelta(hours=hour, minutes=minute)


# Allowed 09:00-17:00 every day: 12:00 is inside, 20:00 is outside.
_OPEN = parse_schedule('{"all": ["09:00-17:00"]}')


@pytest.fixture(autouse=True)
def clean_state():
    """Forget the "Telegram went missing" memory between tests."""
    main._telegram_program_missing = False
    yield
    main._telegram_program_missing = False


def _make_config(telegram_path, recordings_dir=r"D:\Recordings"):
    """A Config-like MagicMock holding the values one cycle reads."""
    cfg = MagicMock()
    cfg.telegram_path = str(telegram_path)
    cfg.recordings_dir = recordings_dir
    cfg.record_list_enabled = False
    cfg.recording_mode = "off"
    cfg.broadcast_enabled = False
    cfg.scale_factor = 1.0
    cfg.refresh_scale_factor = MagicMock(return_value=1.0)
    cfg.screen_resolution = (1920, 1080)
    cfg.screen_rotation = 0
    cfg.second_window_title = "TargetWindow"
    cfg.second_window_retry_limit = 1
    cfg.click_retry_interval = 0.0
    cfg.kill_cooldown_seconds = 0
    return cfg


def _cycle_mocks(*, config, **overrides):
    """Return ``(mocks, patch_context)`` with every window helper recorded."""
    window = MagicMock()
    window.title = "TargetWindow"
    values = dict(
        config=config,
        is_telegram_running=MagicMock(return_value=True),
        kill_telegram=MagicMock(),
        launch_telegram=MagicMock(return_value=True),
        perform_telegram_actions=MagicMock(return_value=True),
        setup_second_window=MagicMock(return_value=True),
        perform_click_sequence=MagicMock(),
        gw=MagicMock(),
        ensure_correct_screen=MagicMock(),
        update_recording=MagicMock(),
        update_broadcast=MagicMock(),
        keep_list_recorder_alive=MagicMock(),
        _last_scale_factor=1.0,
    )
    values["gw"].getWindowsWithTitle.return_value = [window]
    values.update(overrides)
    return values, patch.multiple("main", **values)


def _messages(caplog):
    """Return the automator's own messages, in order."""
    return [record.getMessage() for record in caplog.records if record.name == "main"]


def _missing_line(path):
    return (
        f"The Telegram program '{path}' (TELEGRAM_PATH) was not found; no "
        f"Telegram or window work until it is back. Recording and broadcasting "
        f"are unaffected."
    )


def _back_line(path):
    return f"The Telegram program '{path}' is back; window work resumes."


# ---------------------------------------------------------------------------
# No window work while the program file is missing
# ---------------------------------------------------------------------------

class TestNoWindowWork:
    """A missing file refuses every window action and keeps the rest running."""

    def test_a_missing_file_stops_every_window_call(self, tmp_path):
        path = tmp_path / "Telegram.exe"
        mocks, ctx = _cycle_mocks(config=_make_config(path))
        with ctx:
            outcome = main.run_monitor_cycle(_OPEN, moment=at(12))

        assert outcome is None
        mocks["is_telegram_running"].assert_not_called()
        mocks["launch_telegram"].assert_not_called()
        mocks["perform_telegram_actions"].assert_not_called()
        mocks["setup_second_window"].assert_not_called()
        mocks["perform_click_sequence"].assert_not_called()
        mocks["kill_telegram"].assert_not_called()
        mocks["gw"].getWindowsWithTitle.assert_not_called()

    def test_the_screen_fix_broadcasting_and_helper_still_run(self, tmp_path):
        path = tmp_path / "Telegram.exe"
        mocks, ctx = _cycle_mocks(config=_make_config(path))
        with ctx:
            main.run_monitor_cycle(_OPEN, moment=at(12))

        mocks["ensure_correct_screen"].assert_called_once()
        mocks["update_broadcast"].assert_called_once()
        mocks["keep_list_recorder_alive"].assert_called_once()

    def test_a_folder_counts_as_missing(self, tmp_path, caplog):
        folder = tmp_path / "Telegram Desktop"
        folder.mkdir()
        mocks, ctx = _cycle_mocks(config=_make_config(folder))
        with ctx, caplog.at_level(logging.ERROR):
            main.run_monitor_cycle(_OPEN, moment=at(12))

        mocks["is_telegram_running"].assert_not_called()
        assert _missing_line(str(folder)) in caplog.text

    def test_the_error_names_the_path_and_the_setting(self, tmp_path, caplog):
        path = tmp_path / "Nowhere" / "Telegram.exe"
        mocks, ctx = _cycle_mocks(config=_make_config(path))
        with ctx, caplog.at_level(logging.ERROR):
            main.run_monitor_cycle(_OPEN, moment=at(12))

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert [r.getMessage() for r in errors] == [_missing_line(str(path))]

    def test_the_window_job_keeps_cycling(self, tmp_path):
        path = tmp_path / "Telegram.exe"
        mocks, ctx = _cycle_mocks(config=_make_config(path))
        with ctx:
            for _ in range(3):
                assert main.run_monitor_cycle(_OPEN, moment=at(12)) is None


# ---------------------------------------------------------------------------
# The log speaks on change only
# ---------------------------------------------------------------------------

class TestSpeaksOnChange:
    """One error at the loss, silence while it lasts, one line at the return."""

    def test_the_error_is_written_once_while_it_stays_missing(self, tmp_path, caplog):
        path = tmp_path / "Telegram.exe"
        mocks, ctx = _cycle_mocks(config=_make_config(path))
        with ctx, caplog.at_level(logging.INFO):
            for _ in range(4):
                main.run_monitor_cycle(_OPEN, moment=at(12))

        assert _messages(caplog) == [_missing_line(str(path))]

    def test_the_return_is_one_line_and_window_work_resumes(self, tmp_path, caplog):
        path = tmp_path / "Telegram.exe"
        mocks, ctx = _cycle_mocks(config=_make_config(path))
        with ctx, caplog.at_level(logging.INFO):
            main.run_monitor_cycle(_OPEN, moment=at(12))

        path.write_bytes(b"not really a program")
        with ctx, caplog.at_level(logging.INFO):
            main.run_monitor_cycle(_OPEN, moment=at(12))
            main.run_monitor_cycle(_OPEN, moment=at(12))

        assert _messages(caplog) == [
            _missing_line(str(path)),
            _back_line(str(path)),
        ]
        mocks["is_telegram_running"].assert_called()
        mocks["launch_telegram"].assert_not_called()
        mocks["kill_telegram"].assert_not_called()

    def test_a_later_loss_speaks_again(self, tmp_path, caplog):
        path = tmp_path / "Telegram.exe"
        path.write_bytes(b"present")
        mocks, ctx = _cycle_mocks(config=_make_config(path))
        with ctx, caplog.at_level(logging.INFO):
            main.run_monitor_cycle(_OPEN, moment=at(12))
            path.unlink()
            main.run_monitor_cycle(_OPEN, moment=at(12))

        assert _messages(caplog) == [_missing_line(str(path))]


# ---------------------------------------------------------------------------
# What keeps running, and what still stops
# ---------------------------------------------------------------------------

class TestTheRestIsUntouched:
    """Everything that does not read the Telegram path keeps working."""

    def test_outside_the_allowed_period_telegram_is_still_closed(self, tmp_path, caplog):
        path = tmp_path / "Telegram.exe"
        mocks, ctx = _cycle_mocks(config=_make_config(path))
        with ctx, caplog.at_level(logging.INFO):
            main.run_monitor_cycle(_OPEN, moment=at(20))

        mocks["kill_telegram"].assert_called_once_with()
        assert "shutting down Telegram" in caplog.text

    def test_an_existing_file_runs_the_normal_path(self, tmp_path):
        path = tmp_path / "Telegram.exe"
        path.write_bytes(b"program")
        mocks, ctx = _cycle_mocks(
            config=_make_config(path),
            is_telegram_running=MagicMock(return_value=False),
        )
        with ctx:
            main.run_monitor_cycle(_OPEN, moment=at(12))

        mocks["launch_telegram"].assert_called_once()
        mocks["perform_telegram_actions"].assert_called_once()

    def test_a_missing_file_never_stops_the_missing_file_check(
        self, tmp_path, caplog
    ):
        # The check is one cheap file test, re-run every cycle: restoring the
        # drive heals the run within one cycle, with no restart.
        path = tmp_path / "Telegram.exe"
        mocks, ctx = _cycle_mocks(config=_make_config(path))
        with ctx, caplog.at_level(logging.INFO):
            main.run_monitor_cycle(_OPEN, moment=at(12))
            path.write_bytes(b"program")
            main.run_monitor_cycle(_OPEN, moment=at(12))

        assert _back_line(str(path)) in _messages(caplog)


# ---------------------------------------------------------------------------
# The resolved program path and its file name
# ---------------------------------------------------------------------------

class TestTheResolvedPath:
    """The path is resolved once, and its file name drives detection and kill."""

    def test_an_absent_setting_uses_the_per_user_install(self):
        # The worktree's settings file never names TELEGRAM_PATH, so the loaded
        # settings already hold the per-user install.
        from config import DEFAULT_TELEGRAM_PATH

        assert main.telegram_program_path() == DEFAULT_TELEGRAM_PATH

    def test_the_process_name_follows_the_file_name(self):
        cfg = _make_config(r"D:\Apps\Telegram Coworkers\TelegramX.exe")
        with patch.object(main, "config", cfg):
            assert main.telegram_process_name() == "TelegramX.exe"

    def test_detection_matches_that_file_name(self):
        class FakeProcess:
            def __init__(self, name):
                self.info = {"name": name}

        cfg = _make_config(r"D:\Apps\TelegramX.exe")
        with patch.object(main, "config", cfg):
            with patch("main.psutil.process_iter",
                       return_value=[FakeProcess("Telegram.exe"),
                                     FakeProcess("TelegramX.exe")]):
                assert main.is_telegram_running() is True

            with patch("main.psutil.process_iter",
                       return_value=[FakeProcess("Telegram.exe")]):
                assert main.is_telegram_running() is False

    def test_the_kill_matches_that_file_name(self):
        class FakeProcess:
            def __init__(self, name):
                self.info = {"name": name}
                self.terminated = 0
                self.waited = None
                self.killed = 0

            def terminate(self):
                self.terminated += 1

            def wait(self, timeout=None):
                self.waited = timeout

            def kill(self):
                self.killed += 1

        other = FakeProcess("Telegram.exe")
        wanted = FakeProcess("TelegramX.exe")
        cfg = _make_config(r"D:\Apps\TelegramX.exe")
        with patch.object(main, "config", cfg):
            with patch("main.psutil.process_iter", return_value=[other, wanted]):
                assert main.kill_telegram() is True

        assert wanted.terminated == 1
        assert other.terminated == 0

    def test_the_launch_uses_the_resolved_path(self):
        class FakeWindow:
            title = "Telegram"

        class FakeClock:
            def __init__(self):
                self.now = 0.0

            def time(self):
                self.now += 1.0
                return self.now

            def sleep(self, seconds):
                pass

        cfg = _make_config(r"D:\Apps\Telegram Desktop\Telegram.exe")
        with patch.object(main, "config", cfg):
            with patch("main.subprocess.Popen") as popen, \
                 patch("main.gw") as gw, \
                 patch("main.time", FakeClock()):
                gw.getWindowsWithTitle.return_value = [FakeWindow()]
                assert main.launch_telegram() is True

        popen.assert_called_once_with(r"D:\Apps\Telegram Desktop\Telegram.exe")
