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

---

## 2. AeroAPI pricing and Personal-tier limits

Sources:
<https://www.flightaware.com/commercial/aeroapi/> (tier comparison + full per-query fee table),
<https://www.flightaware.com/commercial/aeroapi/v4/> (result-set definition),
<https://discussions.flightaware.com/t/what-is-considered-a-result-set-using-flights/85117>
(FlightAware staff clarification on `/flights`).
The portal page <https://www.flightaware.com/aeroapi/portal/usage#pricing> sits behind a login and
renders the same figures; the public commercial pages above are the citable versions.

### 2.1 Tiers

| | Personal | Standard | Premium |
| --- | --- | --- | --- |
| Free monthly allowance | Up to **$5/month** in usage fees (**$10/month** for ADS-B feeders) | $5/month ($10 for feeders) | $5/month ($10 for feeders) |
| Monthly minimum fee | **No minimum** | **$100/month** | $1,000/month |
| Result set rate limit | **10 result sets/minute** | 5 result sets/second | 100 result sets/second |
| Historical flight data | Not included | Included | Included |
| Alerts API | **Not included** | Included | Included |
| Volume discounting | Not included | Included | Included |
| Licence | Personal or academic use only | Business / B2C | Business / B2C / B2B |

Confirmed: Personal free allowance is **$5/month**, rate limit is **10 result sets per minute**,
no monthly minimum, and the **next tier up (Standard) has a $100/month minimum**. Note the tiers
are named Personal / Standard / Premium - there is no "Professional" tier.

Two Personal-tier gotchas for a self-hosted tracker:

- Personal is licensed for "storage and distribution of derivative works for **personal or
  academic purposes only**". A private self-hosted tracker for yourself is fine; publishing it is
  not.
- **Alerts are not available on Personal.** Push-style flight events (`POST /alerts`) require
  Standard or above, so a Personal-tier tracker must poll.

### 2.2 Price for the `/flights/{ident}` class

| Endpoint | Price |
| --- | --- |
| `GET /flights/{ident}` | **$0.005 / result set** |
| `GET /flights/{ident}/canonical` | $0.001 / result set |
| `GET /flights/search` | $0.050 / result set |
| `GET /flights/search/advanced` | $0.050 / result set |
| `GET /flights/search/count` | $0.020 / result set |
| `GET /flights/{id}/position` | $0.010 / result set |
| `GET /flights/{id}/track` | $0.012 / result set |
| `GET /flights/{id}/route` | $0.010 / result set |
| `GET /flights/{id}/map` | $0.030 / result set |
| `GET /airports/{id}` class | $0.005 / result set (`/airports`), $0.004 (`/airports/nearby`) |

At $0.005/result set, the **$5 Personal allowance is 1,000 result sets per month** of
`/flights/{ident}` - and only if every call stays at one page.

### 2.3 What exactly counts as a result set

Quoting <https://www.flightaware.com/commercial/aeroapi/>: "A single query can return multiple
results, depending on the call type and input. Pricing is based on result sets, with one set
equaling 15 records."

And <https://www.flightaware.com/commercial/aeroapi/v4/>: "For pricing purposes, a 'result set' is
defined as 15 results (records). Pricing is per result set... Note: The `max_pages` input
parameter can be used to limit/control how many result sets will be returned, with one page being
equivalent to one result set."

FlightAware staff on the forum thread, answering specifically for `/flights`:

- "up to 15 responses (15 responses = 1 page) for it to count as 1 call"
- "the result is the flight. All of the data points within the flight are not a response/result"
- "All of the data encompassed within that call is a single result set... it would be charged as 1 page."

So, precisely: **billing is per page returned, where a page holds up to 15 records, and a record
is one flight object - not one call and not one field.** The consequences:

- A call returning 1 flight costs the same as a call returning 15 flights: 1 result set.
- A call returning 16 flights costs 2 result sets; 31 flights costs 3.
- `max_pages` is the cost lever. `max_pages=1` caps any single call at 1 result set.
- Polling by bare `ident` with no `start`/`end` defaults to ~14 days of flights, which for a daily
  route is comfortably more than 15 records and therefore **more than one result set per poll**
  unless you pin `max_pages=1`.
- The rate limit is also counted in result sets, not requests: on Personal, 10 result sets/minute
  means a `max_pages=1` call can be made roughly every 6 seconds, but a 3-page call consumes 3 of
  the 10.

### 2.4 Hard spending cap

**There is no documented self-serve spending cap for AeroAPI v4.** The $5/month free allowance is
the only automatic control on Personal, and usage past it is billed rather than blocked - nothing
on the public pricing pages describes a user-settable dollar limit or an auto-disable at $0.
FlightAware's support material frames AeroAPI subscriptions as user-monitored: it is on you to
watch the usage graph in the portal.

The "billable query cap" language that turns up in forum threads
(<https://discussions.flightaware.com/t/price-capping/58088>: "By default once the billable query
cap is reached the API will decline further requests and no additional queries can be made until
the next billing cycle") is **FlightXML 3**, the previous generation with fixed subscription
tiers. Do not carry that assumption into AeroAPI v4.

Practical consequence: enforce the budget in your own code. Count result sets locally, persist the
month-to-date count, and refuse to call once you cross your chosen ceiling. Treat AeroAPI as
having no backstop.

---

## 3. Pushover

Source: <https://pushover.net/api> (the whole reference is one page), plus
<https://pushover.net/api/validate> for the credential check.

### 3.1 Sending a message

One endpoint, `POST https://api.pushover.net/1/messages.json`, HTTPS only, parameters sent as a
form body. Two of the three required parameters are credentials, which is the whole of the auth
scheme: there is no header, no bearer token and no signature.

| Parameter | Required | Limit | Notes |
| --- | --- | --- | --- |
| `token` | yes | 30 chars, `[A-Za-z0-9]`, case-sensitive | The *application's* API token, from pushover.net/apps/build |
| `user` | yes | 30 chars, same alphabet | The user or group key from the dashboard. Accepts a comma-separated list, max 50 |
| `message` | yes | 1024 UTF-8 characters | The body |
| `title` | no | 250 characters | Defaults to the application's name |
| `url` | no | 512 characters | Supplementary URL, rendered as a tappable link |
| `url_title` | no | 100 characters | Link text for `url` |
| `priority` | no | `-2` to `2` | See below |
| `device` | no | 25 chars, `[A-Za-z0-9_-]` | Target one device instead of all |
| `sound` | no | - | Built-in or custom sound id |
| `timestamp` | no | - | Unix time the event happened, rather than when it was sent |
| `ttl` | no | seconds | Auto-delete after this long. Ignored at priority 2 |
| `html` / `monospace` | no | `1` | Mutually exclusive |
| `retry` / `expire` / `callback` | priority 2 only | see below | |

### 3.2 Priority, and why this service only uses three of the five

| Value | Name | Behaviour |
| --- | --- | --- |
| `-2` | lowest | No notification at all; increments the iOS badge only |
| `-1` | low | No sound and no vibration; a popup or scrolling notification only |
| `0` | normal | Sound, vibration and alert per the device's own settings. **Treated as `-1` during the user's quiet hours** |
| `1` | high | **Bypasses quiet hours**, always plays a sound and vibrates, displayed in red |
| `2` | emergency | Repeats until acknowledged on the device |

The quiet-hours line is the one that decides the mapping. A gate change, a cancellation and a
diversion are exactly the events that cost you the flight if you sleep through them, so they go at
`1`; everything else is read when you next look at the phone and goes at `0`, respecting quiet
hours. The connectivity check sends at `-1` so that proving the credentials work does not buzz a
pocket.

`2` is deliberately unused. It requires `retry` (minimum 30 seconds) and `expire` (maximum 10800),
re-alerts up to 50 times until someone acknowledges it on the device, and returns a `receipt` to
poll. Even a cancellation is read once and then handled with the airline, so nothing here justifies
a page-until-acknowledged loop.

### 3.3 Reading the response

HTTP 200 means the request was valid and the message is queued. The body still has to be checked:

```json
{"status":1,"request":"647d2300-702c-4b38-8b2f-d56326ae460b"}
```

A 4xx carries the reason in an array, which is worth surfacing verbatim rather than reporting a
bare status code:

```json
{"status":0,"errors":["application token is invalid"],"request":"5042853c-402d-4a18-abcb-168734a801de"}
```

The documented retry policy: 4xx means the input is wrong and must not be retried unchanged; 5xx
and timeouts are temporary and may be retried after 5 seconds or more. No more than 2 concurrent
connections.

### 3.4 Limits and cost

Free accounts send 10,000 messages a month, resetting on the 1st at 00:00 Central. Exceeding it
answers HTTP 429. A tracker polling a handful of flights sends single-digit messages a day, so the
allowance is not a constraint worth designing around.

The cost is on the receiving side: the client app is a one-time purchase per platform after a
30-day trial, with no subscription.

### 3.5 Checking credentials without sending anything

`POST https://api.pushover.net/1/users/validate.json` with `token` and `user` answers `status: 1`
and lists the account's devices. Useful for a setup check that should not push. This service sends
a real message at priority `-1` instead, on the grounds that a check which proves delivery end to
end is worth more than one that proves only that a key parses.


---

## 4. Scriptable widget API

Sources: <https://docs.scriptable.app/listwidget/>, <https://docs.scriptable.app/widgetdate/>,
<https://docs.scriptable.app/widgetstack/>, <https://docs.scriptable.app/keychain/>,
<https://docs.scriptable.app/filemanager/>, <https://docs.scriptable.app/script/>,
<https://docs.scriptable.app/config/>

### 4.1 Confirmed signatures

Every member the plan relies on exists. Exact declarations:

| Member | Declaration | Notes |
| --- | --- | --- |
| `new ListWidget()` | `new ListWidget()` | |
| `ListWidget.addDate()` | `addDate(date: Date): WidgetDate` | Returns a `WidgetDate` you then style |
| `ListWidget.addText()` | `addText(text: string): WidgetText` | |
| `ListWidget.addStack()` | `addStack(): WidgetStack` | Horizontal by default |
| `ListWidget.addSpacer()` | `addSpacer(length: number): WidgetSpacer` | Pass `null` for flexible |
| `ListWidget.addImage()` | `addImage(image: Image): WidgetImage` | |
| `ListWidget.url` | `url: string` | "The URL will be opened when the widget is tapped." |
| `ListWidget.refreshAfterDate` | `refreshAfterDate: Date` | "Indicates when the widget can be refreshed again." |
| `ListWidget.setPadding()` | `setPadding(top: number, leading: number, bottom: number, trailing: number)` | |
| `ListWidget.spacing` | `spacing: number` | |
| `ListWidget.backgroundColor` / `backgroundImage` / `backgroundGradient` | | |
| `ListWidget.addAccessoryWidgetBackground` | `addAccessoryWidgetBackground: bool` | iOS 16+, adaptive background for accessory widgets |
| `ListWidget.presentSmall/Medium/Large/ExtraLarge()` | returns `Promise` | For in-app preview |
| `ListWidget.presentAccessoryInline/Circular/Rectangular()` | returns `Promise` | iOS 16+ preview |
| `WidgetStack.url` | `url: string` | Exists. See the caveat below. |
| `Keychain.contains()` | `static contains(key: string): bool` | |
| `Keychain.set()` | `static set(key: string, value: string)` | Encrypted store |
| `Keychain.get()` | `static get(key: string): string` | **Throws if the key is absent** - guard with `contains()` |
| `Keychain.remove()` | `static remove(key: string)` | |
| `FileManager.local()` | `static local(): FileManager` | |
| `FileManager.iCloud()` | `static iCloud(): FileManager` | Requires iCloud enabled |
| `fm.cacheDirectory()` | `cacheDirectory(): string` | Instance method on the FileManager, so `FileManager.local().cacheDirectory()` is correct |
| `fm.documentsDirectory()` | `documentsDirectory(): string` | |
| `fm.temporaryDirectory()` | `temporaryDirectory(): string` | |
| `fm.joinPath()` | `joinPath(lhsPath: string, rhsPath: string): string` | |
| `fm.readString()` / `fm.writeString()` | `readString(filePath: string): string` / `writeString(filePath: string, content: string)` | |
| `fm.fileExists()` | `fileExists(filePath: string): bool` | |
| `Script.setWidget()` | `static setWidget(widget: any)` | |
| `Script.complete()` | `static complete()` | |
| `Script.name()` | `static name(): string` | |
| `Script.setShortcutOutput()` | `static setShortcutOutput(value: any)` | |

`WidgetDate` styling methods - all take no arguments and return nothing:

| Method | Docs description | Example output |
| --- | --- | --- |
| `applyDateStyle()` | "Display entire date." **This is the default styling.** | `June 3, 2019` |
| `applyTimeStyle()` | "Display time component of the date." | `11:23PM` |
| `applyRelativeStyle()` | "Display date as relative to now." | `2 hours, 23 minutes`; `1 year, 1 month` |
| `applyOffsetStyle()` | "Display date as offset from now." | `+2 hours`; `-3 months` |
| `applyTimerStyle()` | "Display date as timer counting from now." | `2:32`; `36:59:01` |

`WidgetDate` also carries `date`, `textColor`, `font`, `textOpacity`, `lineLimit`,
`minimumScaleFactor`, `shadowColor`, `shadowRadius`, `shadowOffset`, and `url`.

### 4.2 `config.widgetFamily`

Quoting <https://docs.scriptable.app/config/>: "Possible values are: `small`, `medium`, `large`,
`extraLarge`, `accessoryRectangular`, `accessoryInline`, `accessoryCircular`, and `null`."

**`accessoryRectangular` is confirmed present.** `null` is the value when the script is not running
in a widget at all, so branch on it rather than assuming a family. `extraLarge` requires iPadOS 15;
the three `accessory*` families require iOS 16+. `config` also exposes `runsInApp`,
`runsInActionExtension`, `runsWithSiri`, `runsInWidget`, `runsInAccessoryWidget`,
`runsInNotification`, and `runsFromHomeScreen`, all read-only booleans.

### 4.3 Tap targets - the `url` caveat

`ListWidget.url` and `WidgetStack.url` both exist, but they are not interchangeable across sizes.
The `WidgetStack.url` doc says: "The URL will be opened when the text is tapped. **This is only
supported in medium and large widgets. Small widgets can only have a single tap target, which is
specified by the `url` on the widget.**" The same restriction is documented on `WidgetDate.url` and
`WidgetText.url`.

So per-row deep links only work on `medium` / `large`. On `small` and on the Lock Screen accessory
families, set `ListWidget.url` and accept one tap target for the whole widget.

### 4.4 `applyTimerStyle()` with past dates

**It counts up, and it does so silently.** The underlying SwiftUI style (`Text(date, style:
.timer)`) counts down while the date is in the future and, once the date passes, counts up from it.
The rendered string carries no sign and no other marker, so `2:32` is ambiguous between "departs in
2m32s" and "departed 2m32s ago". Scriptable's own example outputs (`2:32`, `36:59:01`) show the
bare magnitude format.

Consequences for a flight widget:

- Never render a bare `applyTimerStyle()` for a time that may already have passed. Compare against
  `Date()` in script and switch to a static `addText("Departed")`, or to `applyOffsetStyle()`
  (which does emit `+2 hours` / `-3 months` with a sign), once the date is behind you.
- The timer is rendered by the system and ticks without a widget reload, which is exactly why it is
  worth using - it is the only way to get a live-updating countdown between refreshes. That same
  property is what makes an unnoticed roll-past-zero persist on screen indefinitely.
- Set `refreshAfterDate` to the moment the display should change meaning (e.g. the scheduled
  departure) so the widget reloads and re-decides which style to draw. Note the doc wording:
  "Indicates when the widget **can** be refreshed again" - WidgetKit treats it as a hint, not a
  guarantee, and budgets reloads. Do not build logic that assumes a refresh lands on time.
- Known platform flakiness: `Text(Date(), style: .timer)` in widgets has regressed in past iOS
  releases (e.g. iOS 16.0 beta 7,
  <https://developer.apple.com/forums/thread/713008>). Have a static fallback string.

---

## 5. Library versions

Latest stable releases on PyPI, read from `https://pypi.org/pypi/<name>/json` (`info.version`) on
2026-08-19.

| Package | Version |
| --- | --- |
| fastapi | 0.141.1 |
| uvicorn | 0.52.4 |
| sqlalchemy | 2.0.52 |
| alembic | 1.19.1 |
| asyncpg | 0.31.0 |
| jinja2 | 3.1.6 |
| httpx | 0.28.1 |
| pydantic | 2.13.4 |
| pydantic-settings | 2.15.0 |
| anthropic | 0.125.0 |
| google-api-python-client | 2.198.0 |
| google-auth-oauthlib | 1.4.0 |
| aioimaplib | 2.0.1 |
| airportsdata | 20260803 |
| python-multipart | 0.0.32 |
| pytest | 9.1.1 |
| pytest-asyncio | 1.4.0 |
| ruff | 0.16.3 |
| mypy | 2.3.1 |

Note: `airportsdata` uses a date-stamped version scheme (`YYYYMMDD`), not semver - pin it
explicitly or it will move under you on every rebuild.

Note: `aioimaplib` is **GPLv3**, and it is the only maintained async IMAP client with real RFC 2177
IDLE support. Nothing conflicts today because this repository declares no licence, but the
published image links it, so it constrains any future non-GPL licensing.

---

## Corrections to the plan

Ordered by how much they change the design.

### 1. `fa_flight_id` polling on `/flights/{ident}` - the plan is CORRECT

Verified against the spec, not assumed. `GET /flights/{ident}` accepts an ident, a registration, or
an `fa_flight_id` in the same path position, and `ident_type` has a literal `fa_flight_id` value.
The plan's polling path stands. Two refinements rather than corrections:

- Always send `ident_type=fa_flight_id` explicitly. The documented default is to interpret the
  string as a registration if it can, so leaving it off is a silent-wrong-answer risk, not just an
  efficiency one.
- Handle the diversion case. The spec says a diverted flight returns "both the original flight and
  any diversions... with a duplicate fa_flight_id", so `flights` can hold more than one element
  even for an `fa_flight_id` lookup. Code that reads `flights[0]` unconditionally will show the
  pre-diversion leg.

### 2. The cost model is probably wrong in the plan's favour and against it at once

Billing is **per page of up to 15 flight records**, not per call and not per flight. That cuts both
ways:

- If the plan budgeted per *flight returned*, it over-estimates. Fifteen flights in one page is one
  result set, $0.005.
- If the plan budgeted per *call* while polling by bare `ident` with no `max_pages`, it
  under-estimates - possibly by several times. An ident lookup with no `start`/`end` returns
  ~14 days of that flight number, which for a daily route is 14+ records and can spill to a second
  page. **Every call in the tracker should carry `max_pages=1`.** That is the documented cost lever:
  "one page being equivalent to one result set."

### 3. There is no spending cap to fall back on

If the plan assumes FlightAware will stop serving at the $5 line, that is wrong. Nothing on the
public pricing pages offers a user-settable cap or an auto-disable for AeroAPI v4; the "billable
query cap" behaviour that appears in forum answers is FlightXML 3, the previous product. Budget
enforcement has to live in the tracker: a persisted month-to-date result-set counter and a hard
refusal past your own ceiling.

Sizing that ceiling: $5 / $0.005 = **1,000 result sets per month**, or ~33/day. One flight polled
every 5 minutes for a 3-hour window is 36 calls - roughly one flight per day and nothing else.
Polling every minute across several flights blows the allowance in days. Plan an adaptive interval
(sparse until T-2h, tighter near departure, stop after `actual_in`).

### 4. Tier naming, and two Personal-tier walls the plan may not account for

The tiers are **Personal / Standard / Premium**. There is no "Professional" tier - if the plan
names one, it is referring to Standard.

- **Alerts are Standard-tier and above.** If the design mentions AeroAPI push alerts as a
  cheaper alternative to polling, that path is closed on Personal - and the next tier up carries a
  **$100/month minimum**, not a per-use fee. Polling is the only Personal-tier option.
- Personal is licensed for "personal or academic purposes only". Fine for a self-hosted private
  tracker; not fine if it is ever shared or published.

### 5. Field-level assumptions worth double-checking in the plan

- `departure_delay` / `arrival_delay` are **seconds**. Anything treating them as minutes is off by
  60x.
- `filed_altitude` is in **hundreds of feet**.
- `route_distance` is **statute miles**, not nautical miles.
- `origin` / `destination` are nested objects and are **nullable** as a whole. `origin.code` may be
  a plain location string rather than an airport code for position-only flights.
- `cancelled` does not mean the airline cancelled the flight. The spec: "Flag indicating that the
  flight is no longer being tracked by FlightAware. There are a number of reasons this could happen
  including cancellation by the airline, but that will not always be the case." Do not send a
  "flight cancelled" notification off this flag alone.
- `codeshares` is ICAO-only; IATA codeshares live in the separate `codeshares_iata`.
- `operator_icao`, `ident_icao`, `ident_iata` are not in the schema's `required` list - use
  `.get()`, do not index.
- `links` is nullable as a whole object, not just `links.next`.

### 6. Ident form

If the plan stores or queries IATA idents (`DL1234`), switch to ICAO (`DAL1234`). The spec: "it is
highly recommended to specify ICAO flight ident rather than IATA flight ident to avoid ambiguity
and unexpected results."

### 7. Scriptable: `applyTimerStyle()` on a past date counts up with no sign

If the widget design shows a bare timer for departure, it will keep ticking after departure and
read identically to a countdown. Gate the style on `date > new Date()` and fall back to
`applyOffsetStyle()` or static text.

### 8. Scriptable: per-row tap targets do not work on small or Lock Screen widgets

`WidgetStack.url` is "only supported in medium and large widgets". If the plan has tappable
per-flight rows in a `small` or `accessoryRectangular` widget, that reduces to a single
`ListWidget.url` for the whole widget.

### 9. Pushover: a 200 does not mean the message was accepted

The status code answers whether the request was well-formed, not whether Pushover took it. A
malformed key still returns 4xx, but the reason lives in the JSON `errors` array, and reporting the
status code alone throws away the one sentence that says which key is wrong. Parse the body.

The other correction is the quiet-hours rule: priority `0` is silently demoted to `-1` while the
user is in quiet hours, so a gate change sent at the default priority can arrive silently at 3am.
Only `1` bypasses it.
