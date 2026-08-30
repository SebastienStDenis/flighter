"""The three extraction tiers, none of which is allowed to touch the network."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from flighter import extract
from flighter.config import Settings
from flighter.extract import (
    MAX_RETRIES,
    MODEL,
    REQUEST_TIMEOUT_SECONDS,
    ConfirmationCode,
    Extraction,
    ExtractionError,
    from_jsonld,
    from_model,
    looks_like_flight,
    render,
)
from flighter.mail import Message, parse_message

FIXTURES = Path(__file__).parent / "fixtures"

BARE_NUMBER_MICRODATA = """
<html><body>
<div itemscope itemtype="http://schema.org/FlightReservation">
  <meta itemprop="reservationNumber" content="CIDLT7"/>
  <meta itemprop="reservationNumber" content="H-GJIZNZ"/>
  <div itemprop="reservationFor" itemscope itemtype="http://schema.org/Flight">
    <meta itemprop="flightNumber" content="161"/>
    <div itemprop="airline" itemscope itemtype="http://schema.org/Airline">
      <meta itemprop="iataCode" content="CI"/>
    </div>
    <div itemprop="departureAirport" itemscope itemtype="http://schema.org/Airport">
      <meta itemprop="iataCode" content="ICN"/>
    </div>
    <meta itemprop="departureTime" content="2026-11-11T12:30:00+09:00"/>
    <div itemprop="arrivalAirport" itemscope itemtype="http://schema.org/Airport">
      <meta itemprop="iataCode" content="TPE"/>
    </div>
    <meta itemprop="arrivalTime" content="2026-11-11T14:20:00+08:00"/>
  </div>
</div>
</body></html>
"""

MICRODATA = """
<html><body>
<div itemscope itemtype="http://schema.org/FlightReservation">
  <meta itemprop="reservationNumber" content="ZZ9K1P"/>
  <div itemprop="underName" itemscope itemtype="http://schema.org/Person">
    <meta itemprop="name" content="Sebastien St-Denis"/>
  </div>
  <div itemprop="reservationFor" itemscope itemtype="http://schema.org/Flight">
    <meta itemprop="flightNumber" content="UA47"/>
    <div itemprop="airline" itemscope itemtype="http://schema.org/Airline">
      <meta itemprop="iataCode" content="UA"/>
    </div>
    <div itemprop="departureAirport" itemscope itemtype="http://schema.org/Airport">
      <meta itemprop="iataCode" content="SFO"/>
    </div>
    <meta itemprop="departureTime" content="2026-12-01T08:15:00-08:00"/>
    <div itemprop="arrivalAirport" itemscope itemtype="http://schema.org/Airport">
      <meta itemprop="iataCode" content="EWR"/>
    </div>
    <meta itemprop="arrivalTime" content="2026-12-01T16:50:00-05:00"/>
  </div>
</div>
</body></html>
"""

MODEL_ANSWER = """
{"is_flight_confirmation": true, "confidence": 0.92,
 "segments": [{"marketing_carrier": "WS", "marketing_number": "1502",
 "operating_carrier": null, "operating_number": null, "origin_iata": "YYC",
 "dest_iata": "YVR", "departure_local": "2026-11-17T06:30:00",
 "arrival_local": "2026-11-17T07:12:00",
 "confirmations": [{"code": "8HTGRX", "name": null}], "seat": "12A"}]}
"""

TWO_REFERENCE_ANSWER = """
{"is_flight_confirmation": true, "confidence": 0.92,
 "segments": [{"marketing_carrier": "WS", "marketing_number": "1502",
 "operating_carrier": null, "operating_number": null, "origin_iata": "YYC",
 "dest_iata": "YVR", "departure_local": "2026-11-17T06:30:00",
 "arrival_local": "2026-11-17T07:12:00",
 "confirmations": [{"code": "8HTGRX", "name": "WestJet"},
 {"code": "1094427718", "name": "Expedia"}, {"code": "QQ4R5T", "name": null}],
 "seat": "12A"}]}
"""


def message(name: str) -> Message:
    return parse_message((FIXTURES / name).read_bytes(), name)


class FakeMessages:
    """Stands in for `client.messages`; records the request so the prompt is testable.

    Validates the answer against the requested type inside the call, the way the SDK's
    `parse` does, so a bad answer surfaces where the real one would.
    """

    def __init__(self, answer: str | None, stop_reason: str) -> None:
        self.answer = answer
        self.stop_reason = stop_reason
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        parsed = None
        if self.answer is not None:
            parsed = kwargs["output_format"].model_validate_json(self.answer)
        return FakeResponse(parsed, self.stop_reason)


class FakeResponse:
    def __init__(self, parsed: Any, stop_reason: str) -> None:
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.usage = FakeUsage()


class FakeUsage:
    input_tokens = 1200
    output_tokens = 180


class FakeAnthropic:
    def __init__(self, answer: str | None, stop_reason: str = "end_turn") -> None:
        self.messages = FakeMessages(answer, stop_reason)


# -- the prefilter -------------------------------------------------------------------


def test_prefilter_accepts_a_structured_confirmation() -> None:
    assert looks_like_flight(message("flight_jsonld.eml"))


def test_prefilter_accepts_a_plain_text_confirmation() -> None:
    assert looks_like_flight(message("flight_plain.eml"))


def test_prefilter_rejects_marketing() -> None:
    assert not looks_like_flight(message("airline_promo.eml"))


def test_prefilter_accepts_on_a_bare_route_alone() -> None:
    """No booking vocabulary at all, but a route is enough to be worth a model call."""
    bare = Message(
        id="bare",
        subject="Re: next week",
        from_addr="a@example.com",
        date=None,
        text_plain="I land on the JFK - LAX leg at nine.",
    )
    assert looks_like_flight(bare)


# -- schema.org ----------------------------------------------------------------------


def test_jsonld_is_read_exactly() -> None:
    extraction = from_jsonld(message("flight_jsonld.eml").text_html)
    assert extraction is not None
    assert extraction.is_flight_confirmation
    assert extraction.confidence == 1.0

    (segment,) = extraction.segments
    assert segment.marketing_carrier == "DL"
    assert segment.marketing_number == "1234"
    assert segment.origin_iata == "JFK"
    assert segment.dest_iata == "LAX"
    assert segment.confirmations == [ConfirmationCode(code="K7QX2M", name=None)]
    assert segment.seat == "14C"


def test_jsonld_keeps_the_wall_clock_and_drops_the_offset() -> None:
    """The email says 18:40-04:00; only the 18:40 is ours to keep."""
    extraction = from_jsonld(message("flight_jsonld.eml").text_html)
    assert extraction is not None
    (segment,) = extraction.segments
    assert segment.departure_local == "2026-09-12T18:40:00"
    assert segment.departure_at == datetime(2026, 9, 12, 18, 40)
    assert segment.departure_at is not None and segment.departure_at.tzinfo is None
    assert segment.arrival_at == datetime(2026, 9, 12, 22, 5)


def test_reservation_package_yields_one_segment_per_leg() -> None:
    extraction = from_jsonld(message("flight_package_jsonld.eml").text_html)
    assert extraction is not None
    assert [
        (s.marketing_carrier, s.marketing_number, s.origin_iata, s.dest_iata)
        for s in extraction.segments
    ] == [
        ("AC", "8830", "YUL", "YYZ"),
        ("AC", "856", "YYZ", "LHR"),
    ]
    # An overnight leg lands on the next calendar day; nothing here may "fix" that.
    assert extraction.segments[1].arrival_at == datetime(2026, 10, 4, 9, 5)


def test_microdata_is_read_like_jsonld() -> None:
    extraction = from_jsonld(MICRODATA)
    assert extraction is not None
    (segment,) = extraction.segments
    assert (segment.marketing_carrier, segment.marketing_number) == ("UA", "47")
    assert (segment.origin_iata, segment.dest_iata) == ("SFO", "EWR")
    assert segment.departure_local == "2026-12-01T08:15:00"


def test_a_number_stated_without_its_designator_keeps_all_its_digits() -> None:
    """`airline` carries the code and `flightNumber` only the digits: CI 161, not CI 1."""
    extraction = from_jsonld(BARE_NUMBER_MICRODATA)
    assert extraction is not None
    (segment,) = extraction.segments
    assert (segment.marketing_carrier, segment.marketing_number) == ("CI", "161")
    assert (segment.origin_iata, segment.dest_iata) == ("ICN", "TPE")


def test_a_reservation_number_stated_twice_is_two_codes_not_a_printed_list() -> None:
    extraction = from_jsonld(BARE_NUMBER_MICRODATA)
    assert extraction is not None
    (segment,) = extraction.segments
    assert [(c.code, c.name) for c in segment.confirmations] == [
        ("CIDLT7", None),
        ("H-GJIZNZ", None),
    ]


@pytest.mark.parametrize(
    ("carrier", "stated", "expected"),
    [
        ("CI", "161", ("CI", "161")),
        ("CI", "CI161", ("CI", "161")),
        ("CI", "CI 161", ("CI", "161")),
        ("CI", "0161", ("CI", "161")),
        ("CI", "CI0161", ("CI", "161")),
        ("", "NH106", ("NH", "106")),
        ("", "ACA871", ("ACA", "871")),
        ("W6", "3312", ("W6", "3312")),
        # Nothing to split: the segment is dropped for want of a carrier rather than
        # having one invented out of the digits.
        ("", "161", ("", "161")),
    ],
)
def test_every_way_a_sender_states_a_flight_number(
    carrier: str, stated: str, expected: tuple[str, str]
) -> None:
    assert extract._split_flight_number(carrier, stated) == expected


def test_html_without_a_reservation_yields_nothing() -> None:
    assert from_jsonld("<html><body><p>Fare sale</p></body></html>") is None
    assert from_jsonld("") is None


def test_plain_text_confirmation_has_no_structured_data() -> None:
    """The fixture that forces the model path; if this ever passes, the test is wrong."""
    assert from_jsonld(message("flight_plain.eml").text_html) is None


# -- the model -----------------------------------------------------------------------


async def test_model_answer_is_validated_into_an_extraction(settings: Settings) -> None:
    client = FakeAnthropic(MODEL_ANSWER)
    extraction = await from_model(
        message("flight_plain.eml"),
        settings=settings,
        client=client,  # type: ignore[arg-type]
    )
    assert isinstance(extraction, Extraction)
    (segment,) = extraction.segments
    assert segment.marketing_carrier == "WS"
    assert segment.departure_at == datetime(2026, 11, 17, 6, 30)
    assert extraction.confidence == pytest.approx(0.92)


async def test_a_leg_booked_under_two_references_keeps_both(settings: Settings) -> None:
    """An agency booking prints its own number beside the airline's, and each is labelled
    where it is printed. A third code the email named nothing keeps no name at all."""
    client = FakeAnthropic(TWO_REFERENCE_ANSWER)
    extraction = await from_model(
        message("flight_plain.eml"),
        settings=settings,
        client=client,  # type: ignore[arg-type]
    )
    assert extraction is not None
    (segment,) = extraction.segments
    assert segment.confirmations == [
        ConfirmationCode(code="8HTGRX", name="WestJet"),
        ConfirmationCode(code="1094427718", name="Expedia"),
        ConfirmationCode(code="QQ4R5T", name=None),
    ]


async def test_model_request_pins_the_schema_and_the_model(settings: Settings) -> None:
    client = FakeAnthropic(MODEL_ANSWER)
    await from_model(
        message("flight_plain.eml"),
        settings=settings,
        client=client,  # type: ignore[arg-type]
    )
    (request,) = client.messages.calls
    assert request["model"] == MODEL
    assert request["output_format"] is Extraction


async def test_the_client_is_built_with_a_bounded_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung request must not hold the mail loop for the SDK's default of ten minutes."""
    built: list[dict[str, Any]] = []

    def build(**kwargs: Any) -> FakeAnthropic:
        built.append(kwargs)
        return FakeAnthropic(MODEL_ANSWER)

    monkeypatch.setattr(extract, "AsyncAnthropic", build)
    settings = Settings(anthropic_api_key="sk-test")

    assert await from_model(message("flight_plain.eml"), settings=settings) is not None
    (kwargs,) = built
    assert kwargs["timeout"] == REQUEST_TIMEOUT_SECONDS
    assert kwargs["max_retries"] == MAX_RETRIES


def test_schema_carries_no_constraints_structured_outputs_reject() -> None:
    """Numeric bounds and string limits are rejected by the API with a 400, not ignored."""
    rejected = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
    rejected |= {"minLength", "maxLength", "minItems", "maxItems"}
    assert not rejected & _keys(Extraction.model_json_schema())


def _keys(node: object) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {key for value in node.values() for key in _keys(value)}
    if isinstance(node, list):
        return {key for item in node for key in _keys(item)}
    return set()


def test_confidence_outside_the_unit_interval_is_clamped_rather_than_refused() -> None:
    assert Extraction(is_flight_confirmation=True, confidence=1.4, segments=[]).confidence == 1.0
    assert Extraction(is_flight_confirmation=True, confidence=-0.2, segments=[]).confidence == 0.0


async def test_prompt_carries_the_sent_date_so_relative_dates_resolve(
    settings: Settings,
) -> None:
    rendered = render(message("flight_plain.eml"))
    assert "2026-08-21T14:22:10-04:00" in rendered
    assert "8HTGRX" in rendered


async def test_malformed_model_output_is_an_error_not_a_booking(settings: Settings) -> None:
    client = FakeAnthropic("Sorry, I could not read that email.")
    with pytest.raises(ExtractionError):
        await from_model(
            message("flight_plain.eml"),
            settings=settings,
            client=client,  # type: ignore[arg-type]
        )


async def test_model_output_missing_a_required_field_is_an_error(settings: Settings) -> None:
    client = FakeAnthropic('{"is_flight_confirmation": true, "segments": []}')
    with pytest.raises(ExtractionError, match="did not match"):
        await from_model(
            message("flight_plain.eml"),
            settings=settings,
            client=client,  # type: ignore[arg-type]
        )


async def test_an_answer_cut_off_mid_json_says_so(settings: Settings) -> None:
    client = FakeAnthropic(MODEL_ANSWER[:80])
    with pytest.raises(ExtractionError, match="cut off"):
        await from_model(
            message("flight_plain.eml"),
            settings=settings,
            client=client,  # type: ignore[arg-type]
        )


async def test_a_refusal_is_an_error_not_a_booking(settings: Settings) -> None:
    client = FakeAnthropic(None, stop_reason="refusal")
    with pytest.raises(ExtractionError, match="refused"):
        await from_model(
            message("flight_plain.eml"),
            settings=settings,
            client=client,  # type: ignore[arg-type]
        )


async def test_no_anthropic_key_means_no_extraction_rather_than_a_crash() -> None:
    settings = Settings(anthropic_api_key="")
    assert await from_model(message("flight_plain.eml"), settings=settings) is None


async def test_an_email_with_no_body_costs_no_model_call(settings: Settings) -> None:
    """The subject alone passes the prefilter, and the model would be paid to read nothing."""
    client = FakeAnthropic(MODEL_ANSWER)
    empty = Message(id="empty", subject="Your itinerary", from_addr="a@example.com", date=None)

    assert looks_like_flight(empty)
    assert await from_model(empty, settings=settings, client=client) is None  # type: ignore[arg-type]
    assert client.messages.calls == []


def test_an_html_only_email_is_rendered_as_its_visible_text() -> None:
    html_only = Message(
        id="html",
        subject="Your itinerary",
        from_addr="a@example.com",
        date=None,
        text_html="<html><body><p>Flight <b>DL1234</b></p><p>JFK to LAX</p></body></html>",
    )
    rendered = render(html_only)
    assert "Flight" in rendered and "DL1234" in rendered and "JFK to LAX" in rendered
    assert "<p>" not in rendered


def test_the_html_part_is_read_over_a_plain_part_that_buries_the_flight() -> None:
    """A mail platform's plain rendering inlines a tracking link behind every word.

    Enough of them and the flight table sits past the cut the prompt is made at, while
    the HTML part the person actually read has it in the first screenful.
    """
    tracked = "( http://link.example.com/ls/click?upn=" + "u001." * 220 + " )"
    plain = "\n".join(f"Manage Your Trip {tracked}" for _ in range(12))
    plain += "\nDelta 4963\nThu, Dec 24\nLaGuardia 09:25 AM\nMontreal 11:02 AM"
    assert plain.index("Delta 4963") > extract.MAX_BODY_CHARS

    both = Message(
        id="both",
        subject="Important Update: Flight Schedule Change",
        from_addr="a@example.com",
        date=None,
        text_plain=plain,
        text_html=(
            "<html><body><a href='http://link.example.com/ls/click'>Manage Your Trip</a>"
            "<table><tr><td>Delta 4963</td><td>Thu, Dec 24</td></tr></table></body></html>"
        ),
    )
    rendered = render(both)
    assert "Delta 4963" in rendered
    assert "link.example.com" not in rendered


def test_an_html_part_with_nothing_to_read_falls_back_to_the_plain_part() -> None:
    image_only = Message(
        id="image",
        subject="Your itinerary",
        from_addr="a@example.com",
        date=None,
        text_plain="Flight DL1234, JFK to LAX",
        text_html="<html><body><img src='itinerary.png'></body></html>",
    )
    assert "DL1234" in render(image_only)
