"""The IATA to ICAO mapping, and the fallback that keeps a gap from being a failure."""

from __future__ import annotations

import pytest

from flighter.airlines import IATA_TO_ICAO, normalise_operator, to_icao


@pytest.mark.parametrize(
    ("iata", "icao"),
    [
        ("DL", "DAL"),
        ("BA", "BAW"),
        ("AC", "ACA"),
        ("9E", "EDV"),
        ("6E", "IGO"),
        ("U2", "EZY"),
    ],
)
def test_known_carriers_convert_to_icao(iata: str, icao: str) -> None:
    assert to_icao(iata) == icao


def test_an_icao_code_passes_through_untouched() -> None:
    assert to_icao("DAL") == "DAL"


def test_an_unknown_carrier_falls_back_to_what_we_were_given() -> None:
    # Degrading to the IATA form is what we would have sent anyway, so a missing entry
    # costs accuracy rather than availability.
    assert to_icao("ZZ") == "ZZ"


def test_codes_are_normalised_before_lookup() -> None:
    assert to_icao(" dl ") == "DAL"


def test_endeavor_name_is_normalised_to_its_ident() -> None:
    assert normalise_operator("ENDEAVOR AIR", None, "5449") == ("9E", "5449")


def test_the_table_is_shaped_the_way_the_lookup_assumes() -> None:
    for iata, icao in IATA_TO_ICAO.items():
        assert len(iata) == 2, iata
        assert len(icao) == 3, icao
        assert iata.isupper() and icao.isupper()
    # A two-letter code mapping to itself would be a typo, not a carrier.
    assert not [k for k, v in IATA_TO_ICAO.items() if k == v]
    # ICAO codes are unique per carrier; a duplicate means two airlines got merged.
    assert len(set(IATA_TO_ICAO.values())) == len(IATA_TO_ICAO)
