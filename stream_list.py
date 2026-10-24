"""Read the Stream List and turn its reply into Listed Person entries.

The local bridge publishes one JSON array of whoever is streaming right now,
each item carrying a ``username`` and a ``stream`` address. This module does
two things only: fetch that text, and parse it into :class:`ListedStream`
values. It holds no state, watches no clock, starts no recording, and knows
nothing about the Clips folder or the recorder engine.

The parse half never raises for bad content. A reply that is not a list, or an
item that is missing a field, comes back as a plain-English problem string so
the caller can log it and carry on: a broken list is an ordinary outcome, not
an exception. Only :func:`fetch_stream_list_text` touches the network, and it
lets its own failure travel to the caller unchanged, because the caller owns
the logging and the decision to keep recording.
"""

import json
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Tuple

# The item keys the bridge sends and this module needs. Extra keys are ignored.
_USERNAME_KEY = "username"
_STREAM_KEY = "stream"


@dataclass(frozen=True)
class ListedStream:
    """One Listed Person as the Stream List described them.

    ``username`` is the display name exactly as sent, only trimmed; it stays
    the person's identity for the whole run. ``stream`` is the absolute address
    the recorder is pointed at. Both are never empty.
    """

    username: str
    stream: str


def fetch_stream_list_text(address: str, timeout: float) -> str:
    """Fetch one Stream List reply at *address* and return it as text.

    This is the single place a network request is made. Tests replace this
    function so the rest of the program can run without a bridge. Failures are
    deliberately not caught: a timeout or a refused connection must reach the
    caller, which logs it and keeps every recording running.
    """
    with urllib.request.urlopen(address, timeout=timeout) as reply:
        return reply.read().decode("utf-8", errors="replace")


def parse_stream_list(text: str) -> Tuple[List[ListedStream], List[str]]:
    """Parse a Stream List reply into entries plus one problem per skip.

    The reply must be a JSON array; anything else gives no entries and exactly
    one problem saying what arrived instead. Items are kept in reply order and
    trimmed. An item is skipped, with a problem naming its position, when it is
    not an object, lacks a field, holds a non-string value, or is blank. A
    duplicate username (compared without case) keeps the first entry and
    reports each later one; nothing is merged or renamed. An empty array means
    nobody is streaming and gives no problems.

    This function never raises for bad content, and it does not log: the
    problems are returned so the caller can log them at the level it chooses.
    """
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return [], [_not_a_list_problem("text that is not JSON")]

    if not isinstance(payload, list):
        return [], [_not_a_list_problem(_describe(payload))]

    entries: List[ListedStream] = []
    problems: List[str] = []
    seen_usernames = set()

    for position, item in enumerate(payload, start=1):
        entry, problem = _parse_item(item, position)
        if problem is not None:
            problems.append(problem)
            continue

        # Identity is the display name, so a repeat must not become a second
        # entry: the first one is already the person being recorded. Comparing
        # without case stops "Qiqi" and "qiqi" opening two recordings.
        username_key = entry.username.casefold()
        if username_key in seen_usernames:
            problems.append(
                "Stream List item %d was skipped: username %r is already listed."
                % (position, entry.username)
            )
            continue

        seen_usernames.add(username_key)
        entries.append(entry)

    return entries, problems


def _parse_item(
    item, position: int
) -> Tuple[Optional[ListedStream], Optional[str]]:
    """Return the entry for one reply *item*, or None plus its problem."""
    if not isinstance(item, dict):
        return None, _item_problem(position, "it is not an object")

    for key, label in ((_USERNAME_KEY, "username"), (_STREAM_KEY, "stream")):
        if key not in item:
            return None, _item_problem(position, "it has no %s" % label)

        value = item[key]
        if not isinstance(value, str):
            return None, _item_problem(position, "its %s is not text" % label)

        if not value.strip():
            return None, _item_problem(position, "its %s is blank" % label)

    return (
        ListedStream(
            username=item[_USERNAME_KEY].strip(),
            stream=item[_STREAM_KEY].strip(),
        ),
        None,
    )


def _describe(payload) -> str:
    """Name what the reply held, in words a log reader understands."""
    if payload is None:
        return "nothing (null)"
    if isinstance(payload, bool):
        return "a true/false value"
    if isinstance(payload, dict):
        return "a JSON object"
    if isinstance(payload, str):
        return "a JSON string"
    if isinstance(payload, (int, float)):
        return "a JSON number"
    return "an unexpected value"


def _not_a_list_problem(arrived: str) -> str:
    """One problem for a whole reply that is not a JSON array."""
    return "Stream List reply was not a JSON array; it was %s." % arrived


def _item_problem(position: int, reason: str) -> str:
    """One problem for a single item that had to be skipped."""
    return "Stream List item %d was skipped: %s." % (position, reason)
