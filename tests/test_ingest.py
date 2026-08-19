"""The pipeline, against a stand-in session: no database, no Gmail, no Anthropic."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from flight_tracker import ingest
from flight_tracker.config import Settings
from flight_tracker.extract import Extraction, Segment
from flight_tracker.gmail import Message, parse_message
from flight_tracker.models import IngestLog, Passenger

FIXTURES = Path(__file__).parent / "fixtures"

SELF = Passenger(id=1, display_name="Sebastien St-Denis", is_self=True)
OTHER = Passenger(id=2, display_name="Marie Tremblay", is_self=False)


def message(name: str) -> Message:
    return parse_message((FIXTURES / name).read_bytes(), name)


def extraction(
    *,
    names: list[str] | None = None,
    confidence: float = 0.99,
    tz_hint: str | None = None,
) -> Extraction:
    return Extraction(
        is_flight_confirmation=True,
        passenger_names=names if names is not None else ["SEBASTIEN ST-DENIS"],
        confidence=confidence,
        segments=[
            Segment(
                marketing_carrier="WS",
                marketing_number="1502",
                operating_carrier=None,
                operating_number=None,
                origin_iata="YYC",
                dest_iata="YVR",
                departure_local="2026-11-17T06:30:00",
                departure_tz_hint=tz_hint,
                arrival_local="2026-11-17T07:12:00",
                confirmation_code="8HTGRX",
                seat="12A",
            )
        ],
    )


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return self._rows


class FakeSession:
    """Enough AsyncSession to run the pipeline: the ingest log, and the passenger list."""

    def __init__(self, passengers: list[Passenger]) -> None:
        self.passengers = passengers
        self.log: dict[str, IngestLog] = {}
        self.rolled_back = False

    async def get(self, model: type, pk: str) -> Any:
        assert model is IngestLog
        return self.log.get(pk)

    async def execute(self, statement: Any) -> FakeResult:
        return FakeResult(list(self.passengers))

    def add(self, row: Any) -> None:
        assert isinstance(row, IngestLog)
        self.log[row.gmail_message_id] = row

    async def flush(self) -> None:
        return None

    async def rollback(self) -> None:
        self.rolled_back = True


class Recorder:
    """Captures what the pipeline asked bookings.py and airports.py to do."""

    def __init__(self, duplicate: bool = False) -> None:
        self.created: list[dict[str, Any]] = []
        self.zones_asked: list[str] = []
        self.duplicate = duplicate

    async def airport_tz(self, session: Any, iata: str) -> str:
        self.zones_asked.append(iata)
        return {"YYC": "America/Edmonton", "JFK": "America/New_York"}.get(iata, "UTC")

    async def find_duplicate(self, session: Any, *args: Any) -> Any:
        return _Booking(99) if self.duplicate else None

    async def create_booking(self, session: Any, **kwargs: Any) -> Any:
        self.created.append(kwargs)
        return _Booking(len(self.created))


class _Booking:
    def __init__(self, booking_id: int) -> None:
        self.id = booking_id


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(ingest, "airport_tz", rec.airport_tz)
    monkeypatch.setattr(ingest, "find_duplicate", rec.find_duplicate)
    monkeypatch.setattr(ingest, "create_booking", rec.create_booking)
    return rec


def use_model(monkeypatch: pytest.MonkeyPatch, result: Extraction | None) -> None:
    async def fake(message: Message, **kwargs: Any) -> Extraction | None:
        return result

    monkeypatch.setattr(ingest, "from_model", fake)


# -- names ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ticket_name",
    ["SEBASTIEN ST-DENIS", "Sebastien St Denis", "St-Denis/Sebastien Mr", "Sébastien St-Denis"],
)
def test_a_name_off_a_ticket_matches_the_passenger(ticket_name: str) -> None:
    assert ingest.names_match(ticket_name, "Sebastien St-Denis")


def test_a_different_person_does_not_match() -> None:
    assert not ingest.names_match("Marie Tremblay", "Sebastien St-Denis")


def test_a_middle_name_is_ignored() -> None:
    assert ingest.names_match("SEBASTIEN MARC ST-DENIS", "Sebastien St-Denis")


# -- the pipeline --------------------------------------------------------------------


async def test_structured_confirmation_becomes_an_active_booking(
    settings: Settings, recorder: Recorder
) -> None:
    session = FakeSession([SELF])
    outcome = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_jsonld.eml"),
        settings=settings,
    )

    assert outcome == "created"
    (created,) = recorder.created
    assert created["status"] == "active"
    assert created["passenger_id"] == SELF.id
    assert created["marketing_carrier"] == "DL"
    assert created["departure_local"] == datetime(2026, 9, 12, 18, 40)
    assert created["source"] == "email"
    assert created["source_message_id"] == "flight_jsonld.eml"

    logged = session.log["flight_jsonld.eml"]
    assert logged.outcome == "created"
    assert logged.raw_extraction is not None
    assert logged.raw_extraction["segments"][0]["confirmation_code"] == "K7QX2M"


async def test_multi_segment_itinerary_books_every_leg(
    settings: Settings, recorder: Recorder
) -> None:
    session = FakeSession([SELF])
    outcome = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_package_jsonld.eml"),
        settings=settings,
    )

    assert outcome == "created"
    assert [(c["marketing_number"], c["origin_iata"]) for c in recorder.created] == [
        ("8830", "YUL"),
        ("856", "YYZ"),
    ]
    assert session.log["flight_package_jsonld.eml"].outcome == "created"


async def test_marketing_email_never_reaches_an_extractor(
    settings: Settings, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(_: Message, **kwargs: Any) -> None:
        raise AssertionError("the prefilter should have stopped this")

    monkeypatch.setattr(ingest, "from_model", explode)
    session = FakeSession([SELF])

    outcome = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("airline_promo.eml"),
        settings=settings,
    )

    assert outcome == "no_flight"
    assert session.log["airline_promo.eml"].raw_extraction is None
    assert recorder.created == []


async def test_the_model_path_runs_when_there_is_no_structured_data(
    settings: Settings, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_model(monkeypatch, extraction())
    session = FakeSession([SELF])

    outcome = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_plain.eml"),
        settings=settings,
    )

    assert outcome == "created"
    assert recorder.created[0]["marketing_carrier"] == "WS"


async def test_the_stated_timezone_hint_is_discarded(
    settings: Settings, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The airline said Asia/Tokyo for a flight out of Calgary. It is not consulted."""
    use_model(monkeypatch, extraction(tz_hint="Asia/Tokyo"))
    session = FakeSession([SELF])

    await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_plain.eml"),
        settings=settings,
    )

    (created,) = recorder.created
    assert created["departure_local"] == datetime(2026, 11, 17, 6, 30)
    assert created["departure_local"].tzinfo is None
    assert "departure_tz_hint" not in created
    # The only zone anybody asked about came from the airports table.
    assert recorder.zones_asked == ["YYC"]


async def test_an_unrecognised_name_goes_to_review_under_the_self_passenger(
    settings: Settings, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_model(monkeypatch, extraction(names=["JEAN-PHILIPPE BEAULIEU"]))
    session = FakeSession([SELF])

    outcome = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_plain.eml"),
        settings=settings,
    )

    assert outcome == "review"
    (created,) = recorder.created
    assert created["status"] == "pending_review"
    assert created["passenger_id"] == SELF.id


async def test_a_second_passenger_is_matched_by_name(
    settings: Settings, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_model(monkeypatch, extraction(names=["TREMBLAY/MARIE"]))
    session = FakeSession([SELF, OTHER])

    outcome = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_plain.eml"),
        settings=settings,
    )

    assert outcome == "created"
    assert recorder.created[0]["passenger_id"] == OTHER.id


async def test_low_confidence_goes_to_review(
    settings: Settings, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_model(monkeypatch, extraction(confidence=0.4))
    session = FakeSession([SELF])

    outcome = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_plain.eml"),
        settings=settings,
    )

    assert outcome == "review"
    assert recorder.created[0]["status"] == "pending_review"
    assert recorder.created[0]["extraction_confidence"] == pytest.approx(0.4)


async def test_a_flight_we_already_have_is_not_booked_twice(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = Recorder(duplicate=True)
    monkeypatch.setattr(ingest, "airport_tz", rec.airport_tz)
    monkeypatch.setattr(ingest, "find_duplicate", rec.find_duplicate)
    monkeypatch.setattr(ingest, "create_booking", rec.create_booking)
    session = FakeSession([SELF])

    outcome = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_jsonld.eml"),
        settings=settings,
    )

    assert outcome == "duplicate"
    assert rec.created == []


async def test_the_same_message_delivered_twice_is_a_no_op(
    settings: Settings, recorder: Recorder
) -> None:
    session = FakeSession([SELF])
    first = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_jsonld.eml"),
        settings=settings,
    )
    second = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_jsonld.eml"),
        settings=settings,
    )

    assert (first, second) == ("created", "created")
    assert len(recorder.created) == 1


async def test_a_failing_extraction_is_logged_and_swallowed(
    settings: Settings, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(message: Message, **kwargs: Any) -> Extraction:
        raise RuntimeError("model output did not match the extraction schema")

    monkeypatch.setattr(ingest, "from_model", boom)
    session = FakeSession([SELF])

    outcome = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_plain.eml"),
        settings=settings,
    )

    assert outcome == "error"
    logged = session.log["flight_plain.eml"]
    assert logged.error is not None and "RuntimeError" in logged.error
    assert session.rolled_back


async def test_an_extraction_that_is_not_a_confirmation_is_no_flight(
    settings: Settings, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_model(
        monkeypatch,
        Extraction(is_flight_confirmation=False, passenger_names=[], confidence=0.1, segments=[]),
    )
    session = FakeSession([SELF])

    outcome = await ingest.process_message(
        session,  # type: ignore[arg-type]
        message("flight_plain.eml"),
        settings=settings,
    )

    assert outcome == "no_flight"
    # The raw answer is still kept: it is the evidence for why nothing was booked.
    assert session.log["flight_plain.eml"].raw_extraction is not None
    assert recorder.created == []
