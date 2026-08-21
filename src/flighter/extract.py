"""Turn an email into flight segments, cheapest tier first.

Three tiers, in order: a free keyword prefilter that throws out mail that plainly holds
no booking, schema.org structured data that airlines embed for Gmail and Outlook and
which is exact when present, and only then a model call. Every tier produces the same
`Extraction`, so the pipeline downstream never learns which one answered.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from anthropic import AsyncAnthropic
from bs4 import BeautifulSoup
from bs4.element import Tag
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from .config import Settings, get_settings
from .mail import Message

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"

# Confirmations put everything that matters in the first screenful; the rest is legal
# boilerplate and fare rules, and truncating keeps a forwarded 200-message thread from
# costing a fortune.
MAX_BODY_CHARS = 8000
# Shared by thinking and the answer, so this is not merely the size of the JSON.
MAX_TOKENS = 8000

# The SDK would otherwise wait ten minutes per attempt and attempt three times, and the
# mail loop can do nothing else while it waits. A confirmation takes seconds to read; a
# call that has not answered in a minute is not going to.
REQUEST_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 1


class ExtractionError(RuntimeError):
    """The model answered with something that is not a valid extraction."""


class Segment(BaseModel):
    """One flight. A return trip is two of these, a connection is two more."""

    # Structured outputs need a closed schema, and a field the model invented is a field
    # nothing downstream would ever read.
    model_config = ConfigDict(extra="forbid")

    marketing_carrier: str
    marketing_number: str
    operating_carrier: str | None
    operating_number: str | None
    origin_iata: str
    dest_iata: str
    departure_local: str
    arrival_local: str | None
    confirmation_code: str | None
    seat: str | None

    @field_validator("marketing_carrier", "operating_carrier", "origin_iata", "dest_iata")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else value

    @property
    def departure_at(self) -> datetime | None:
        return _wall_clock(self.departure_local)

    @property
    def arrival_at(self) -> datetime | None:
        return _wall_clock(self.arrival_local)


class Extraction(BaseModel):
    """Whether the email confirms flights, and every segment it names if so."""

    model_config = ConfigDict(extra="forbid")

    is_flight_confirmation: bool
    # Unbounded in the schema: structured outputs reject numeric minimum/maximum, so the
    # range is enforced here rather than declared there.
    confidence: float
    segments: list[Segment]

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return min(max(value, 0.0), 1.0)


# -- tier 1: the free prefilter ------------------------------------------------------

# Phrases specific enough that a marketing email is unlikely to carry one. "flight" and
# "book" are deliberately absent: every fare sale in the world contains both.
_BOOKING_PHRASES = (
    "boarding pass",
    "your trip",
    "your itinerary",
    "itinerary",
    "confirmation",
    "confirmed",
    "e-ticket",
    "eticket",
    "electronic ticket",
    "booking reference",
    "record locator",
    "reservation code",
    "check in for your flight",
    "check-in is now open",
    "flightreservation",
    "pnr",
)
# JFK to LAX, JFK-LAX, JFK -> LAX.
_ROUTE = re.compile(r"\b([A-Z]{3})\s*(?:-{1,2}|to|>|\u2013|\u2192)\s*([A-Z]{3})\b")
# Two airport codes in the parenthesised form confirmations use: "New York (JFK)".
_PAREN_CODE = re.compile(r"\(([A-Z]{3})\)")
# DL1234, DL 1234, LH 400. Two letters or a letter/digit pair, as IATA allows.
_FLIGHT_NUMBER = re.compile(r"\b(?:[A-Z]{2}|[A-Z]\d|\d[A-Z])\s?\d{1,4}\b")


def looks_like_flight(message: Message) -> bool:
    """A coarse, free guess at whether this email is worth a model call.

    Tuned to over-accept. A false negative loses a flight for good; a false positive
    costs one model call, which is what the pipeline would do for every email anyway.
    """
    text = "\n".join([message.subject, message.text_plain, message.text_html])
    lowered = text.lower()

    if any(phrase in lowered for phrase in _BOOKING_PHRASES):
        return True
    if _ROUTE.search(text):
        return True
    if len(set(_PAREN_CODE.findall(text))) >= 2:
        return True
    return "flight" in lowered and bool(_FLIGHT_NUMBER.search(text))


# -- tier 2: schema.org --------------------------------------------------------------

_FLIGHT_RESERVATION = "FlightReservation"
# Airlines nest a multi-leg itinerary in one of these; each leg is its own reservation.
_NESTING_KEYS = ("@graph", "subReservation", "itemListElement", "reservationFor")


def from_jsonld(html: str) -> Extraction | None:
    """Read schema.org FlightReservations out of the HTML part.

    Airlines embed these because Gmail and Outlook consume them, so when one is present
    it is the airline's own statement of the booking rather than a guess: confidence 1.0.
    """
    if not html.strip():
        return None
    soup = BeautifulSoup(html, "html.parser")

    reservations = _reservations(_jsonld_blocks(soup))
    if not reservations:
        reservations = _reservations(_microdata_blocks(soup))
    if not reservations:
        return None

    segments = [segment for node in reservations if (segment := _segment_from(node)) is not None]
    if not segments:
        log.debug("found %d FlightReservation node(s) but none were complete", len(reservations))
        return None

    return Extraction(is_flight_confirmation=True, confidence=1.0, segments=segments)


def _jsonld_blocks(soup: BeautifulSoup) -> list[Any]:
    blocks: list[Any] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError:
            log.debug("skipping malformed JSON-LD block")
    return blocks


def _microdata_blocks(soup: BeautifulSoup) -> list[Any]:
    """The same graph expressed as itemscope/itemprop attributes, as Gmail also accepts."""
    return [
        _microdata_object(element)
        for element in soup.find_all(itemscope=True)
        if element.find_parent(itemscope=True) is None
    ]


def _microdata_object(element: Tag) -> dict[str, Any]:
    obj: dict[str, Any] = {"@type": _type_name(element.get("itemtype"))}
    for child in element.find_all(itemprop=True):
        if child.find_parent(itemscope=True) is not element:
            continue
        name = str(child.get("itemprop"))
        value: Any = (
            _microdata_object(child) if child.has_attr("itemscope") else _microdata_value(child)
        )
        # Repeated properties are a list in the schema; a package's legs arrive this way.
        if name in obj:
            existing = obj[name]
            obj[name] = [*existing, value] if isinstance(existing, list) else [existing, value]
        else:
            obj[name] = value
    return obj


def _microdata_value(element: Tag) -> str:
    for attribute in ("content", "datetime", "href", "src"):
        value = element.get(attribute)
        if value:
            return str(value)
    return element.get_text(strip=True)


def _type_name(itemtype: Any) -> str:
    return str(itemtype or "").rstrip("/").rsplit("/", 1)[-1]


def _reservations(blocks: list[Any]) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    for block in blocks:
        for node in _walk(block):
            if _type_name(node.get("@type")) == _FLIGHT_RESERVATION and node not in seen:
                seen.append(node)
    return seen


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    """Every object in the graph, however the sender chose to nest it."""
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for key in _NESTING_KEYS:
            if key in node:
                yield from _walk(node[key])


def _segment_from(reservation: dict[str, Any]) -> Segment | None:
    flight = reservation.get("reservationFor")
    if isinstance(flight, list):
        flight = flight[0] if flight else None
    if not isinstance(flight, dict):
        return None

    airline = _obj(flight.get("airline"))
    carrier, number = _split_flight_number(
        str(airline.get("iataCode") or ""), str(flight.get("flightNumber") or "")
    )
    origin = _iata(flight.get("departureAirport"))
    dest = _iata(flight.get("arrivalAirport"))
    departure = _naive_iso(flight.get("departureTime"))

    if not (carrier and number and origin and dest and departure):
        return None

    operator = _obj(flight.get("operatedBy"))
    seat = _obj(_obj(reservation.get("reservedTicket")).get("ticketedSeat")).get("seatNumber")

    return Segment(
        marketing_carrier=carrier,
        marketing_number=number,
        operating_carrier=str(operator.get("iataCode")) if operator.get("iataCode") else None,
        operating_number=None,
        origin_iata=origin,
        dest_iata=dest,
        departure_local=departure,
        arrival_local=_naive_iso(flight.get("arrivalTime")),
        confirmation_code=_text(reservation.get("reservationNumber")),
        seat=_text(seat),
    )


def _obj(value: Any) -> dict[str, Any]:
    """Schema.org properties are objects, bare strings, or absent; normalise to a dict."""
    return value if isinstance(value, dict) else {}


def _iata(airport: Any) -> str | None:
    if isinstance(airport, dict):
        return _text(airport.get("iataCode"))
    return _text(airport)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _split_flight_number(carrier: str, flight_number: str) -> tuple[str, str]:
    """`("NH", "NH106")` and `("", "NH106")` both mean NH 106."""
    flight_number = flight_number.strip().upper().replace(" ", "")
    carrier = carrier.strip().upper()
    if carrier and flight_number.startswith(carrier):
        return carrier, flight_number[len(carrier) :].lstrip("0") or flight_number[len(carrier) :]
    match = re.fullmatch(r"([A-Z0-9]{2,3}?)(\d{1,4})", flight_number)
    if match:
        return carrier or match.group(1), match.group(2)
    return carrier, flight_number


def _naive_iso(value: Any) -> str | None:
    """Drop any offset the sender stated and keep the wall-clock reading.

    Airlines put the wrong offset in these fields often enough that the only safe
    reading is "this is the time on the clock at that airport"; the zone is resolved
    downstream from the airport itself.
    """
    parsed = _wall_clock(_text(value))
    return parsed.isoformat() if parsed else None


def _wall_clock(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except ValueError:
        log.debug("unparseable timestamp %r", value)
        return None


# -- tier 3: the model ---------------------------------------------------------------

SYSTEM_PROMPT = """\
You read one email and decide whether it confirms flights the recipient is booked on. \
If it does, you extract every segment.

Set is_flight_confirmation true only for a booking confirmation, e-ticket, itinerary, \
boarding pass, check-in notice, or a change or cancellation of one. A fare sale, a \
price alert, a route announcement, a loyalty statement, and a travel newsletter are all \
false, however many airport codes and dates they contain.

Emit one segment per flight. A return trip is two segments. A connection is two \
segments. Never merge legs.

Times are local wall-clock time at their own airport, written as naive ISO 8601 with no \
offset and no zone suffix: 2026-09-12T18:40:00. If the email states an offset or a \
timezone, ignore it entirely and copy the clock time as printed. We resolve zones from \
the airport codes ourselves, which is right more often than the airline is.

Relative dates ("tomorrow", "next Tuesday") resolve against the email's sent date, \
which is given to you below.

marketing_carrier is the two-character IATA code on the ticket ("DL") and \
marketing_number is the digits alone ("1234"). Fill operating_carrier and \
operating_number only when the email names a different airline actually flying the leg.

Use null for anything the email does not state. Never guess an airport code, a seat, or \
a time. confidence is your certainty that these specific segments, with these specific \
times, are real and correctly read: lower it when times or codes are inferred rather \
than printed.
"""


def body_text(message: Message) -> str:
    """The readable body: the HTML part's visible text, or the plain part without one.

    The HTML part is the email the person read. The plain part beside it is whatever the
    sender's mail platform generated from it, which can mean a thousand-character tracking
    link inlined behind every word and every layout variant rendered in turn, so that the
    first screenful is links and the flights sit far below the cut.
    """
    if message.text_html:
        visible = BeautifulSoup(message.text_html, "html.parser").get_text("\n", strip=True)
        if visible:
            return visible
    return message.text_plain


def render(message: Message) -> str:
    """The email as the model sees it. The sent date anchors every relative date in it."""
    sent = message.date.isoformat() if message.date else "unknown"
    body = body_text(message)[:MAX_BODY_CHARS]
    return "\n".join(
        [
            "<email>",
            f"From: {message.from_addr}",
            f"Subject: {message.subject}",
            f"Sent: {sent}",
            "",
            "<body>",
            body if body else "(no readable text body)",
            "</body>",
            "</email>",
        ]
    )


async def from_model(
    message: Message,
    *,
    settings: Settings | None = None,
    client: AsyncAnthropic | None = None,
) -> Extraction | None:
    """The paid fallback, for the airlines that embed no structured data.

    Raises `ExtractionError` when the model answered but not with an extraction: it
    refused, it ran out of room, or what it wrote does not fit the schema.
    """
    settings = settings or get_settings()
    if not body_text(message):
        log.info("%s has no readable body; nothing to extract from", message.id)
        return None
    if client is None:
        if not settings.anthropic_api_key:
            log.warning("no Anthropic key configured; cannot extract %s", message.id)
            return None
        client = AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=MAX_RETRIES,
        )

    try:
        # A closed JSON schema rather than a "reply with JSON" instruction: the response
        # is a booking, and a prose apology wrapped around it is not recoverable.
        response = await client.messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            output_format=Extraction,
            messages=[{"role": "user", "content": render(message)}],
        )
    except ValidationError as exc:
        if any(error["type"] == "json_invalid" for error in exc.errors()):
            raise ExtractionError(
                f"model output is not complete JSON; it was most likely cut off at "
                f"{MAX_TOKENS} tokens: {exc}"
            ) from exc
        raise ExtractionError(f"model output did not match the extraction schema: {exc}") from exc

    log.info(
        "%s: %s used %d input and %d output tokens",
        message.id,
        MODEL,
        response.usage.input_tokens,
        response.usage.output_tokens,
    )
    if response.stop_reason == "refusal":
        raise ExtractionError("model refused to read the email")
    if response.stop_reason == "max_tokens":
        raise ExtractionError(f"model output was cut off at {MAX_TOKENS} tokens")
    extraction = response.parsed_output
    if extraction is None:
        raise ExtractionError(f"model returned no text (stop={response.stop_reason})")
    return extraction
