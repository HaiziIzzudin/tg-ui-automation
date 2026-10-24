"""Tests for the recorder module.

Every test drives a fake child process: the recorder program is never run and
no real stream is opened. ``subprocess.Popen`` is replaced, so only the
argument list, the pipes, and the start folder the recorder asks for are
observed.
"""
import datetime
import io
import logging
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add the parent directory to sys.path so we can import recorder
sys.path.insert(0, str(Path(__file__).parent.parent))

import recorder
from recorder import (
    CHILD_TAIL_LINES,
    CLIP_NAME_PATTERN,
    LIVE_START_INDEX,
    MIN_FREE_BYTES_STOP,
    MIN_FREE_BYTES_WARN,
    SEGMENT_FORMAT_OPTIONS,
    STATUS_GAVE_UP,
    STREAM_CHECK_CUSHION,
    STREAM_CHECK_MAX_LOOKBACK,
    ChildOutputReader,
    Recorder,
    RecorderSettings,
    build_ffmpeg_command,
    choose_live_start_index,
    clamp_start_index,
    classify_free_space,
    ffmpeg_available,
    is_useful_child_line,
)


@pytest.fixture(autouse=True)
def fake_stream_check(monkeypatch):
    """Never touch the network: fake the Stream Check for every recorder test.

    A test that cares about the check calls the real function itself (with its
    request layer faked) or overrides this fixture's return value.
    """
    fake = MagicMock(return_value=LIVE_START_INDEX)
    monkeypatch.setattr(recorder, "choose_live_start_index", fake)
    return fake


# 2024-01-01 is a Monday; moments are offsets from it, on the local clock.
_MONDAY = datetime.datetime(2024, 1, 1)


def at(seconds: int = 0) -> datetime.datetime:
    return _MONDAY + datetime.timedelta(seconds=seconds)


def make_settings(output_dir, **overrides) -> RecorderSettings:
    values = dict(
        stream_url="http://example.test/live.m3u8",
        output_dir=str(output_dir),
        clip_seconds=7200,
        reconnect_seconds=5.0,
        ffmpeg_path="ffmpeg",
    )
    values.update(overrides)
    return RecorderSettings(**values)


# ==============================================================================
# Fake child process
# ==============================================================================

class FakeInput:
    """A stand-in for the child's stdin pipe.

    A write of the quit key ends the child when it is set to quit on cue, so a
    graceful stop can be told apart from a terminate or a kill.
    """

    def __init__(self, child):
        self.child = child
        self.written = []
        self.flushed = False

    def write(self, data):
        self.written.append(data)
        if self.child.quit_on_q:
            self.child.alive = False
        return len(data)

    def flush(self):
        self.flushed = True


class FakeChild:
    """A fake recorder process with just the surface the recorder uses."""

    def __init__(self, exit_code=0, quit_on_q=True, ignore_terminate=False, stderr=None):
        self.exit_code = exit_code
        self.alive = True
        self.quit_on_q = quit_on_q
        self.ignore_terminate = ignore_terminate
        self.terminated = False
        self.killed = False
        self.waits = []
        self.stdin = FakeInput(self)
        self.stderr = stderr

    def poll(self):
        return None if self.alive else self.exit_code

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.alive:
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
        return self.exit_code

    def terminate(self):
        self.terminated = True
        if not self.ignore_terminate:
            self.alive = False

    def kill(self):
        self.killed = True
        self.alive = False

    def die(self, exit_code=1):
        """Simulate the stream dropping and the child leaving on its own."""
        self.alive = False
        self.exit_code = exit_code


class ExplodingChild:
    """A child whose every method raises, to prove stop() never raises."""

    def __init__(self):
        self.stdin = ExplodingInput()

    def poll(self):
        raise RuntimeError("poll blew up")

    def wait(self, timeout=None):
        raise RuntimeError("wait blew up")

    def terminate(self):
        raise RuntimeError("terminate blew up")

    def kill(self):
        raise RuntimeError("kill blew up")


class ExplodingInput:
    def write(self, data):
        raise RuntimeError("write blew up")

    def flush(self):
        raise RuntimeError("flush blew up")


class Spawner:
    """A fake ``subprocess.Popen`` handing out prepared fake children."""

    def __init__(self, *children):
        self.children = list(children)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, "kwargs": kwargs})
        if not self.children:
            raise AssertionError("The recorder spawned more children than expected")
        return self.children.pop(0)


def install_spawner(monkeypatch, *children) -> Spawner:
    spawner = Spawner(*children)
    monkeypatch.setattr(recorder.subprocess, "Popen", spawner)
    return spawner


class FailingSpawner(Spawner):
    """A fake ``subprocess.Popen`` that runs out of children and then fails.

    The prepared children are handed out first; every later start fails the
    way a broken recorder program would. This is what lets a test walk a
    recorder through failed reconnection attempts.
    """

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, "kwargs": kwargs})
        if self.children:
            return self.children.pop(0)
        raise OSError("the recorder program refused to start")


def install_failing_spawner(monkeypatch, *children) -> FailingSpawner:
    spawner = FailingSpawner(*children)
    monkeypatch.setattr(recorder.subprocess, "Popen", spawner)
    return spawner


# ==============================================================================
# Settings and command
# ==============================================================================

class TestRecorderSettings:
    def test_is_frozen(self, tmp_path):
        settings = make_settings(tmp_path)
        with pytest.raises(FrozenInstanceError):
            settings.stream_url = "elsewhere"

    def test_holds_the_given_values(self, tmp_path):
        settings = make_settings(tmp_path, clip_seconds=60, reconnect_seconds=2.5)
        assert settings.clip_seconds == 60
        assert settings.reconnect_seconds == 2.5

    def test_the_retry_and_prefix_values_are_defaulted(self, tmp_path):
        settings = make_settings(tmp_path)
        assert settings.clip_name_prefix == ""
        assert settings.retry_limit is None
        assert settings.retry_reset_seconds == 120.0

    def test_positional_callers_go_on_working(self, tmp_path):
        # The old five values alone still build the settings, so the new
        # values had to be added last.
        settings = RecorderSettings(
            "http://example.test/live.m3u8", str(tmp_path), 60, 5.0, "ffmpeg"
        )
        assert settings.clip_name_prefix == ""
        assert settings.retry_limit is None
        assert settings.retry_reset_seconds == 120.0


class TestBuildFfmpegCommand:
    def test_mirrors_the_old_script_argument_for_argument(self, tmp_path):
        settings = make_settings(tmp_path, stream_url="http://host/live.m3u8")
        command = build_ffmpeg_command(settings)
        assert command == [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-live_start_index", str(LIVE_START_INDEX),
            "-i", "http://host/live.m3u8",
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-f", "segment",
            "-segment_time", "7200",
            "-reset_timestamps", "1",
            "-segment_format", "mp4",
            "-segment_format_options", SEGMENT_FORMAT_OPTIONS,
            "-strftime", "1",
            os.path.join(str(tmp_path), CLIP_NAME_PATTERN),
        ]

    def test_the_fragment_flags_reach_the_file_writer_not_the_wrapper(self, tmp_path):
        # A plain -movflags goes to the "one file per Clip" wrapper, which has
        # no such option: it is silently ignored, the index is written only on
        # a clean close, and every Clip killed or still open cannot be played.
        # The flags must travel to the writer through -segment_format_options.
        command = build_ffmpeg_command(make_settings(tmp_path))
        assert "-movflags" not in command
        options = command[command.index("-segment_format_options") + 1]
        assert "frag_keyframe" in options and "empty_moov" in options
        assert command[command.index("-segment_format") + 1] == "mp4"

    def test_the_spawned_command_carries_the_writer_options(self, tmp_path, monkeypatch):
        # Same protection seen from the outside: the command the recorder
        # actually spawns carries the writer options.
        spawner = install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())

        spawned = spawner.calls[0]["command"]
        assert "-segment_format_options" in spawned
        assert "-movflags" not in spawned

    def test_clip_length_is_written_from_the_settings(self, tmp_path):
        command = build_ffmpeg_command(make_settings(tmp_path, clip_seconds=900))
        assert command[command.index("-segment_time") + 1] == "900"

    def test_starts_at_the_live_edge_never_the_oldest_segment(self, tmp_path):
        # The playlist's oldest entries were deleted on the server long before
        # now, so asking for the oldest segment (index 0) means endless
        # not-found errors and no Clip ever appears. The shipped command must
        # stay off it: it starts a few segments back from the newest instead.
        assert LIVE_START_INDEX < 0
        command = build_ffmpeg_command(make_settings(tmp_path))
        start = command[command.index("-live_start_index") + 1]
        assert start == str(LIVE_START_INDEX)
        assert start != "0"

    def test_output_pattern_is_timestamped_and_lives_in_the_clips_folder(self, tmp_path):
        command = build_ffmpeg_command(make_settings(tmp_path))
        pattern = command[-1]
        assert pattern.endswith(CLIP_NAME_PATTERN)
        assert pattern.startswith(str(tmp_path))

    def test_recorder_program_is_first(self, tmp_path):
        command = build_ffmpeg_command(
            make_settings(tmp_path, ffmpeg_path=r"C:\tools\ffmpeg.exe")
        )
        assert command[0] == r"C:\tools\ffmpeg.exe"

    def test_uses_the_chosen_start_index_when_given(self, tmp_path):
        command = build_ffmpeg_command(make_settings(tmp_path), -7)
        assert command[command.index("-live_start_index") + 1] == "-7"

    def test_suppresses_the_progress_lines(self, tmp_path):
        # Continuous progress lines must never reach the log at any level.
        assert "-nostats" in build_ffmpeg_command(make_settings(tmp_path))

    def test_does_not_add_nostdin(self, tmp_path):
        # The graceful quit writes the quit key to the child's input, so the
        # no-stdin flag must never be added.
        assert "-nostdin" not in build_ffmpeg_command(make_settings(tmp_path))

    def test_an_empty_prefix_keeps_todays_pattern_exactly(self, tmp_path):
        command = build_ffmpeg_command(make_settings(tmp_path, clip_name_prefix=""))
        assert command[-1] == os.path.join(str(tmp_path), CLIP_NAME_PATTERN)

    def test_a_prefix_leads_the_file_name_with_a_two_digit_year(self, tmp_path):
        command = build_ffmpeg_command(
            make_settings(tmp_path, clip_name_prefix="Alif_Naim_")
        )
        assert command[-1] == os.path.join(
            str(tmp_path), "Alif_Naim_%y%m%d_%H%M%S.mp4"
        )

    def test_the_prefix_never_becomes_a_folder(self, tmp_path):
        command = build_ffmpeg_command(
            make_settings(tmp_path, clip_name_prefix="Alif_Naim_")
        )
        assert os.path.dirname(command[-1]) == str(tmp_path)


class TestFfmpegAvailable:
    def test_bare_name_on_the_system_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(recorder.shutil, "which", lambda name: r"C:\bin\ffmpeg.exe")
        assert ffmpeg_available(make_settings(tmp_path, ffmpeg_path="ffmpeg")) is True

    def test_bare_name_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(recorder.shutil, "which", lambda name: None)
        assert ffmpeg_available(make_settings(tmp_path, ffmpeg_path="ffmpeg")) is False

    def test_existing_absolute_path(self, tmp_path):
        program = tmp_path / "ffmpeg.exe"
        program.write_text("")
        assert ffmpeg_available(make_settings(tmp_path, ffmpeg_path=str(program))) is True

    def test_missing_absolute_path(self, tmp_path):
        missing = tmp_path / "nowhere" / "ffmpeg.exe"
        assert ffmpeg_available(make_settings(tmp_path, ffmpeg_path=str(missing))) is False

    def test_empty_path(self, tmp_path):
        assert ffmpeg_available(make_settings(tmp_path, ffmpeg_path="")) is False


class TestClassifyFreeSpace:
    def test_thresholds_are_the_agreed_constants(self):
        assert MIN_FREE_BYTES_WARN == 10 * 1024 ** 3
        assert MIN_FREE_BYTES_STOP == 2 * 1024 ** 3

    def test_well_above_warning_is_ok(self):
        assert classify_free_space(MIN_FREE_BYTES_WARN * 4) == "ok"

    def test_exactly_at_warning_is_ok(self):
        assert classify_free_space(MIN_FREE_BYTES_WARN) == "ok"

    def test_just_below_warning_is_warn(self):
        assert classify_free_space(MIN_FREE_BYTES_WARN - 1) == "warn"

    def test_exactly_at_stop_is_warn(self):
        assert classify_free_space(MIN_FREE_BYTES_STOP) == "warn"

    def test_just_below_stop_is_stop(self):
        assert classify_free_space(MIN_FREE_BYTES_STOP - 1) == "stop"

    def test_empty_disk_is_stop(self):
        assert classify_free_space(0) == "stop"

    def test_no_arguments_uses_the_module_constants(self):
        # A call with only free space keeps behaving like before.
        assert classify_free_space(MIN_FREE_BYTES_WARN) == "ok"
        assert classify_free_space(MIN_FREE_BYTES_WARN - 1) == "warn"
        assert classify_free_space(MIN_FREE_BYTES_STOP - 1) == "stop"

    def test_supplied_levels_move_the_boundaries(self):
        warn, stop = 500, 100
        assert classify_free_space(500, warn, stop) == "ok"
        assert classify_free_space(499, warn, stop) == "warn"
        assert classify_free_space(100, warn, stop) == "warn"
        assert classify_free_space(99, warn, stop) == "stop"

    def test_supplied_levels_are_used_without_the_constants(self):
        # Far below the module constants, yet above the supplied levels.
        assert classify_free_space(1, warn_bytes=1, stop_bytes=0) == "ok"
        assert classify_free_space(0, warn_bytes=1, stop_bytes=0) == "warn"
        assert classify_free_space(-1, warn_bytes=1, stop_bytes=0) == "stop"


# ==============================================================================
# Stream Check
# ==============================================================================
# The network is never touched: the one request function is replaced, so the
# whole check is driven from prepared playlists.

_MASTER = "http://host/live.m3u8"
_VARIANT = "http://host/path/variant.m3u8"
_MASTER_TEXT = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\npath/variant.m3u8\n"


def variant_text(count: int, prefix: str = "seg") -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for index in range(count):
        lines.append("#EXTINF:8.000,")
        lines.append(f"{prefix}{index}.ts")
    return "\n".join(lines) + "\n"


def segment_address(index: int, prefix: str = "seg") -> str:
    return f"http://host/path/{prefix}{index}.ts"


def install_fetcher(monkeypatch, responses, timeouts=None):
    """Replace the request function with a map of address -> reply or error.

    A reply that is an Exception instance is raised; anything else is returned
    as text. Every call's timeout is recorded when *timeouts* is given.
    """
    calls = []

    def fetch(address, timeout):
        calls.append((address, timeout))
        if timeouts is not None:
            timeouts.append(timeout)
        try:
            reply = responses[address]
        except KeyError:
            raise AssertionError(f"Unexpected request for {address!r}")
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(recorder, "fetch_playlist_text", fetch)
    return calls


class TestStreamCheck:
    """The check picks the newest real segment and never raises."""

    def test_the_newest_segment_costs_one_look(self, monkeypatch, caplog):
        responses = {_MASTER: _MASTER_TEXT, _VARIANT: variant_text(5)}
        responses[segment_address(4)] = "ok"
        calls = install_fetcher(monkeypatch, responses)

        with caplog.at_level(logging.INFO):
            chosen = choose_live_start_index(_MASTER)

        # Newest (position 0) works: start two back, one look.
        assert chosen == -2
        assert chosen == -(0 + STREAM_CHECK_CUSHION)
        assert [address for address, _ in calls] == [
            _MASTER, _VARIANT, segment_address(4),
        ]

    def test_it_walks_back_to_the_first_segment_that_answers(self, monkeypatch):
        responses = {_MASTER: _MASTER_TEXT, _VARIANT: variant_text(6)}
        # The newest two are listed but not there yet; the third answers.
        responses[segment_address(5)] = OSError("not found")
        responses[segment_address(4)] = OSError("not found")
        responses[segment_address(3)] = "ok"
        install_fetcher(monkeypatch, responses)

        chosen = choose_live_start_index(_MASTER)

        # The working segment is 2 back; the cushion makes the start 4 back.
        assert chosen == -(2 + STREAM_CHECK_CUSHION)

    def test_segments_are_resolved_against_the_variant_address(self, monkeypatch):
        responses = {
            _MASTER: "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\npath/variant.m3u8?token=abc\n",
            "http://host/path/variant.m3u8?token=abc": variant_text(3),
        }
        responses[segment_address(2)] = "ok"
        calls = install_fetcher(monkeypatch, responses)

        chosen = choose_live_start_index(_MASTER)

        assert chosen == -2
        assert [address for address, _ in calls] == [
            _MASTER,
            "http://host/path/variant.m3u8?token=abc",
            "http://host/path/seg2.ts",
        ]

    def test_the_oldest_entry_is_never_chosen(self, monkeypatch):
        responses = {_MASTER: _MASTER_TEXT, _VARIANT: variant_text(3)}
        responses[segment_address(2)] = OSError("not found")
        responses[segment_address(1)] = OSError("not found")
        responses[segment_address(0)] = "ok"  # only the oldest answers
        install_fetcher(monkeypatch, responses)

        chosen = choose_live_start_index(_MASTER)

        # -(2 + 2) = -4 would fall at or before the oldest; it is clamped to
        # the second oldest (-2), never the oldest (-3).
        assert chosen == -2
        assert chosen > -3

    def test_clamp_start_index_never_reaches_the_oldest(self):
        assert clamp_start_index(-4, 3) == -2
        assert clamp_start_index(-2, 10) == -2
        assert clamp_start_index(-20, 5) == -4

    def test_at_most_twelve_looks(self, monkeypatch, caplog):
        count = 20
        responses = {_MASTER: _MASTER_TEXT, _VARIANT: variant_text(count)}
        for index in range(count):
            responses[segment_address(index)] = OSError("not found")
        calls = install_fetcher(monkeypatch, responses)

        with caplog.at_level(logging.WARNING):
            chosen = choose_live_start_index(_MASTER)

        assert chosen == LIVE_START_INDEX
        looked = [a for a, _ in calls if a.startswith("http://host/path/seg")]
        assert len(looked) == STREAM_CHECK_MAX_LOOKBACK == 12
        assert any("default position" in r.getMessage() for r in caplog.records)

    def test_every_request_stays_within_the_per_request_timeout(self, monkeypatch):
        responses = {_MASTER: _MASTER_TEXT, _VARIANT: variant_text(4)}
        for index in range(4):
            responses[segment_address(index)] = OSError("not found")
        timeouts = []
        install_fetcher(monkeypatch, responses, timeouts=timeouts)

        choose_live_start_index(_MASTER)

        assert timeouts
        assert max(timeouts) <= recorder.STREAM_CHECK_REQUEST_TIMEOUT

    def test_the_total_deadline_stops_the_check(self, monkeypatch, caplog):
        clock = {"now": 0.0}
        monkeypatch.setattr(recorder.time, "monotonic", lambda: clock["now"])

        def fetch(address, timeout):
            if address == _MASTER:
                clock["now"] += 1.0
                return _MASTER_TEXT
            if address == _VARIANT:
                clock["now"] += 1.0
                return variant_text(20)
            clock["now"] += 20.0  # blow the ten-second budget
            raise OSError("not found")

        monkeypatch.setattr(recorder, "fetch_playlist_text", fetch)

        with caplog.at_level(logging.WARNING):
            chosen = choose_live_start_index(_MASTER)

        assert chosen == LIVE_START_INDEX
        assert any("default position" in r.getMessage() for r in caplog.records)

    def test_a_master_that_is_already_a_segment_list_is_used_as_is(self, monkeypatch):
        responses = {_MASTER: variant_text(4)}
        # With no variant line, the reply is the segment list and its segments
        # resolve against its own address.
        responses["http://host/seg3.ts"] = "ok"
        install_fetcher(monkeypatch, responses)

        assert choose_live_start_index(_MASTER) == -2

    def test_a_network_failure_falls_back_with_a_warning(self, monkeypatch, caplog):
        install_fetcher(monkeypatch, {_MASTER: OSError("no route to host")})

        with caplog.at_level(logging.WARNING):
            chosen = choose_live_start_index(_MASTER)

        assert chosen == LIVE_START_INDEX
        assert any(
            "could not look at the stream" in r.getMessage() for r in caplog.records
        )

    def test_an_empty_playlist_falls_back(self, monkeypatch, caplog):
        responses = {_MASTER: _MASTER_TEXT, _VARIANT: "#EXTM3U\n"}
        install_fetcher(monkeypatch, responses)

        with caplog.at_level(logging.WARNING):
            chosen = choose_live_start_index(_MASTER)

        assert chosen == LIVE_START_INDEX
        assert any(
            "no segments" in r.getMessage() for r in caplog.records
        )

    def test_the_outcome_is_logged_once_in_plain_words(self, monkeypatch, caplog):
        responses = {_MASTER: _MASTER_TEXT, _VARIANT: variant_text(4)}
        responses[segment_address(3)] = "ok"
        install_fetcher(monkeypatch, responses)

        with caplog.at_level(logging.INFO):
            choose_live_start_index(_MASTER)

        outcomes = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "Stream Check" in r.getMessage()
        ]
        assert len(outcomes) == 1

    def test_it_never_raises(self, monkeypatch):
        def exploding_fetch(address, timeout):
            raise RuntimeError("the whole world is broken")

        monkeypatch.setattr(recorder, "fetch_playlist_text", exploding_fetch)
        # Must not raise, and must give the documented fallback.
        assert choose_live_start_index(_MASTER) == LIVE_START_INDEX


# ==============================================================================
# Start
# ==============================================================================

class TestStart:
    def test_starts_a_child_and_reports_running(self, tmp_path, monkeypatch):
        child = FakeChild()
        spawner = install_spawner(monkeypatch, child)
        settings = make_settings(tmp_path / "clips")
        rec = Recorder(settings)

        assert rec.start(at()) is True
        assert rec.is_running() is True
        assert rec.status() == "running"
        assert spawner.calls[0]["command"] == build_ffmpeg_command(settings)

    def test_creates_the_clips_folder(self, tmp_path, monkeypatch):
        spawner = install_spawner(monkeypatch, FakeChild())
        clips = tmp_path / "Recordings"
        Recorder(make_settings(clips)).start(at())
        assert clips.is_dir()

    def test_gives_the_child_a_pipe_input_and_a_piped_error_stream(self, tmp_path, monkeypatch):
        spawner = install_spawner(monkeypatch, FakeChild())
        Recorder(make_settings(tmp_path)).start(at())
        kwargs = spawner.calls[0]["kwargs"]
        assert kwargs["stdin"] is subprocess.PIPE
        assert kwargs["stdout"] is subprocess.DEVNULL
        # The child's own words are piped to a background reader, never
        # discarded, so a failure can explain itself.
        assert kwargs["stderr"] is subprocess.PIPE

    def test_child_runs_in_the_automator_folder(self, tmp_path, monkeypatch):
        spawner = install_spawner(monkeypatch, FakeChild())
        Recorder(make_settings(tmp_path)).start(at())
        kwargs = spawner.calls[0]["kwargs"]
        assert kwargs["cwd"] == os.path.dirname(os.path.abspath(recorder.__file__))

    def test_empty_address_is_refused_with_a_log(self, tmp_path, monkeypatch, caplog):
        spawner = install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(tmp_path, stream_url=""))

        caplog.set_level(logging.ERROR)
        assert rec.start(at()) is False
        assert spawner.calls == []
        assert rec.status() == "stopped"
        assert any("stream address" in record.message for record in caplog.records)

    def test_failed_spawn_leaves_it_stopped(self, tmp_path, monkeypatch, caplog):
        def exploding_popen(command, **kwargs):
            raise OSError("no such program")

        monkeypatch.setattr(recorder.subprocess, "Popen", exploding_popen)
        rec = Recorder(make_settings(tmp_path))

        caplog.set_level(logging.ERROR)
        assert rec.start(at()) is False
        assert rec.is_running() is False
        assert rec.status() == "stopped"
        assert any("ffmpeg" in record.message for record in caplog.records)

    def test_unwritable_clips_folder_leaves_it_stopped(self, tmp_path, monkeypatch):
        def exploding_makedirs(path, exist_ok=False):
            raise OSError("denied")

        monkeypatch.setattr(recorder.os, "makedirs", exploding_makedirs)
        monkeypatch.setattr(recorder.subprocess, "Popen", lambda *a, **k: FakeChild())
        rec = Recorder(make_settings(tmp_path))

        assert rec.start(at()) is False
        assert rec.status() == "stopped"

    def test_start_while_running_spawns_nothing_more(self, tmp_path, monkeypatch):
        spawner = install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(tmp_path))
        assert rec.start(at()) is True
        assert rec.start(at()) is True
        assert len(spawner.calls) == 1

    def test_start_uses_the_chosen_start_index(self, tmp_path, monkeypatch, fake_stream_check):
        fake_stream_check.return_value = -7
        spawner = install_spawner(monkeypatch, FakeChild())
        settings = make_settings(tmp_path)

        Recorder(settings).start(at())

        command = spawner.calls[0]["command"]
        assert command[command.index("-live_start_index") + 1] == "-7"
        fake_stream_check.assert_called_once_with(settings.stream_url)

    def test_the_stream_check_runs_again_on_every_reconnection(
        self, tmp_path, monkeypatch, fake_stream_check
    ):
        first = FakeChild()
        second = FakeChild()
        install_spawner(monkeypatch, first, second)
        rec = Recorder(make_settings(tmp_path, reconnect_seconds=0.0))
        rec.start(at(0))
        first.die()

        rec.supervise(at(0))  # notice the drop
        rec.supervise(at(0))  # reconnect

        assert fake_stream_check.call_count == 2


# ==============================================================================
# Stop
# ==============================================================================

class TestStop:
    def test_graceful_stop_asks_the_child_to_quit(self, tmp_path, monkeypatch):
        child = FakeChild(quit_on_q=True)
        install_spawner(monkeypatch, child)
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())

        assert rec.stop() is True
        assert child.stdin.written == [b"q"]
        assert child.terminated is False
        assert child.killed is False
        assert rec.is_running() is False

    def test_stop_when_nothing_runs_returns_false(self, tmp_path, monkeypatch):
        install_spawner(monkeypatch)
        rec = Recorder(make_settings(tmp_path))
        assert rec.stop() is False

    def test_stop_after_a_dead_child_does_not_terminate(self, tmp_path, monkeypatch):
        child = FakeChild()
        install_spawner(monkeypatch, child)
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())
        child.die()

        assert rec.stop() is True
        assert child.terminated is False
        assert child.killed is False

    def test_lingering_child_is_terminated(self, tmp_path, monkeypatch):
        child = FakeChild(quit_on_q=False)
        install_spawner(monkeypatch, child)
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())

        assert rec.stop() is True
        assert child.terminated is True
        assert child.killed is False
        assert rec.is_running() is False

    def test_child_that_ignores_terminate_is_killed(self, tmp_path, monkeypatch):
        child = FakeChild(quit_on_q=False, ignore_terminate=True)
        install_spawner(monkeypatch, child)
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())

        assert rec.stop() is True
        assert child.terminated is True
        assert child.killed is True
        assert rec.is_running() is False

    def test_stop_never_raises(self, tmp_path, monkeypatch):
        install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())
        rec._child = ExplodingChild()

        rec.stop()  # must not raise
        assert rec.is_running() is False

    def test_plain_stop_leaves_the_recorder_stopped(self, tmp_path, monkeypatch):
        install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())

        rec.stop()
        assert rec.status() == "stopped"

    def test_stop_for_disk_sets_the_disk_stop_latch(self, tmp_path, monkeypatch):
        install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())

        rec.stop_for_disk()
        assert rec.status() == "disk-stop"

    def test_explicit_start_clears_the_disk_stop_latch(self, tmp_path, monkeypatch):
        install_spawner(monkeypatch, FakeChild(), FakeChild())
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())
        rec.stop_for_disk()
        assert rec.status() == "disk-stop"

        assert rec.start(at(10)) is True
        assert rec.status() == "running"


# ==============================================================================
# Restart
# ==============================================================================

class TestRestart:
    def test_restart_closes_the_old_child_and_starts_a_fresh_clip(self, tmp_path, monkeypatch):
        old = FakeChild()
        new = FakeChild()
        spawner = install_spawner(monkeypatch, old, new)
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())

        assert rec.restart(at(60)) is True
        assert old.stdin.written == [b"q"]
        assert len(spawner.calls) == 2
        assert rec.is_running() is True
        assert rec._child is new

    def test_restart_of_a_stopped_recorder_just_starts(self, tmp_path, monkeypatch):
        spawner = install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(tmp_path))
        assert rec.restart(at()) is True
        assert len(spawner.calls) == 1


# ==============================================================================
# Supervise (reconnection)
# ==============================================================================

class TestSupervise:
    def test_happy_path_child_is_left_alone(self, tmp_path, monkeypatch):
        spawner = install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())

        rec.supervise(at(30))
        assert len(spawner.calls) == 1
        assert rec.status() == "running"

    def test_never_started_recorder_is_left_alone(self, tmp_path, monkeypatch):
        spawner = install_spawner(monkeypatch)
        rec = Recorder(make_settings(tmp_path))

        rec.supervise(at(30))
        assert spawner.calls == []
        assert rec.status() == "stopped"

    def test_dropped_stream_waits_then_reconnects_with_a_fresh_clip(self, tmp_path, monkeypatch):
        first = FakeChild()
        second = FakeChild()
        spawner = install_spawner(monkeypatch, first, second)
        rec = Recorder(make_settings(tmp_path, reconnect_seconds=30.0))
        rec.start(at(0))
        first.die(exit_code=1)

        # First look notices the drop and starts the wait.
        rec.supervise(at(1))
        assert rec.status() == "waiting-to-retry"
        assert len(spawner.calls) == 1

        # Still inside the wait: nothing new.
        rec.supervise(at(29))
        assert rec.status() == "waiting-to-retry"
        assert len(spawner.calls) == 1

        # The wait has elapsed: a fresh Clip begins.
        rec.supervise(at(31))
        assert rec.status() == "running"
        assert len(spawner.calls) == 2
        assert rec._child is second

    def test_wait_is_measured_from_when_the_drop_was_noticed(self, tmp_path, monkeypatch):
        first = FakeChild()
        second = FakeChild()
        install_spawner(monkeypatch, first, second)
        rec = Recorder(make_settings(tmp_path, reconnect_seconds=10.0))
        rec.start(at(0))
        first.die()

        # The drop lay unnoticed until t=100; the wait starts then.
        rec.supervise(at(100))
        rec.supervise(at(109))
        assert rec.status() == "waiting-to-retry"
        rec.supervise(at(111))
        assert rec.status() == "running"

    def test_a_zero_wait_reconnects_at_once(self, tmp_path, monkeypatch):
        first = FakeChild()
        second = FakeChild()
        spawner = install_spawner(monkeypatch, first, second)
        rec = Recorder(make_settings(tmp_path, reconnect_seconds=0.0))
        rec.start(at(0))
        first.die()

        rec.supervise(at(0))
        rec.supervise(at(0))
        assert len(spawner.calls) == 2
        assert rec.status() == "running"

    def test_disk_stop_suppresses_reconnection(self, tmp_path, monkeypatch):
        spawner = install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())
        rec.stop_for_disk()

        rec.supervise(at(1000))
        assert rec.status() == "disk-stop"
        assert len(spawner.calls) == 1

    def test_failed_reconnect_is_retried_after_the_wait(self, tmp_path, monkeypatch):
        first = FakeChild()
        calls = []

        def flaky_popen(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                first.die()
                return first
            raise OSError("stream address wrong")

        monkeypatch.setattr(recorder.subprocess, "Popen", flaky_popen)
        rec = Recorder(make_settings(tmp_path, reconnect_seconds=5.0))
        rec.start(at(0))
        first.die()

        rec.supervise(at(1))  # notice
        rec.supervise(at(10))  # wait elapsed, start fails
        assert rec.status() == "waiting-to-retry"

        # A later cycle, after a fresh wait, tries again.
        rec.supervise(at(20))
        assert len(calls) == 3


# ==============================================================================
# Retry Limit (a List Recording gives up after a few attempts)
# ==============================================================================

def give_up_lines(caplog) -> list:
    """The recorded log lines that say no further attempt will be made."""
    return [
        record.message
        for record in caplog.records
        if "no more attempts" in record.message.lower()
    ]


class TestRetryLimit:
    def test_no_limit_reconnects_forever(self, tmp_path, monkeypatch):
        # The automator relies on endless reconnection, so None must change
        # nothing.
        first = FakeChild()
        spawner = install_failing_spawner(monkeypatch, first)
        rec = Recorder(make_settings(tmp_path, reconnect_seconds=5.0))
        rec.start(at(0))
        first.die()

        rec.supervise(at(1))  # notice the drop
        for second in range(10, 120, 10):
            rec.supervise(at(second))

        assert len(spawner.calls) > 5
        assert rec.status() == "waiting-to-retry"

    def test_two_failed_attempts_give_up_and_no_third_is_made(
        self, tmp_path, monkeypatch
    ):
        first = FakeChild()
        spawner = install_failing_spawner(monkeypatch, first)
        rec = Recorder(
            make_settings(tmp_path, reconnect_seconds=5.0, retry_limit=2)
        )
        rec.start(at(0))
        first.die()

        rec.supervise(at(1))  # notice the drop; the first attempt is due at t=6
        assert rec.status() == "waiting-to-retry"
        assert len(spawner.calls) == 1

        rec.supervise(at(6))  # first attempt fails
        assert rec.status() == "waiting-to-retry"
        assert len(spawner.calls) == 2

        rec.supervise(at(11))  # second attempt fails: that is the limit
        assert rec.status() == STATUS_GAVE_UP
        assert len(spawner.calls) == 3

        # Later monitor cycles must not attempt a third start.
        rec.supervise(at(60))
        rec.supervise(at(600))
        assert len(spawner.calls) == 3
        assert rec.status() == STATUS_GAVE_UP

    def test_a_long_lived_child_restores_the_attempts(self, tmp_path, monkeypatch):
        first = FakeChild()
        second = FakeChild()
        spawner = install_failing_spawner(monkeypatch, first)
        rec = Recorder(
            make_settings(
                tmp_path,
                reconnect_seconds=5.0,
                retry_limit=2,
                retry_reset_seconds=120.0,
            )
        )
        rec.start(at(0))
        first.die()

        rec.supervise(at(1))  # notice; first attempt due at t=6
        rec.supervise(at(6))  # first attempt fails: one failure counted
        assert rec.status() == "waiting-to-retry"
        assert len(spawner.calls) == 2

        spawner.children.append(second)
        rec.supervise(at(11))  # second attempt succeeds
        assert rec.status() == "running"
        assert len(spawner.calls) == 3

        # This child lived far longer than the reset window, so its exit is a
        # good run: the failure count starts over.
        second.die()
        rec.supervise(at(500))
        assert rec.status() == "waiting-to-retry"

        rec.supervise(at(505))  # attempt one of the new drop fails
        assert rec.status() == "waiting-to-retry"
        assert len(spawner.calls) == 4

        rec.supervise(at(510))  # attempt two fails: the limit again
        assert rec.status() == STATUS_GAVE_UP
        assert len(spawner.calls) == 5

    def test_a_short_lived_child_does_not_restore_the_attempts(
        self, tmp_path, monkeypatch
    ):
        first = FakeChild()
        second = FakeChild()
        spawner = install_failing_spawner(monkeypatch, first)
        rec = Recorder(
            make_settings(
                tmp_path,
                reconnect_seconds=0.0,
                retry_limit=2,
                retry_reset_seconds=120.0,
            )
        )
        rec.start(at(0))
        first.die()

        rec.supervise(at(1))  # notice
        rec.supervise(at(1))  # attempt one fails: one failure counted

        spawner.children.append(second)
        rec.supervise(at(1))  # attempt two succeeds, then drops at once
        assert rec.status() == "running"

        second.die()
        rec.supervise(at(2))  # noticed after one second: no reset
        rec.supervise(at(2))  # this failure reaches the limit of two
        assert rec.status() == STATUS_GAVE_UP
        assert len(spawner.calls) == 4

    def test_an_explicit_start_clears_giving_up_and_the_count(
        self, tmp_path, monkeypatch
    ):
        first = FakeChild()
        spare = FakeChild()
        spawner = install_failing_spawner(monkeypatch, first)
        rec = Recorder(
            make_settings(tmp_path, reconnect_seconds=0.0, retry_limit=2)
        )
        rec.start(at(0))
        first.die()

        rec.supervise(at(1))  # notice
        rec.supervise(at(1))  # attempt one fails
        rec.supervise(at(1))  # attempt two fails: gave up
        assert rec.status() == STATUS_GAVE_UP

        # The caller asks for recording again: that clears both the state and
        # the count, so a later drop gets its two attempts back.
        spawner.children.append(spare)
        assert rec.start(at(10)) is True
        assert rec.status() == "running"

        spare.die()
        rec.supervise(at(11))  # notice; the wait is zero
        rec.supervise(at(11))  # attempt one fails
        assert rec.status() == "waiting-to-retry"

        rec.supervise(at(11))  # attempt two fails: the full limit again
        assert rec.status() == STATUS_GAVE_UP
        assert len(spawner.calls) == 6

    def test_the_no_more_attempts_line_is_written_once(
        self, tmp_path, monkeypatch, caplog
    ):
        first = FakeChild()
        install_failing_spawner(monkeypatch, first)
        rec = Recorder(
            make_settings(tmp_path, reconnect_seconds=0.0, retry_limit=1)
        )
        rec.start(at(0))
        first.die()
        caplog.set_level(logging.INFO)

        rec.supervise(at(1))  # notice
        rec.supervise(at(1))  # the one attempt fails: gave up
        assert rec.status() == STATUS_GAVE_UP

        for second in (2, 3, 4):
            rec.supervise(at(second))

        assert len(give_up_lines(caplog)) == 1


# ==============================================================================
# State
# ==============================================================================

class TestState:
    def test_a_fresh_recorder_is_stopped(self, tmp_path):
        assert Recorder(make_settings(tmp_path)).status() == "stopped"

    def test_a_child_that_exited_counts_as_not_running(self, tmp_path, monkeypatch):
        child = FakeChild()
        install_spawner(monkeypatch, child)
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())

        child.die()
        assert rec.is_running() is False

    def test_status_reports_waiting_to_retry_before_the_wait_elapses(self, tmp_path, monkeypatch):
        child = FakeChild()
        install_spawner(monkeypatch, child)
        rec = Recorder(make_settings(tmp_path, reconnect_seconds=60.0))
        rec.start(at(0))
        child.die()

        rec.supervise(at(1))
        assert rec.status() == "waiting-to-retry"

    def test_status_reports_disk_stop(self, tmp_path, monkeypatch):
        install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())
        rec.stop_for_disk()
        assert rec.status() == "disk-stop"

    def test_gave_up_is_its_own_status_value(self):
        assert STATUS_GAVE_UP == "gave-up"

    def test_the_disk_latch_is_reported_before_giving_up(self, tmp_path, monkeypatch):
        first = FakeChild()
        install_failing_spawner(monkeypatch, first)
        rec = Recorder(
            make_settings(tmp_path, reconnect_seconds=0.0, retry_limit=1)
        )
        rec.start(at(0))
        first.die()
        rec.supervise(at(1))
        rec.supervise(at(1))
        assert rec.status() == STATUS_GAVE_UP

        rec.stop_for_disk()
        assert rec.status() == "disk-stop"


# ==============================================================================
# Current Clip announcement
# ==============================================================================

def clip_names_in(caplog) -> list:
    """The Clip names named by the recorded log lines, in order."""
    return [
        record.message
        for record in caplog.records
        if record.levelno == logging.INFO and "current Clip" in record.message
    ]


class TestAnnounceCurrentClip:
    def test_the_first_written_file_is_announced_once_at_information_level(
        self, tmp_path, monkeypatch, caplog
    ):
        install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())

        # Nothing yet: the recorder program needs a moment to open its file.
        caplog.set_level(logging.DEBUG)
        rec.supervise(at(1))
        assert clip_names_in(caplog) == []
        assert rec.current_clip() is None

        (tmp_path / "20240101_000100.mp4").write_text("")
        rec.supervise(at(2))

        announced = clip_names_in(caplog)
        assert len(announced) == 1
        assert "20240101_000100.mp4" in announced[0]
        assert rec.current_clip() == "20240101_000100.mp4"

    def test_the_same_name_is_never_announced_twice(self, tmp_path, monkeypatch, caplog):
        install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())
        (tmp_path / "20240101_000100.mp4").write_text("")

        caplog.set_level(logging.DEBUG)
        for second in (1, 2, 3, 4):
            rec.supervise(at(second))

        assert len(clip_names_in(caplog)) == 1

    def test_each_later_clip_is_announced_when_it_appears(self, tmp_path, monkeypatch, caplog):
        install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())

        caplog.set_level(logging.DEBUG)
        (tmp_path / "20240101_000100.mp4").write_text("")
        rec.supervise(at(1))

        # The usual clip-length rollover, or a reconnection after a drop.
        (tmp_path / "20240101_030100.mp4").write_text("")
        rec.supervise(at(2))
        rec.supervise(at(3))

        announced = clip_names_in(caplog)
        assert len(announced) == 2
        assert "20240101_000100.mp4" in announced[0]
        assert "20240101_030100.mp4" in announced[1]
        assert rec.current_clip() == "20240101_030100.mp4"

    def test_files_already_in_the_folder_are_not_announced(self, tmp_path, monkeypatch, caplog):
        (tmp_path / "20231231_235900.mp4").write_text("from an earlier run")
        install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())

        caplog.set_level(logging.DEBUG)
        rec.supervise(at(1))
        rec.supervise(at(2))

        assert clip_names_in(caplog) == []
        assert rec.current_clip() is None

        # Only the file that appears after the start counts.
        (tmp_path / "20240101_000100.mp4").write_text("")
        rec.supervise(at(3))
        assert len(clip_names_in(caplog)) == 1
        assert rec.current_clip() == "20240101_000100.mp4"

    def test_a_prefixed_recorder_ignores_other_peoples_files(
        self, tmp_path, monkeypatch, caplog
    ):
        # The Clips folder is shared: another Listed Person's file, or the
        # automator's own Clip, can appear at any moment. This recorder names
        # only the file led by its own prefix.
        install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(tmp_path, clip_name_prefix="Jevv_"))
        rec.start(at())

        caplog.set_level(logging.DEBUG)
        (tmp_path / "Qiqi_240101_000100.mp4").write_text("")
        (tmp_path / "20240101_000100.mp4").write_text("")
        rec.supervise(at(1))
        assert rec.current_clip() is None

        (tmp_path / "Jevv_240101_000200.mp4").write_text("")
        rec.supervise(at(2))
        assert rec.current_clip() == "Jevv_240101_000200.mp4"

    def test_stopping_forgets_the_name_and_a_later_start_announces_again(
        self, tmp_path, monkeypatch, caplog
    ):
        install_spawner(monkeypatch, FakeChild(), FakeChild())
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())
        (tmp_path / "20240101_000100.mp4").write_text("")

        caplog.set_level(logging.DEBUG)
        rec.supervise(at(1))
        assert rec.current_clip() == "20240101_000100.mp4"

        rec.stop()
        assert rec.current_clip() is None

        caplog.clear()
        rec.start(at(60))
        # The file from the first run is old now: no line for it.
        rec.supervise(at(61))
        assert clip_names_in(caplog) == []

        (tmp_path / "20240101_020000.mp4").write_text("")
        rec.supervise(at(62))
        announced = clip_names_in(caplog)
        assert len(announced) == 1
        assert "20240101_020000.mp4" in announced[0]

    def test_nothing_is_announced_while_stopped(self, tmp_path, monkeypatch, caplog):
        install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())
        (tmp_path / "20240101_000100.mp4").write_text("")

        caplog.set_level(logging.DEBUG)
        rec.supervise(at(1))
        rec.stop()

        caplog.clear()
        rec.supervise(at(2))
        assert clip_names_in(caplog) == []
        assert rec.current_clip() is None

    def test_a_missing_folder_is_a_debug_note_only(self, tmp_path, monkeypatch, caplog):
        folder = tmp_path / "clips"
        install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(folder))
        rec.start(at())
        folder.rmdir()

        caplog.set_level(logging.DEBUG)
        rec.supervise(at(1))  # must not raise

        assert rec.is_running() is True
        assert rec.current_clip() is None
        assert any(record.levelno == logging.DEBUG for record in caplog.records)
        assert not [
            record for record in caplog.records if record.levelno >= logging.INFO
        ]

    def test_a_folder_that_is_really_a_file_is_a_debug_note_only(
        self, tmp_path, monkeypatch, caplog
    ):
        folder = tmp_path / "clips"
        install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(folder))
        rec.start(at())
        folder.rmdir()
        folder.write_text("not a folder")

        caplog.set_level(logging.DEBUG)
        rec.supervise(at(1))  # must not raise

        assert rec.is_running() is True
        assert rec.current_clip() is None
        assert not [
            record
            for record in caplog.records
            if record.levelno >= logging.INFO and "Clip" in record.message
        ]

    def test_a_failure_inside_the_hook_is_swallowed_and_logged_at_debug(
        self, tmp_path, monkeypatch, caplog
    ):
        install_spawner(monkeypatch, FakeChild())
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())

        def exploding_newest_clip_name():
            raise RuntimeError("the folder blew up")

        monkeypatch.setattr(rec, "_newest_clip_name", exploding_newest_clip_name)

        caplog.set_level(logging.DEBUG)
        rec.supervise(at(1))  # must not raise

        assert rec.is_running() is True
        assert any(record.levelno == logging.DEBUG for record in caplog.records)


class TestChildOutput:
    """The child's own words are read, kept short, and explained on failure."""

    def test_useful_lines_are_recognised(self):
        assert is_useful_child_line("Input #0, hls, from 'http://host/x'")
        assert is_useful_child_line("  Stream #0:0: Video: h264")
        assert is_useful_child_line("Output #0, segment, to 'clip.mp4'")
        assert is_useful_child_line("Press [q] to stop, [?] for help")

    def test_chatter_is_not_useful(self):
        assert not is_useful_child_line("frame=  120 fps= 30 q=-1.0 size=  256kB")
        assert not is_useful_child_line("")
        assert not is_useful_child_line("[segment @ 0x1] Opening 'seg.ts'")

    def test_reader_logs_every_line_and_raises_the_useful_few(self, caplog):
        text = (
            b"Input #0, hls, from 'http://host/x'\n"
            b"frame=  1 fps=0.0\n"
            b"  Stream #0:0: Video: h264\n"
            b"Output #0, segment, to 'clip.mp4'\n"
            b"Press [q] to stop, [?] for help\n"
        )
        reader = ChildOutputReader(io.BytesIO(text))

        with caplog.at_level(logging.DEBUG):
            reader.run()

        # Every line is in the log; the useful ones at information, the rest
        # at debug.
        assert len(caplog.records) == 5
        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 4
        assert any("frame=" in r.getMessage() for r in caplog.records
                   if r.levelno == logging.DEBUG)

    def test_reader_keeps_only_the_last_twenty_lines(self):
        text = "".join(f"line {i}\n" for i in range(25)).encode("utf-8")
        reader = ChildOutputReader(io.BytesIO(text))
        reader.run()

        tail = reader.tail()
        assert len(tail) == CHILD_TAIL_LINES == 20
        assert tail[0] == "line 5"
        assert tail[-1] == "line 24"

    def test_reader_never_raises_on_a_broken_stream(self, caplog):
        class BrokenStream:
            def __iter__(self):
                raise RuntimeError("pipe blew up")

        reader = ChildOutputReader(BrokenStream())
        with caplog.at_level(logging.DEBUG):
            reader.run()  # must not raise
        assert any(record.levelno == logging.DEBUG for record in caplog.records)

    def test_unexpected_exit_dumps_the_tail_at_warning(self, tmp_path, monkeypatch, caplog):
        child = FakeChild(stderr=io.BytesIO(b"Input #0, hls\nsegment 1 gone\n"))
        install_spawner(monkeypatch, child)
        rec = Recorder(make_settings(tmp_path, reconnect_seconds=60.0))
        rec.start(at(0))

        child.die(exit_code=1)
        with caplog.at_level(logging.WARNING):
            rec.supervise(at(1))

        tail = [r for r in caplog.records if "before it stopped on its own" in r.getMessage()]
        assert len(tail) == 1
        assert "segment 1 gone" in tail[0].getMessage()

    def test_normal_stop_does_not_dump_the_tail(self, tmp_path, monkeypatch, caplog):
        child = FakeChild(stderr=io.BytesIO(b"Input #0, hls\nsegment 1 gone\n"))
        install_spawner(monkeypatch, child)
        rec = Recorder(make_settings(tmp_path))
        rec.start(at(0))

        with caplog.at_level(logging.WARNING):
            rec.stop()

        assert not [
            r for r in caplog.records
            if "before it stopped on its own" in r.getMessage()
        ]

    def test_a_child_without_a_stream_gets_no_reader(self, tmp_path, monkeypatch):
        install_spawner(monkeypatch, FakeChild())  # stderr is None
        rec = Recorder(make_settings(tmp_path))
        rec.start(at())
        assert rec._reader is None


class TestModuleBoundaries:
    def test_does_not_import_main_config_or_schedule(self):
        for name in ("main", "config", "schedule"):
            assert name not in recorder.__dict__
