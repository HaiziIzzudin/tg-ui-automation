"""Tests for the Stream List client.

Every reply is hardcoded text and no real network is touched: the one place
that would open a connection (the request layer, and once the fetch function
itself) is replaced by a fake. ``parse_stream_list`` is fed strings directly,
so the bulk of these tests never look at the network at all.
"""
import io
import sys
import urllib.request
from pathlib import Path

import pytest

# Add the parent directory to sys.path so we can import stream_list
sys.path.insert(0, str(Path(__file__).parent.parent))

import stream_list
from stream_list import ListedStream, parse_stream_list

LIST_ADDRESS = "http://127.0.0.1:8479/"

# One good reply, in the shape the bridge sends. The names contain spaces and
# non-Latin script on purpose: both must survive untouched.
GOOD_REPLY = """
[
  {"username": "Jevv", "stream": "http://127.0.0.1:8479/668453130/camera.m3u8"},
  {"username": "Alif Naim", "stream": "http://127.0.0.1:8479/8198214155/camera.m3u8"},
  {"username": "محمد", "stream": "http://127.0.0.1:8479/1670812362/camera.m3u8"}
]
"""


def entries_of(text: str) -> list[ListedStream]:
    """Parse *text* and return only the entries, failing on any problem."""
    entries, problems = parse_stream_list(text)
    assert problems == [], f"unexpected problems: {problems}"
    return entries


# ---------------------------------------------------------------------------
# A good reply
# ---------------------------------------------------------------------------

def test_good_reply_parses_in_order():
    entries = entries_of(GOOD_REPLY)

    assert entries == [
        ListedStream("Jevv", "http://127.0.0.1:8479/668453130/camera.m3u8"),
        ListedStream("Alif Naim", "http://127.0.0.1:8479/8198214155/camera.m3u8"),
        ListedStream("محمد", "http://127.0.0.1:8479/1670812362/camera.m3u8"),
    ]


def test_values_are_trimmed():
    entries = entries_of(
        '[{"username": "  Alif Naim\\t", "stream": "  http://example.test/a.m3u8  "}]'
    )

    assert entries == [ListedStream("Alif Naim", "http://example.test/a.m3u8")]


def test_empty_array_is_nobody_streaming():
    assert parse_stream_list("[]") == ([], [])


def test_entry_is_frozen():
    """The caller keeps these as identity; nothing may rewrite one in place."""
    entry = ListedStream("Qiqi", "http://example.test/a.m3u8")

    with pytest.raises(Exception):
        entry.username = "Other"


# ---------------------------------------------------------------------------
# Replies that are not a list at all
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, arrived",
    [
        ('{"username": "Jevv", "stream": "http://example.test/a.m3u8"}', "object"),
        ('"just a string"', "string"),
        ("42", "number"),
        ("null", "null"),
        ("", "not JSON"),
        ("<html>oops</html>", "not JSON"),
    ],
)
def test_non_array_reply_gives_no_entries_and_one_problem(text, arrived):
    entries, problems = parse_stream_list(text)

    assert entries == []
    assert len(problems) == 1
    assert arrived in problems[0], f"{arrived!r} missing from {problems[0]!r}"


# ---------------------------------------------------------------------------
# Broken items
# ---------------------------------------------------------------------------

def test_item_missing_username_is_skipped_alone():
    entries, problems = parse_stream_list(
        """
        [
          {"stream": "http://example.test/a.m3u8"},
          {"username": "noww", "stream": "http://example.test/b.m3u8"}
        ]
        """
    )

    assert entries == [ListedStream("noww", "http://example.test/b.m3u8")]
    assert len(problems) == 1
    assert "1" in problems[0]


def test_item_missing_stream_is_skipped_alone():
    entries, problems = parse_stream_list(
        """
        [
          {"username": "Qiqi"},
          {"username": "noww", "stream": "http://example.test/b.m3u8"}
        ]
        """
    )

    assert entries == [ListedStream("noww", "http://example.test/b.m3u8")]
    assert len(problems) == 1
    assert "1" in problems[0]


@pytest.mark.parametrize(
    "bad_field",
    ['{"username": 7, "stream": "http://example.test/a.m3u8"}',
     '{"username": "Qiqi", "stream": null}',
     '{"username": ["Qiqi"], "stream": "http://example.test/a.m3u8"}'],
)
def test_item_with_non_string_value_is_skipped(bad_field):
    entries, problems = parse_stream_list(
        "[%s, {\"username\": \"noww\", \"stream\": \"http://example.test/b.m3u8\"}]"
        % bad_field
    )

    assert entries == [ListedStream("noww", "http://example.test/b.m3u8")]
    assert len(problems) == 1
    assert "1" in problems[0]


@pytest.mark.parametrize(
    "bad_field",
    ['{"username": "   ", "stream": "http://example.test/a.m3u8"}',
     '{"username": "Qiqi", "stream": "\\t "}'],
)
def test_item_with_blank_text_is_skipped(bad_field):
    entries, problems = parse_stream_list(
        '[%s, {"username": "noww", "stream": "http://example.test/b.m3u8"}]'
        % bad_field
    )

    assert entries == [ListedStream("noww", "http://example.test/b.m3u8")]
    assert len(problems) == 1
    assert "1" in problems[0]


def test_item_that_is_not_an_object_is_skipped():
    entries, problems = parse_stream_list(
        """
        [
          "Qiqi",
          null,
          {"username": "noww", "stream": "http://example.test/b.m3u8"}
        ]
        """
    )

    assert entries == [ListedStream("noww", "http://example.test/b.m3u8")]
    assert len(problems) == 2
    assert "1" in problems[0]
    assert "2" in problems[1]


def test_every_broken_item_is_reported_in_position_order():
    entries, problems = parse_stream_list(
        """
        [
          {},
          {"username": "Qiqi"},
          {"username": "noww", "stream": "http://example.test/b.m3u8"}
        ]
        """
    )

    assert entries == [ListedStream("noww", "http://example.test/b.m3u8")]
    assert len(problems) == 2
    assert "1" in problems[0]
    assert "2" in problems[1]


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

def test_duplicate_name_keeps_first_and_reports_the_rest():
    entries, problems = parse_stream_list(
        """
        [
          {"username": "Qiqi", "stream": "http://example.test/first.m3u8"},
          {"username": "Qiqi", "stream": "http://example.test/second.m3u8"}
        ]
        """
    )

    assert entries == [ListedStream("Qiqi", "http://example.test/first.m3u8")]
    assert len(problems) == 1
    assert "2" in problems[0]


def test_duplicate_name_is_compared_without_case():
    entries, problems = parse_stream_list(
        """
        [
          {"username": "Qiqi", "stream": "http://example.test/first.m3u8"},
          {"username": "qiqi", "stream": "http://example.test/second.m3u8"},
          {"username": "QI QI", "stream": "http://example.test/third.m3u8"}
        ]
        """
    )

    assert [entry.username for entry in entries] == ["Qiqi", "QI QI"]
    assert len(problems) == 1
    assert "2" in problems[0]


# ---------------------------------------------------------------------------
# Extra keys
# ---------------------------------------------------------------------------

def test_unknown_extra_keys_are_ignored():
    entries = entries_of(
        """
        [
          {
            "username": "Jevv",
            "stream": "http://example.test/a.m3u8",
            "viewers": 12,
            "extra": {"nested": true}
          }
        ]
        """
    )

    assert entries == [ListedStream("Jevv", "http://example.test/a.m3u8")]


# ---------------------------------------------------------------------------
# The one place a request happens
# ---------------------------------------------------------------------------

def test_fetch_uses_the_given_timeout_and_decodes_the_reply(monkeypatch):
    seen = {}

    def fake_urlopen(address, timeout):
        seen["address"] = address
        seen["timeout"] = timeout
        return io.BytesIO(GOOD_REPLY.encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    text = stream_list.fetch_stream_list_text(LIST_ADDRESS, 2.5)

    assert text == GOOD_REPLY
    assert seen == {"address": LIST_ADDRESS, "timeout": 2.5}


def test_fetch_failure_reaches_the_caller(monkeypatch):
    """A dead bridge must be the caller's problem to log, never swallowed here."""

    def refusing_urlopen(address, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", refusing_urlopen)

    with pytest.raises(TimeoutError):
        stream_list.fetch_stream_list_text(LIST_ADDRESS, 0.5)


def test_replaced_fetch_reply_flows_into_the_parser(monkeypatch):
    """The seam the program uses: fetch the text once, then parse it."""
    monkeypatch.setattr(
        stream_list, "fetch_stream_list_text", lambda address, timeout: GOOD_REPLY
    )

    text = stream_list.fetch_stream_list_text(LIST_ADDRESS, 1.0)
    entries, problems = parse_stream_list(text)

    assert problems == []
    assert [entry.username for entry in entries] == ["Jevv", "Alif Naim", "محمد"]
