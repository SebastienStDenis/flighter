"""IATA to ICAO airline codes, so we ask FlightAware the unambiguous question.

FlightAware's own guidance is to send ICAO idents: two-letter IATA codes are reused
across carriers and across time, so `DL1234` can resolve to the wrong operator while
`DAL1234` cannot. A booking only ever knows the IATA code printed on the ticket, and
there is no free lookup for this, so the mapping is carried here.

It is deliberately a curated list of carriers a person actually flies rather than an
exhaustive registry. An unknown code falls through to the IATA form, which is what we
would have sent anyway, so a gap degrades to the old behaviour instead of failing.
"""

from __future__ import annotations

IATA_TO_ICAO: dict[str, str] = {
    # North America
    "9E": "EDV",
    "AA": "AAL",
    "AC": "ACA",
    "AS": "ASA",
    "B6": "JBU",
    "DL": "DAL",
    "F9": "FFT",
    "G4": "AAY",
    "HA": "HAL",
    "NK": "NKS",
    "PD": "POE",
    "SY": "SCX",
    "TS": "TSC",
    "UA": "UAL",
    "WN": "SWA",
    "WS": "WJA",
    # Europe
    "A3": "AEE",
    "AF": "AFR",
    "AY": "FIN",
    "AZ": "ITY",
    "BA": "BAW",
    "BT": "BTI",
    "DY": "NAX",
    "EI": "EIN",
    "FI": "ICE",
    "FR": "RYR",
    "IB": "IBE",
    "JU": "ASL",
    "KL": "KLM",
    "LG": "LGL",
    "LH": "DLH",
    "LO": "LOT",
    "LX": "SWR",
    "OK": "CSA",
    "OS": "AUA",
    "OU": "CTN",
    "SK": "SAS",
    "SN": "BEL",
    "TP": "TAP",
    "U2": "EZY",
    "UX": "AEA",
    "VS": "VIR",
    "VY": "VLG",
    "W6": "WZZ",
    # Middle East and Africa
    "AT": "RAM",
    "EK": "UAE",
    "ET": "ETH",
    "EY": "ETD",
    "GF": "GFA",
    "KQ": "KQA",
    "LY": "ELY",
    "MS": "MSR",
    "QR": "QTR",
    "RJ": "RJA",
    "SA": "SAA",
    "SV": "SVA",
    "TK": "THY",
    "WY": "OMA",
    # Asia and the Pacific
    "6E": "IGO",
    "AI": "AIC",
    "BR": "EVA",
    "CA": "CCA",
    "CI": "CAL",
    "CX": "CPA",
    "CZ": "CSN",
    "FJ": "FJI",
    "GA": "GIA",
    "HU": "CHH",
    "JL": "JAL",
    "JQ": "JST",
    "KE": "KAL",
    "MH": "MAS",
    "MU": "CES",
    "NH": "ANA",
    "NZ": "ANZ",
    "OZ": "AAR",
    "PR": "PAL",
    "QF": "QFA",
    "SQ": "SIA",
    "TG": "THA",
    "VA": "VOZ",
    "VN": "HVN",
    # Latin America
    "AD": "AZU",
    "AM": "AMX",
    "AR": "ARG",
    "AV": "AVA",
    "CM": "CMP",
    "G3": "GLO",
    "LA": "LAN",
    "Y4": "VOI",
}

OPERATOR_ALIASES: dict[str, str] = {
    "ENDEAVOR AIR": "9E",
}


def normalise_operator(
    carrier: str | None, number: str | None, marketing_number: str
) -> tuple[str | None, str | None]:
    if carrier is None:
        return None, number
    code = carrier.strip().upper()
    alias = OPERATOR_ALIASES.get(code)
    if alias is None:
        return code, number
    return alias, number or marketing_number


def to_icao(carrier: str) -> str:
    """Best-effort ICAO form of an airline code, unchanged when we cannot do better."""
    code = carrier.strip().upper()
    # Three letters is already ICAO; the mapping only ever shortens two-character codes.
    if len(code) == 3:
        return code
    return IATA_TO_ICAO.get(code, code)
