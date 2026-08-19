"""The three extraction tiers, none of which is allowed to touch the network."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from flight_tracker.config import Settings
from flight_tracker.extract import (
    Extraction,
    ExtractionError,
    from_jsonld,
    from_model,
    looks_like_flight,
    render,
)
from flight_tracker.gmail import Message, parse_message

FIXTURES = Path(__file__).parent / "fixtures"

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
{"is_flight_confirmation": true, "passenger_names": ["SEBASTIEN ST-DENIS"], "confidence": 0.92,
 "segments": [{"marketing_carrier": "WS", "marketing_number": "1502",
 "operating_carrier": null, "operating_number": null, "origin_iata": "YYC",
 "dest_iata": "YVR", "departure_local": "2026-11-17T06:30:00", "departure_tz_hint": null,
 "arrival_local": "2026-11-17T07:12:00", "confirmation_code": "8HTGRX", "seat": "12A"}]}
"""


def message(name: str) -> Message:
    return parse_message((FIXTURES / name).read_bytes(), name)


class FakeMessages:
    """Stands in for `client.messages`; records the request so the prompt is testable."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return FakeResponse(self.answer)


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [FakeBlock(text)]
        self.stop_reason = "end_turn"


class FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class FakeAnthropic:
    def __init__(self, answer: str) -> None:
        self.messages = FakeMessages(answer)


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
    assert extraction.passenger_names == ["SEBASTIEN ST-DENIS"]

    (segment,) = extraction.segments
    assert segment.marketing_carrier == "DL"
    assert segment.marketing_number == "1234"
    assert segment.origin_iata == "JFK"
    assert segment.dest_iata == "LAX"
    assert segment.confirmation_code == "K7QX2M"
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
    assert extraction.passenger_names == ["Sebastien St-Denis"]


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


async def test_model_request_pins_the_schema_and_the_configured_model(
    settings: Settings,
) -> None:
    client = FakeAnthropic(MODEL_ANSWER)
    await from_model(
        message("flight_plain.eml"),
        settings=settings,
        client=client,  # type: ignore[arg-type]
    )
    (request,) = client.messages.calls
    assert request["model"] == settings.anthropic_model
    schema = request["output_config"]["format"]["schema"]
    assert schema["properties"].keys() >= {"is_flight_confirmation", "confidence", "segments"}


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
    with pytest.raises(ExtractionError):
        await from_model(
            message("flight_plain.eml"),
            settings=settings,
            client=client,  # type: ignore[arg-type]
        )


async def test_no_anthropic_key_means_no_extraction_rather_than_a_crash() -> None:
    settings = Settings(anthropic_api_key="")
    assert await from_model(message("flight_plain.eml"), settings=settings) is None
