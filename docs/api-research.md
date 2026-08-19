# API Research

Reference notes for the self-hosted flight tracker. Every claim below is tied to the doc URL it came from.

Primary AeroAPI source: the official machine-readable spec at
<https://www.flightaware.com/commercial/aeroapi/resources/aeroapi-openapi.yml>
(`info.version: 4.17.1` as served at time of writing). The human portal at
<https://www.flightaware.com/aeroapi/portal/documentation> renders this same spec behind a login.

---

## 1. FlightAware AeroAPI v4

### 1.1 Base URL and auth

| Item | Value |
| --- | --- |
| Base URL | `https://aeroapi.flightaware.com/aeroapi` |
| Auth scheme | `apiKey`, `in: header` |
| Header name | `x-apikey` |
| Header format | `x-apikey: <YOUR_API_KEY>` - raw key, no `Bearer`, no username |

Source: `servers` / `components.securitySchemes.ApiKeyAuth` in the OpenAPI spec. The spec's server
entry is templated as `https://{env}.flightaware.com/aeroapi` with `env` defaulting to `aeroapi`.
The spec text explicitly notes: "Unlike previous versions of AeroAPI, authentication is now
controlled by an API key that must be set in the header `x-apikey`. Your FlightAware username is
not used when authenticating to the API."
(<https://www.flightaware.com/commercial/aeroapi/resources/aeroapi-openapi.yml>)

### 1.2 `GET /flights/{ident}`

`operationId: get_flight`, tag `flights`.

Path parameter `ident` (required, string). Spec description: "The ident, registration, or
fa_flight_id to fetch. If using a flight ident, it is highly recommended to specify ICAO flight
ident rather than IATA flight ident to avoid ambiguity and unexpected results."
Documented examples: `UAL4` (ident), `N123HQ` (registration), `UAL1234-1234567890-airline-0123`
(fa_flight_id).

Query parameters - the complete list:

| Param | In | Type | Default | Notes |
| --- | --- | --- | --- | --- |
| `ident_type` | query | enum | none | `designator` \| `registration` \| `fa_flight_id`. "By default, the passed ident is interpreted as a registration if possible. This parameter can force the ident to be interpreted as a designator instead." |
| `start` | query | ISO-8601 date or date-time | ~11 days in the past | Inclusive lower bound, compared against `scheduled_out` (or `scheduled_off` when `scheduled_out` is missing). Must be no further than 10 days in the past / 2 days in the future. Bare date implies `00:00:00Z`. |
| `end` | query | ISO-8601 date or date-time | ~2 days in the future | Exclusive upper bound, same comparison field and same 10-day-past / 2-day-future limits. |
| `max_pages` | query | integer, min 1 | `1` | "Maximum number of pages to fetch. This is an upper limit and not a guarantee of how many pages will be returned." |
| `cursor` | query | string | none | "Opaque value used to get the next batch of data from a paged collection." |

All four of `max_pages`, `start`, `end`, `ident_type` are confirmed present with those exact
spellings. There is no `limit`, `page`, or `offset` param - paging is `cursor` + `max_pages`.

Endpoint semantics, quoted from the spec description: "Returns the flight info status summary for
a registration, ident, or fa_flight_id. If a fa_flight_id is specified then a maximum of 1 flight
is returned, unless the flight has been diverted in which case both the original flight and any
diversions will be returned with a duplicate fa_flight_id. If a registration or ident is
specified, approximately 14 days of recent and scheduled flight information is returned, ordered
by `scheduled_out` (or `scheduled_off` if `scheduled_out` is missing) descending."

### 1.3 Response envelope

200 response content type is `application/json; charset=UTF-8`. Top-level object, all three keys
required:

```json
{
  "links": { "next": "/flights/UAL4?cursor=..." },
  "num_pages": 1,
  "flights": [ { "...": "flight object" } ]
}
```

| Key | Type | Notes |
| --- | --- | --- |
| `links` | object, **nullable** | Has one required key, `next` (string, `uri-reference`). The whole `links` object is null when there is no next page. |
| `num_pages` | integer, min 1 | "Number of pages returned" |
| `flights` | array | Array of flight objects (schema below) |

Yes: it is a `flights` array, there is a `links` object, and there is a `num_pages` integer.

400 error body is a flat object: `title`, `reason`, `detail`, `status` (all required).
Documented 400 reason: "Ident may be missing or not in fa_flight_id format or pages may be < 1."

### 1.4 Flight object - full field list

The item schema is `allOf: [BaseFlight, ForesightPredictionsAvailable]`. Flat - the only nested
objects are `origin` and `destination`.

| Field | Type | Nullable | Description |
| --- | --- | --- | --- |
| `ident` | string | no | Operator code + flight number (commercial) or registration (GA) |
| `ident_icao` | string | yes | ICAO operator code + flight number |
| `ident_iata` | string | yes | IATA operator code + flight number |
| `actual_runway_off` | string | yes | Actual departure runway at origin |
| `actual_runway_on` | string | yes | Actual arrival runway at destination |
| `fa_flight_id` | string | no | Unique FlightAware id. Diversions produce a duplicate `fa_flight_id` on the new leg. |
| `operator` | string | yes | ICAO code if it exists, otherwise IATA |
| `operator_icao` | string | yes | ICAO operator code |
| `operator_iata` | string | yes | IATA operator code |
| `flight_number` | string | yes | Bare flight number |
| `registration` | string | yes | Tail number |
| `atc_ident` | string | yes | ATC ident when known and different from `ident` |
| `inbound_fa_flight_id` | string | yes | `fa_flight_id` of the aircraft's previous flight |
| `codeshares` | array of string | yes | ICAO codeshares |
| `codeshares_iata` | array of string | yes | IATA codeshares |
| `blocked` | boolean | no | Blocked from public viewing |
| `diverted` | boolean | no | Flight was diverted |
| `cancelled` | boolean | no | No longer tracked by FlightAware. Spec warning: "There are a number of reasons this could happen including cancellation by the airline, but that will not always be the case." |
| `position_only` | boolean | no | No flight plan / schedule / intent available |
| `origin` | object (`FlightAirportRef`) | yes | See below |
| `destination` | object (`FlightAirportRef`) | yes | See below |
| `departure_delay` | integer | yes | **Seconds**. Negative = early. Gate-based, falls back to runway. |
| `arrival_delay` | integer | yes | **Seconds**. Negative = early. |
| `filed_ete` | integer | yes | Runway-to-runway filed duration, seconds |
| `progress_percent` | integer 0-100 | yes | Runway departure/arrival based. Null for en route position-only flights. |
| `status` | string | no | Human-readable status summary |
| `aircraft_type` | string | yes | ICAO type code generally, IATA when ICAO unknown |
| `route_distance` | integer | yes | Planned distance in **statute miles** |
| `filed_airspeed` | integer | yes | Filed IFR airspeed, knots |
| `filed_altitude` | integer | yes | Filed IFR altitude, **hundreds of feet** |
| `route` | string | yes | Textual route description |
| `baggage_claim` | string | yes | Baggage claim location at destination |
| `seats_cabin_business` | integer | yes | |
| `seats_cabin_coach` | integer | yes | |
| `seats_cabin_first` | integer | yes | |
| `gate_origin` | string | yes | |
| `gate_destination` | string | yes | |
| `terminal_origin` | string | yes | |
| `terminal_destination` | string | yes | |
| `type` | enum string | no | `General_Aviation` \| `Airline` |
| `scheduled_out` | date-time | yes | Scheduled gate departure |
| `estimated_out` | date-time | yes | Estimated gate departure |
| `actual_out` | date-time | yes | Actual gate departure |
| `scheduled_off` | date-time | yes | Scheduled runway departure |
| `estimated_off` | date-time | yes | Estimated runway departure |
| `actual_off` | date-time | yes | Actual runway departure |
| `scheduled_on` | date-time | yes | Scheduled runway arrival |
| `estimated_on` | date-time | yes | Estimated runway arrival |
| `actual_on` | date-time | yes | Actual runway arrival |
| `scheduled_in` | date-time | yes | Scheduled gate arrival |
| `estimated_in` | date-time | yes | Estimated gate arrival |
| `actual_in` | date-time | yes | Actual gate arrival |
| `foresight_predictions_available` | boolean | no | From the `ForesightPredictionsAvailable` sub-schema |

Every name in the task list was confirmed with that exact spelling. Notes on the ones easy to get
wrong:

- `seats_cabin_business` / `seats_cabin_coach` / `seats_cabin_first` - that is the exact order of
  the words, `seats_cabin_*` not `cabin_seats_*`.
- `codeshares` is ICAO-form; `codeshares_iata` is a separate field. Do not assume one list.
- `departure_delay` / `arrival_delay` are **seconds**, not minutes.
- `filed_altitude` is in **hundreds of feet** (FL-style), not feet.
- `operator_icao` is a real property but is **not** in the schema's `required` list, unlike
  `operator` and `operator_iata`. Treat it as possibly absent, not merely null.
- `ident_icao` and `ident_iata` are likewise properties but absent from `required`.
- `foresight_predictions_available` is required and will always be present.

#### `origin` / `destination` (title `FlightAirportRef`)

Both are nested objects, nullable, with identical shape:

| Key | Type | Nullable | Required | Notes |
| --- | --- | --- | --- | --- |
| `code` | string | yes | **yes** | "ICAO/IATA/LID code or string indicating the location where tracking of the flight began/ended for position-only flights" |
| `code_icao` | string | yes | no | ICAO code |
| `code_iata` | string | yes | no | IATA code |
| `code_lid` | string | yes | no | LID code |
| `timezone` | string | yes | no | TZ database format, e.g. `America/New_York` |
| `name` | string | yes | no | Common name, e.g. `LaGuardia` |
| `city` | string | yes | no | Closest city, e.g. `New York` |
| `airport_info_url` | string (uri-reference) | yes | **yes** | Null for position-only flights |

So: yes, nested objects; the keys are `code`, `code_icao`, `code_iata`, `code_lid`, `timezone`,
`name`, `city`, `airport_info_url`. There is no `code_iso` / `country` / lat-lon on this ref - use
the `/airports/{id}` endpoint for that.

### 1.5 Can you poll by `fa_flight_id` on the main `/flights/{ident}` path?

**Yes. This is valid.** The design doc's assumption holds.

The spec's own description for `GET /flights/{ident}` states: "Returns the flight info status
summary for a registration, ident, or fa_flight_id. If a fa_flight_id is specified then a maximum
of 1 flight is returned, unless the flight has been diverted in which case both the original
flight and any diversions will be returned with a duplicate fa_flight_id." The path parameter's
own description says "The ident, registration, or fa_flight_id to fetch" and gives
`UAL1234-1234567890-airline-0123` as a worked example, and `ident_type` accepts the literal value
`fa_flight_id`.

Recommended call shape for polling a known flight:

```
GET /flights/{fa_flight_id}?ident_type=fa_flight_id&max_pages=1
```

Passing `ident_type=fa_flight_id` removes the "interpreted as a registration if possible"
ambiguity and guarantees a 400 rather than a wrong-flight 200 if the id is malformed.

Caveat worth coding for: a **diverted** flight returns more than one element in `flights` with
the *same* `fa_flight_id`. Do not write `flights[0]` and assume it is the current leg - check
`diverted` and pick the leg by timestamps.

Sub-resources are a different path parameter. The spec spells them `/flights/{id}/position`,
`/flights/{id}/track`, `/flights/{id}/route`, `/flights/{id}/map` (note `{id}`, not `{ident}`) -
those take an `fa_flight_id` only. Also on the `{ident}` form: `/flights/{ident}/canonical` and
`/flights/{ident}/intents`.

### 1.6 Timestamp format

All `*_out` / `*_off` / `*_on` / `*_in` fields are `type: string, format: date-time`, and every
one carries the same spec example: `2021-12-31T19:59:59Z`.

So: **ISO-8601, UTC, `Z` suffix, second precision, no offset and no fractional seconds.**
Parse with `datetime.fromisoformat` on Python 3.11+ (which accepts `Z`) or normalize `Z` to
`+00:00` first on older runtimes. The `start` / `end` request params accept either a full
date-time (`2021-12-31T19:59:59Z`) or a bare date (`2021-12-31`, implying `00:00:00Z`).

### 1.7 `ident_type` values, and ICAO vs IATA idents

Exactly three enum values: `designator`, `registration`, `fa_flight_id`.

**ICAO form is preferred.** The spec states, in the `ident` path-param description: "If using a
flight ident, it is highly recommended to specify ICAO flight ident rather than IATA flight ident
to avoid ambiguity and unexpected results. Setting the ident_type can also be used to help
disambiguate."

Practically: send `DAL1234` (ICAO), not `DL1234` (IATA). IATA airline codes are two characters and
are reused across carriers, so `DL1234` can resolve to the wrong operator. If you only hold an
IATA ident, pass `ident_type=designator` at minimum, and prefer resolving it to ICAO first.
Default behaviour with no `ident_type` is to try to read the string as a registration, which is
the wrong guess for a commercial flight number.
