# flighter

A self-hosted replacement for Flighty. It reads flight bookings out of your Gmail, tracks
them on FlightAware, keeps a Google Calendar honest, and puts a live countdown on your
phone's lock screen. One user, one machine, no App Store.

```
Gmail  →  extraction  →  bookings  →  AeroAPI polling  →  snapshots  →  change detection
          JSON-LD                     cadence tightens    append-only    diff last two
          Claude fallback             as departure                       dead band
          review queue                approaches                             │
                                                                   ┌─────────┴─────────┐
                                                                   ▼                   ▼
                                                              ntfy push        Google Calendar
                                                                   │
                                             web UI  ←─────────────┘─────→  Scriptable widget
                                             flight detail                  live countdown
```

The point is not that a phone can show a flight number. The point is that the gate change
arrives while you are still at the wrong end of the terminal.

## Why it is built this way

**Bookings and observations are separate.** A booking is what you or an email asserts, and
you can edit it. A snapshot is what FlightAware saw, is append-only, and is never
corrected. Change detection is a diff of the newest two snapshots, so rewriting a snapshot
would erase the very event it should have raised.

**Passengers are first-class.** Tracking your sister's flight into JFK so you know when to
leave for the airport is a normal thing to want, and it is not a note field.

**Timezones are resolved from the airport, never from the email.** Airlines state offsets
wrong often enough that trusting them is a bug waiting for a date-line flight. Every
airport carries an IANA zone, every instant in the database is UTC, and every time on
screen is labelled with the zone it is local to. `18:40 EDT (JFK)` is unambiguous;
`18:40` is a missed flight.

**The API budget is a circuit breaker, not a dashboard.** AeroAPI's Personal tier is free
up to $5/month and the next tier up carries a **$100/month minimum**. FlightAware itself
offers no spending cap on v4, so the only thing standing between a polling bug and a
hundred-dollar bill is this service: polling stops dead at a configurable month-to-date
estimate, pushes an alert, and shows a banner until the next month starts.

Billing is per *page of up to 15 flight records*, not per call, so every request is sent
with `max_pages=1`. At $0.005 per page the free allowance is exactly 1,000 polls a month.

**The widget countdown is a real timer.** It is drawn with Scriptable's `applyTimerStyle`,
so it ticks continuously, offline, without the widget refreshing. A countdown rendered as
a pre-computed string is wrong within a minute of being drawn, which is worse than
useless in an airport.

## Prerequisites

Four credentials. Have them before you start.

### FlightAware AeroAPI key

Sign up for the **Personal** tier at
[flightaware.com/aeroapi/signup/personal](https://www.flightaware.com/aeroapi/signup/personal).
It is free up to $5/month of usage, rate-limited to 10 result sets per minute, and
licensed for personal use only.

> Read the pricing page before you enable anything beyond this service. The tier above
> Personal has a $100/month minimum with no free allowance, and FlightAware provides no
> cap of its own. `AEROAPI_MONTHLY_CAP_USD` defaults to `4.00` and stops all polling when
> month-to-date estimated spend passes it. `/health` shows the running total.

### Google OAuth client (Gmail + Calendar)

One project, one client, both APIs. A five-minute setup you do once.

1. At [console.cloud.google.com](https://console.cloud.google.com), create a project.
2. **APIs & Services → Library** → enable the **Gmail API** and the **Google Calendar API**.
3. **APIs & Services → OAuth consent screen**: set it up for **External** users, then set
   the publishing status to **In production**.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID → Desktop app**.
   Copy the client id and secret.

Step 3 is not optional. An app left in **Testing** issues refresh tokens that expire after
**7 days**, and the service will silently stop working every week. Publishing does not
require Google's verification review; you will see a one-time "Google hasn't verified this
app" screen (Advanced → Go to …), and Google exempts apps used only by their author.

> **Turn off Google Calendar's own "Events from Gmail"** (Calendar → Settings → Events
> from Gmail). If you leave it on, every flight lands on your calendar twice: once from
> Google and once from here.

### Anthropic API key

From [console.anthropic.com](https://console.anthropic.com). It is only the fallback: any
email carrying schema.org `FlightReservation` JSON-LD is parsed exactly and for free, and
most airlines embed it because Gmail and Outlook read it. The model is for the ones that
do not.

### A Cloudflare Tunnel

The widget fetches from your phone on cellular, and you open flight detail pages from
wherever you happen to be standing. Both need the service reachable from outside your
house, and neither should require opening a port on your router. Create a tunnel in the
Cloudflare Zero Trust dashboard, point a hostname at `http://app:8000`, and put the token
in `TUNNEL_TOKEN`.

## Run it

```sh
git clone https://github.com/sebastienstdenis/flighter
cd flighter
cp .env.example .env
$EDITOR .env
docker compose pull          # fetch the published image instead of compiling it here
docker compose up -d
docker compose exec app flighter migrate
docker compose exec app flighter seed-airports
docker compose exec app flighter check
```

Pushing to `main` publishes a `linux/amd64` + `linux/arm64` image to
`ghcr.io/sebastienstdenis/flighter:latest`, so updating the home stack is:

```sh
docker compose pull && docker compose up -d
```

The package inherits this repository's visibility, so it pulls anonymously with no
`docker login` on the desktop. Publishing is gated behind a job that re-runs lint, types
and tests, so a commit that fails CI never ships as `:latest`.

To build locally instead of pulling, `docker compose build` still works from a checkout.

`check` exercises Postgres, AeroAPI, Gmail, Google Calendar and ntfy in turn and tells you
which one is broken, which is the question you will actually have.

Then open the hostname your tunnel publishes. Add a passenger for yourself, and either add
a flight by hand or let the mail loop find one.

To pick up flights already sitting in your mailbox:

```sh
docker compose exec app flighter backfill --days 30
```

### Running from a checkout

```sh
uv sync --all-groups
uv run flighter migrate
uv run flighter serve
```

## Commands

| Command | Purpose |
|---|---|
| `flighter serve` | The API, the poll worker and the mail loop |
| `flighter migrate` | Apply database migrations |
| `flighter seed-airports` | Load the airport table and its IANA timezones |
| `flighter backfill --days 30` | Ingest recent mail once |
| `flighter poll` | One polling pass, then exit |
| `flighter check` | Exercise every external dependency |

## The widget

See [`widget/README.md`](widget/README.md). Short version: install Scriptable, copy in
`widget/flights-widget.js`, run it once in the app to store your `WIDGET_TOKEN` in the
Keychain, then add it to your home and lock screens.

## Development

```sh
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest -q
```

The test suite is deliberately database-free and network-free. Everything worth testing
here is a pure function over data: timezone normalisation, poll cadence, snapshot
diffing, calendar event bodies, widget payloads. Migrations are verified separately in CI
against a real Postgres, forwards and backwards.

## Backups

`scripts/backup.sh` runs `pg_dump` into a named volume and keeps two weeks. Wire it to the
host's crontab:

```
0 4 * * * docker compose -f /path/to/docker-compose.yml exec -T db /usr/local/bin/backup.sh
```
