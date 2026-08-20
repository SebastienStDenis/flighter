# flighter

A self-hosted replacement for Flighty. You flag a booking email grey; it reads the flight
out, tracks it on FlightAware, keeps an iCloud calendar honest, and puts a live countdown
on your phone's lock screen. One user, one machine, no App Store.

```
you mark an email  →  iCloud IMAP  →  extraction  →  bookings  →  AeroAPI polling  →  change detection
flag it the           IDLE + sweep    JSON-LD                     cadence tightens    diff last two
import colour         every mailbox   Claude fallback             as departure        snapshots
      │               done → unflag   review queue                approaches          dead band
      ▼               failed → stays                                                      │
Pushover push                                                                 ┌───────────┴───────┐
imported, or why not                                                          ▼                   ▼
                                                                        Pushover push     iCloud Calendar
                                                                              │                 CalDAV
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

**You say which emails are flights.** Nothing scans your inbox and nothing guesses. You
give a booking email one colour of flag from whichever device is in your hand, and that
message - only that message - is imported. Deciding it yourself is simpler and more
predictable than a heuristic deciding for you, and it removes false positives as a
category rather than tuning them down. A flag rather than a mailbox because the email
never has to move: it stays filed wherever you already keep it, before and after.

**The mark is the queue.** There is no cursor and no window to re-scan: whatever carries
the flag is what is still to do. A message that imported has its flag taken off and is
left exactly where it stands, which is what finishing means; a message that failed keeps
its flag, which is what retrying means. Either way your phone is told - what was added, or
what went wrong and a link straight back to the email - and a message that has already
been reported as failed is never reported again, because a push every five minutes is
worse than the bug it names.

Bodies are read with `BODY.PEEK[]`, so no email is ever silently marked as read, and the
ingest log is keyed on each email's `Message-ID` so re-filing a message does not make it
new again.

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
it. Everything else - the public URL, the spend cap, the import flag, the calendar -
is a preference: it has a working default, it is edited at `/settings`, and the database
is the only place it lives. No value has two homes, so there is never a
question of which one wins.

The one file the app writes for itself is `data/secrets.env`, holding the credentials it
mints rather than asks for: today just the widget token generated on first boot. That key
never appears in `.env` either.

## Prerequisites

Four accounts and an Apple ID you already have, and only three of them involve a form.

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

The mail loop signs in to `imap.mail.me.com` and the calendar sync signs in to
`caldav.icloud.com`, both as you, and **one app-specific password covers both**. With
two-factor authentication on - and it is, for every Apple ID that can reach iCloud Mail -
neither service accepts your Apple ID password, so an app-specific one is not optional.

At [appleid.apple.com](https://appleid.apple.com), **Sign-In and Security →
App-Specific Passwords → +**, name it `flighter`, and put the generated
`xxxx-xxxx-xxxx-xxxx` string in `ICLOUD_APP_PASSWORD`. `ICLOUD_EMAIL` is the address you
sign in with.

> Changing your Apple ID password **revokes every app-specific password**, this one
> included. `flighter check` is what tells you that is what happened: generate a new one,
> put it in `.env`, and restart.

Only one connection is ever held: iCloud allows about five per account, and your phone and
your Mac are already using some of them.

### A flag colour of its own

Pick one of Apple Mail's flag colours, give it to this app, and never use it for anything
else. The default is **grey**, the last colour in the row and the one nobody reaches for
by habit. There is nothing to create: the flag already exists on every device you are
signed in on.

To import a flight, flag the email: on the iPhone, **open it, tap the More button, tap
Flag**, then pick the colour. On the Mac, select it and choose the colour from the **flag
button in the toolbar**. Within a few seconds you get a push naming the flight and linking
to its page here, and the flag comes off - the email itself does not move. If nothing could
be read out of it you get a push saying so, with a link that opens the email in Mail, and
the flag stays on so the next pass tries again - except when there was simply no flight in
it, which is an answer that will not change, so that one is unflagged too.

Rename the flag to `flighter` in Mail on the Mac if you want your own word for it -
**click the flag name in the sidebar, click it again, and type**. That name is a label on
that Mac and never leaves it; the colour is the whole of what travels, and the colour is
what this watches for.

> **Red is not on the list.** Apple encodes a flag's colour as up to three IMAP keywords,
> and red is the index they all leave unset - so a red flag is indistinguishable from a
> plain flag set by anything else. The other six are unambiguous. One caveat, from Apple's
> own documentation: in the iOS Mail *categories* view, "you cannot flag emails that have
> been categorized as Promotions, Updates, or Transactions", and airline confirmations are
> Transactions. The current iPhone guide describes flagging them anyway; either way, List
> View has no such restriction. The mapping, the sources and the disagreement are in
> [`docs/api-research.md`](docs/api-research.md) §6.

### A calendar called Flights

Open the Calendar app on any of your devices and make a new iCloud calendar. Call it
`Flights`, or anything else, and type that name into `/settings`. The app finds it by
name - it never guesses a URL, because iCloud serves every account from a different
cluster under a different numeric principal.

It has to be made by hand: iCloud does not let a CalDAV client create a calendar, and
writing flights into your main calendar would mean undoing a bad sync one event at a
time instead of by deleting one calendar.

> Apple can also put flights on your calendar by itself, from the same emails. Those
> land in the read-only **Siri Suggestions** calendar under *Other* rather than in a real
> one, so they show up beside this app's entries rather than duplicating them inside the
> same calendar. If the pair annoys you, turn the suggestions off: on a Mac, **System
> Settings → Apple Intelligence & Siri → Siri Suggestions & Privacy → Calendar**, and
> switch off *Show Siri Suggestions in App*; on iOS the same switch is under
> **Settings → Apps → Calendar → Siri**.

### FlightAware AeroAPI key

Sign up for the **Personal** tier at
[flightaware.com/aeroapi/signup/personal](https://www.flightaware.com/aeroapi/signup/personal).
It is free up to $5/month of usage, rate-limited to 10 result sets per minute, and
licensed for personal use only.

> Read the pricing page before you enable anything beyond this service. The tier above
> Personal has a $100/month minimum with no free allowance, and FlightAware provides no
> cap of its own. The monthly cap on the settings page defaults to `$4.00` and stops all
> polling when month-to-date estimated spend passes it. `/health` shows the running total.

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
2. Type the **calendar name** you made in the Calendar app, `Flights` or whatever you
   called it. Leave it empty and nothing is written to any calendar.
3. Pick the **import flag** colour, `grey` by default, and decide not to use that colour
   for anything else.
4. **Run checks**. It exercises Postgres, AeroAPI, iCloud mail, iCloud Calendar and
   Pushover in turn and names the broken one, which is the question you will actually
   have. The mail check says how many messages are carrying the flag and waiting.

Then either add a flight by hand or flag a booking email.

Pushing to `main` publishes a `linux/amd64` + `linux/arm64` image to
`ghcr.io/sebastienstdenis/flighter:latest`, so updating the home stack is:

```sh
docker compose pull && docker compose up -d
```

The package inherits this repository's visibility, so it pulls anonymously with no
`docker login` on the desktop. Publishing is gated behind a job that re-runs lint, types
and tests, so a commit that fails CI never ships as `:latest`.

To build locally instead of pulling, `docker compose build` still works from a checkout.

A flag set on mail in the inbox is picked up within seconds; every mailbox is swept again
every few minutes regardless, since IDLE only ever reports the one it is watching.
To run a sweep on the spot instead of waiting:

```sh
docker compose exec app flighter import
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
| `flighter import` | Import every marked email now, then exit |
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

### The stylesheet

The screens are built from [Basecoat](https://basecoatui.com), which is shadcn/ui as
plain HTML classes, so a card or a badge is the library's rather than something invented
here. `styles/app.css` is the source; `src/flighter/static/flighter.css` is the compiled
result and is committed, so a checkout and the image both run without Node. After editing
a template or the source stylesheet:

```sh
npm install
npm run build
```

The image is still pure Python: it copies the compiled file and never runs a build.

## Backups

`scripts/backup.sh` runs `pg_dump` into a named volume and keeps two weeks. Wire it to the
host's crontab:

```
0 4 * * * docker compose -f /path/to/docker-compose.yml exec -T db /usr/local/bin/backup.sh
```
