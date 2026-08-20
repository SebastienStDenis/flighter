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

**Everything is configured in the app.** There is no file to fill in before first boot.
You start the container, open `/settings`, and type your Apple ID, your FlightAware key
and your Pushover keys into the Connections tab; each one takes effect immediately, with
no restart. Credentials are stored in `data/secrets.env`, which the app writes with mode
`0600` and never reads back out into a page: the tab shows whether each connection is
set and offers a box to replace it, and **Forget** is the only way to clear one.

Preferences - the public URL, the spend cap, the import flag, the calendar - live in the
database instead, because they are worth diffing and backing up alongside the flights.
No value has two homes, so there is never a question of which one wins.

The environment is still read, and outranked. Setting `AEROAPI_KEY` in the container's
environment or in a `.env` beside `docker-compose.yml` seeds it for a deployment that has
never had one typed in; the moment somebody saves one on the settings page, that is the
value, and a container restarted with the old one in its environment does not undo it.
`DATABASE_URL` is the exception and stays an environment value with a working default:
the app cannot read a setting out of a database it has not opened yet.

**All of the state is one file in one directory.** `data/` holds the SQLite database and
that secrets file, and nothing outside it survives the container. One volume to mount,
one thing to back up, and one thing to delete to start over. SQLite rather than a
database server because there is one writer, a few hundred rows a year, and no query here
that a server would answer any faster - and a second container is a second thing to
upgrade, watch and restore.

## What you need

Four accounts and an Apple ID you already have, and only three of them involve a form.
Collect these before you start, or collect them with the app already running: every one
of them is typed into **Settings &rarr; Connections**, and nothing has to be in place
before the first boot.

### iCloud app-specific password

The mail loop signs in to `imap.mail.me.com` and the calendar sync signs in to
`caldav.icloud.com`, both as you, and **one app-specific password covers both**. With
two-factor authentication on - and it is, for every Apple ID that can reach iCloud Mail -
neither service accepts your Apple ID password, so an app-specific one is not optional.

At [appleid.apple.com](https://appleid.apple.com), **Sign-In and Security →
App-Specific Passwords → +**, name it `flighter`, and paste the generated
`xxxx-xxxx-xxxx-xxxx` string into the **iCloud** card on the Connections tab, along with
the address you sign in with.

> Changing your Apple ID password **revokes every app-specific password**, this one
> included. **Run checks** is what tells you that is what happened: generate a new one,
> paste it into the same box and save. Checks sign in on the new password straight away,
> and the mail loop drops its connection and picks it up on its next pass, so there is
> nothing to restart.

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

Something that fails for a passing reason - the model, iCloud, the network - is tried
again after two minutes and again after ten. If it still fails, your phone is told once
and the email is set aside: it keeps its flag, so it is still where you left it in Mail,
and it is listed on `/health` with a **Try again** button. Unflagging it in Mail drops it.

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

### A calendar of its own

Open the Calendar app on any of your devices and make a new iCloud calendar - call it
`Flights`, or anything else - then pick it from the list on `/settings`. The list comes
from your account over CalDAV, so there is no name to type and nothing to spell wrong,
and renaming the calendar later does not break anything: what is stored is the calendar
itself rather than what it is called.

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
licensed for personal use only. The key goes in the **FlightAware** card on the
Connections tab.

> Read the pricing page before you enable anything beyond this service. The tier above
> Personal has a $100/month minimum with no free allowance, and FlightAware provides no
> cap of its own. The monthly cap on the settings page defaults to `$4.00` and stops all
> polling when month-to-date estimated spend passes it. `/health` shows the running total.

### Anthropic API key

From [console.anthropic.com](https://console.anthropic.com), into the **Anthropic** card
on the Connections tab. It is the one connection that is genuinely optional: any email
carrying schema.org `FlightReservation` JSON-LD is parsed exactly and for free, and most
airlines embed it because Gmail and Outlook read it. The model is for the ones that do
not, and leaving this empty costs you only those emails.

### Pushover

Pushes reach the phone through [Pushover](https://pushover.net), which is hosted, so
there is no notification server in the stack to keep alive.

1. Sign up at [pushover.net](https://pushover.net). Your **user key** is on the dashboard
   you land on.
2. Register this service as an application at
   [pushover.net/apps/build](https://pushover.net/apps/build) - a name is all it asks
   for - and copy the **API token** it hands back.
3. Install the Pushover app on the phone and sign in as the same user. That is what
   actually rings.

Both go in the **Pushover** card on the Connections tab.

Sending is free, at 10,000 messages a month per application, which a flight tracker
never comes near. The phone app is the cost: free for 30 days, then a one-time purchase
of around $5 per platform, with no subscription behind it.

## Run it

```sh
git clone https://github.com/sebastienstdenis/flighter
cd flighter
docker compose pull          # fetch the published image instead of compiling it here
docker compose up -d
```

There is nothing to fill in first. That is the whole install: one container, one volume,
one port. On first boot the app runs its own migrations, seeds the airport table with its
timezones, generates the widget token, and starts serving on port 8000 of the host.

The app has no login of its own. Anything that can reach the host on that port can read
and edit your flights, so it relies on the machine it runs on not being reachable from
the internet.

Open `http://<host>:8000/settings`. It opens on **Connections**, which lists what is still
to do, in order. Work down it:

1. **iCloud.** Your Apple ID and the app-specific password you made above. One password
   covers both the mailbox this reads and the calendar it writes.
2. **FlightAware.** The AeroAPI key. This is where every gate, delay and diversion comes
   from, so nothing is tracked until it is set.
3. **Pushover.** The application token and your user key, which is how the phone is told.
4. **Run checks**, at the bottom of the same tab. It exercises the database, AeroAPI,
   iCloud mail, iCloud Calendar and Pushover in turn and names the broken one, which is
   the question you will actually have. The mail check says how many messages are
   carrying the flag and waiting; the Pushover check sends a real push.

Each card saves on its own and is live in the running app at once: the next poll uses a
new AeroAPI key, the next push a new Pushover token, and the mail loop drops its
connection and signs in again on a new password at the end of its current pass. So you can
type one in and prove it with **Run checks**, which signs in there and then, before moving
to the next. Nothing you type is ever shown back: a
card that is already set shows an empty box that means *leave this alone*, and **Forget**
is what clears it.

Then finish on **Preferences**:

1. Set the **public base URL**. It is the address of the *host*, and it is read on your
   phone rather than on the machine serving it - calendar links, pushes and the widget
   all use it - so it has to be an address that works when you are not at home. If the
   host is on your tailnet, its tailnet name is that address. The page offers whatever
   you opened it on.
2. Pick the **calendar** you made in the Calendar app from the list your account offers.
   Leave it unset and nothing is written to any calendar.
3. Pick the **import flag** colour, `grey` by default, and decide not to use that colour
   for anything else.

Then either add a flight by hand or flag a booking email.

If you would rather a deployment come up already knowing a credential - a rebuild you do
not want to reconfigure by hand - put it in the container's environment, either through
an `environment:` block or a `.env` beside `docker-compose.yml`, using the names
`ICLOUD_EMAIL`, `ICLOUD_APP_PASSWORD`, `AEROAPI_KEY`, `ANTHROPIC_API_KEY`,
`PUSHOVER_TOKEN` and `PUSHOVER_USER_KEY`. That only ever seeds: anything saved on the
settings page wins over it from then on, and the file is optional, so a fresh clone
without one starts fine.

Pushing to `main` publishes a `linux/amd64` + `linux/arm64` image to
`ghcr.io/sebastienstdenis/flighter:latest`, so updating the home stack is:

```sh
docker compose pull && docker compose up -d
```

The package inherits this repository's visibility, so it pulls anonymously with no
`docker login` on the desktop. Publishing is gated behind a job that re-runs lint, types
and tests, so a commit that fails CI never ships as `:latest`.

To build locally instead of pulling, `docker compose build` still works from a checkout.

The named `data` volume needs no setup. If you would rather keep the state in the
checkout, point it at `./data:/app/data` and give that directory to the user the
container runs as: `chown 10001:10001 data`.

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

`serve` migrates and seeds on the way up, so a fresh checkout needs nothing else: open
`http://127.0.0.1:8000/settings` and fill it in there, exactly as in the container. It
writes `./data/flighter.db` and `./data/secrets.env` into the checkout rather than into
the container volume; `flighter serve --host 0.0.0.0` if you want it off the loopback.

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
diffing, calendar event bodies, widget payloads, and the column that keeps every stored
instant in UTC. Migrations are verified separately in CI, forwards and backwards.

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

`scripts/backup.sh` writes a consistent copy of the database to `data/backups/` and keeps
two weeks of them. It uses SQLite's `VACUUM INTO` rather than copying the file, because
the app is running and a copy of a file being written to is not a backup. Wire it to the
host's crontab:

```
0 4 * * * docker compose -f /path/to/docker-compose.yml exec -T app /app/scripts/backup.sh
```

Those copies live in the same volume as the database they came from, so they survive a
bad migration but not a lost disk. Getting them off the machine is a job for whatever
already backs the machine up.

**A copy of the `data` volume is a copy of your credentials**: `data/secrets.env` holds
your Apple ID and its app-specific password, your AeroAPI key, your Anthropic key, your
Pushover token and user key, and the widget token, all in plain text. The database beside
it holds none of them - `scripts/backup.sh` copies only the database, so its output in
`data/backups/` carries your flights and your preferences and no secret - but anything
that archives the whole volume is archiving the lot, so treat it the way you would treat
the password itself.
