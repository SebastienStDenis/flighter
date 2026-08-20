# flighter

A self-hosted replacement for Flighty. It reads flight bookings out of your iCloud
mailbox, tracks them on FlightAware, keeps a Google Calendar honest, and puts a live
countdown on your phone's lock screen. One user, one machine, no App Store.

```
iCloud IMAP  →  extraction  →  bookings  →  AeroAPI polling  →  snapshots  →  change detection
IDLE            JSON-LD                     cadence tightens    append-only    diff last two
                Claude fallback             as departure                       dead band
                review queue                approaches                             │
                                                                         ┌─────────┴─────────┐
                                                                         ▼                   ▼
                                                                   Pushover push     Google Calendar
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

**The mailbox is read, never written.** Everything is fetched with `BODY.PEEK[]`, so
nothing is marked read, flagged or moved: the folder looks exactly the same afterwards as
your phone left it. What has been dealt with is tracked here instead, as a UIDVALIDITY
and the highest UID processed, and the ingest log is keyed on each email's `Message-ID`
so re-filing a message does not make it new again.

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

**Credentials and preferences live in different places, and never both.** A credential is
set once by hand in `.env`, is never handed back out by the UI, and the app never writes
it. Everything else - the public URL, the spend cap, the watched folder, the calendar -
is a preference: it has a working default, it is edited at `/settings`, and the database
is the only place it lives. No value has two homes, so there is never a
question of which one wins.

The one file the app writes for itself is `data/secrets.env`, holding the credentials it
mints rather than asks for: the Google refresh token from the consent flow, and the
widget token generated on first boot. Those keys never appear in `.env` either.

## Prerequisites

Five accounts and an Apple ID you already have, and only three of them involve a form.

### Tailscale

The widget fetches from your phone, and you open flight pages from wherever you happen to
be standing. Both need the service reachable from outside the house, and neither should
mean opening a port on the router or publishing a hostname to the internet.

Create an auth key at
[login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys)
and put it in `TS_AUTHKEY`. The stack joins your tailnet as `flighter`, and every device
signed in reaches it at `https://flighter.<your-tailnet>.ts.net` over a real certificate.
Nothing is exposed to anyone else, which is the reason there is no login page in front of
this app.

Your phone therefore needs the Tailscale app connected. It stays up in the background; if
it drops, the widget shows the last data it had rather than failing loudly.

### iCloud app-specific password

The mail loop signs in to `imap.mail.me.com` as you. With two-factor authentication on -
and it is, for every Apple ID that can reach iCloud Mail - IMAP refuses your Apple ID
password outright, so it needs an app-specific one.

At [appleid.apple.com](https://appleid.apple.com), **Sign-In and Security →
App-Specific Passwords → +**, name it `flighter`, and put the generated
`xxxx-xxxx-xxxx-xxxx` string in `ICLOUD_APP_PASSWORD`. `ICLOUD_EMAIL` is the address you
sign in with.

> Changing your Apple ID password **revokes every app-specific password**, this one
> included. `flighter check` is what tells you that is what happened: generate a new one,
> put it in `.env`, and restart.

The folder to watch is a preference, `INBOX` by default, and only one connection is ever
held: iCloud allows about five per account, and your phone and your Mac are already using
some of them.

### FlightAware AeroAPI key

Sign up for the **Personal** tier at
[flightaware.com/aeroapi/signup/personal](https://www.flightaware.com/aeroapi/signup/personal).
It is free up to $5/month of usage, rate-limited to 10 result sets per minute, and
licensed for personal use only.

> Read the pricing page before you enable anything beyond this service. The tier above
> Personal has a $100/month minimum with no free allowance, and FlightAware provides no
> cap of its own. The monthly cap on the settings page defaults to `$4.00` and stops all
> polling when month-to-date estimated spend passes it. `/health` shows the running total.

### Google OAuth client (Calendar)

One project, one client. A five-minute setup you do once, and the only Google value you
ever paste is the client id and secret - the refresh token is minted by the app.

1. At [console.cloud.google.com](https://console.cloud.google.com), create a project.
2. **APIs & Services → Library** → enable the **Google Calendar API**.
3. **APIs & Services → OAuth consent screen**: set it up for **External** users, then set
   the publishing status to **In production**.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID → Web
   application**. Add `https://flighter.<your-tailnet>.ts.net/settings/google/callback`
   as an authorised redirect URI - the settings page prints the exact string. Copy the
   client id and secret into `.env`.

Step 3 is not optional. An app left in **Testing** issues refresh tokens that expire after
**7 days**, and the service will silently stop working every week. Publishing does not
require Google's verification review; you will see a one-time "Google hasn't verified this
app" screen (Advanced → Go to …), and Google exempts apps used only by their author.

### Anthropic API key

From [console.anthropic.com](https://console.anthropic.com). It is only the fallback: any
email carrying schema.org `FlightReservation` JSON-LD is parsed exactly and for free, and
most airlines embed it because Gmail and Outlook read it. The model is for the ones that
do not.

### Pushover

Pushes reach the phone through [Pushover](https://pushover.net), which is hosted, so
there is no notification server in the stack to keep alive.

1. Sign up at [pushover.net](https://pushover.net). Your **user key** is on the dashboard
   you land on; that is `PUSHOVER_USER_KEY`.
2. Register this service as an application at
   [pushover.net/apps/build](https://pushover.net/apps/build) - a name is all it asks
   for. The **API token** it hands back is `PUSHOVER_TOKEN`.
3. Install the Pushover app on the phone and sign in as the same user. That is what
   actually rings.

Sending is free, at 10,000 messages a month per application, which a flight tracker
never comes near. The phone app is the cost: free for 30 days, then a one-time purchase
of around $5 per platform, with no subscription behind it.

## Run it

```sh
git clone https://github.com/sebastienstdenis/flighter
cd flighter
cp .env.example .env
$EDITOR .env
docker compose pull          # fetch the published image instead of compiling it here
docker compose up -d
```

That is the whole install. On first boot the app runs its own migrations, seeds the
airport table with its timezones, generates the widget token, and starts serving.

Open `https://flighter.<your-tailnet>.ts.net/settings` and finish there:

1. Set the **public base URL** - the page offers the address you opened it on.
2. **Connect Google**, once. Sign in, accept the calendar scope, and the app stores the
   refresh token and creates a calendar called *Flights* to write into.
3. **Run checks**. It exercises Postgres, AeroAPI, iCloud, Calendar and Pushover in turn
   and names the broken one, which is the question you will actually have.

Then either add a flight by hand or let the mail loop find one.

Pushing to `main` publishes a `linux/amd64` + `linux/arm64` image to
`ghcr.io/sebastienstdenis/flighter:latest`, so updating the home stack is:

```sh
docker compose pull && docker compose up -d
```

The package inherits this repository's visibility, so it pulls anonymously with no
`docker login` on the desktop. Publishing is gated behind a job that re-runs lint, types
and tests, so a commit that fails CI never ships as `:latest`.

To build locally instead of pulling, `docker compose build` still works from a checkout.

To pick up flights already sitting in your mailbox:

```sh
docker compose exec app flighter backfill --days 30
```

### Running from a checkout

```sh
uv sync --all-groups
uv run flighter serve
```

`serve` migrates and seeds on the way up, so a fresh database needs nothing else. It
writes its minted credentials to `./data/secrets.env` rather than the container volume.

## Commands

| Command | Purpose |
|---|---|
| `flighter serve` | The API, the poll worker and the mail loop |
| `flighter migrate` | Apply database migrations; `serve` already does this on boot |
| `flighter seed-airports` | Load the airport table and its IANA timezones; likewise |
| `flighter backfill --days 30` | Ingest recent mail once |
| `flighter poll` | One polling pass, then exit |
| `flighter check` | Exercise every external dependency |

## The widget

See [`widget/README.md`](widget/README.md). Short version: install Scriptable, copy in
`widget/flights-widget.js`, run it once in the app to store the widget token from
`/settings` in the Keychain, then add it to your home and lock screens.

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
