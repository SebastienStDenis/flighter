"""What the service says about a flagged email that came to nothing.

The phone and the email page tell a person the same thing about the same email, so both
take their words from here rather than each writing its own. The headline is the same
sentence every time, because a person reading a lock screen wants to know what happened
before they are told which email it happened to; which failure it was, and which email,
are the lines underneath. The page has the subject at the top of the card already, so it
asks for the reason on its own and lets the card say the rest.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

HEADLINE = "Email could not be imported"

# Said after whatever went wrong: the email has not moved, so it can still be found and
# dealt with where the person left it.
STILL_FLAGGED = "The email is still flagged in Mail."

# Stands in for a reason on a row old enough, or odd enough, to have been written without
# one. Nothing the pipeline records today gets here.
UNEXPLAINED = "No reason was recorded."

NO_FLIGHT = "No flight booking was found in this email."
UNREADABLE = "This looks like a flight email, but no flight details could be read from it."


def unknown_airport(iata: str) -> str:
    """The one failure that names the thing to correct: a code that is not an airport."""
    return f"{iata} is not a recognised airport code."


def unpublished(flights: Sequence[str]) -> str:
    """The flights an email named that no airline flies, and so nothing of it was booked."""
    named = flights[0] if len(flights) == 1 else f"{', '.join(flights[:-1])} and {flights[-1]}"
    verb = "is" if len(flights) == 1 else "are"
    return f"{named} {verb} not on any airline's schedule for that day."


class Notice(NamedTuple):
    """A headline to read at a glance, and the lines under it.

    `lines` rather than one string so that a page can put each on its own row and a push
    can join them with newlines, without either deciding what the words are.
    """

    headline: str
    lines: tuple[str, ...]

    @property
    def body(self) -> str:
        return "\n".join(self.lines)


def import_failed(*, subject: str | None, reason: str | None) -> Notice:
    """What became of an email the service has stopped trying to read.

    The email is named first and explained second, because the person is being told
    about one email among the several they flagged this morning.
    """
    named = f"Subject: {subject.strip()}" if subject and subject.strip() else None
    said = f"{sentence(reason)} {STILL_FLAGGED}"
    return Notice(HEADLINE, (named, said) if named else (said,))


def sentence(reason: str | None) -> str:
    """What went wrong, on its own.

    The page shows this much and no more: the buttons beside it are the whole of what
    to do about the email, so a sentence telling a person where to find it in Mail is
    one they have already been given a shorter answer to. A push has no buttons, which
    is why it adds one.
    """
    return _sentence(reason) if reason else UNEXPLAINED


def _sentence(text: str) -> str:
    """A reason reads as a sentence even when it came from an exception message.

    Only the first letter is touched, and only where it is lower case: an exception says
    "the model timed out", and a reason of ours starts with a code like JFK that is not
    ours to re-case.
    """
    text = text.strip()
    if text[:1].islower():
        text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else f"{text}."
