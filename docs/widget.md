# Flighter widget for Scriptable

An iOS home screen and Lock Screen widget for the flight tracker. It reads
`GET /api/widget` and draws it. Every display decision is made on the server, so the
script only changes when the layout changes, and when it does the phone picks the new
one up by itself.

## Install

Everything happens on the **Widget** tab of the server's settings page, opened on the
phone. Nothing is typed and nothing is edited.

1. **Get Scriptable.** Free, on the App Store.
2. **Install the script.** The button downloads `Flighter.scriptable`; opening the download
   imports it into Scriptable with its name and icon set. If that does not suit, the same
   step offers the script to copy: in Scriptable, tap `+`, paste, and name the script
   `Flighter`. The name matters, because the next step runs it by name.
3. **Connect this phone.** The button opens Scriptable and runs the script with the
   server's address and the widget token in the URL. The script stores both in the iOS
   Keychain and renders a preview, which is how you check the address works from the
   phone. Neither value is ever written into the script.
4. **Add the widget.**
   - **Home Screen**: long press the wallpaper, tap `+`, choose Scriptable, pick a size,
     then tap the placed widget and set *Script* to Flighter and *When Interacting* to
     *Run Script*.
   - **Lock Screen**: long press the Lock Screen, tap Customise, tap the area under the
     clock, choose Scriptable, and pick the rectangular widget. Set the script the same
     way. iOS 16 or later.

The tab then says when a phone last fetched flights, which is the only way to tell from
the server's side that a widget is actually talking to it.

## When the token changes

**Regenerate** under *Advanced* on the same tab mints a new token. Every phone stops
updating and says *Reconnect needed*; tapping **Connect this phone** again on each one
is the whole repair. The same two words, and the same repair, cover a token that was
never stored and one the server no longer accepts.

## Updating the script

Every time the widget reaches the server it also compares itself with the server's copy
at `/static/flights-widget.js` and replaces itself if they differ, so the next reload
runs the new one. Update the server and the phone follows on its own; tapping Connect
does the same thing at once, and says so.

## Sizes

| Family | What it shows |
| --- | --- |
| `accessoryRectangular` (Lock Screen) | Next flight only, three tight lines. **iOS 16 or later.** |
| Small | Next flight: the number, the route under it, its pill, and its line |
| Medium | Up to three flights, two lines each, each row tappable |
| Large | Same rows, with room to breathe |

Tapping opens the flight's page on your server. On medium and large each row deep-links
to its own flight; small and Lock Screen widgets get a single tap target, which is an iOS
restriction and not a choice made here.

## What it shows

A list. Each flight is two lines: the airline's mark with the flight number and route,
and what the flight is counting to at the far end of that line; then the board's status
pill, one line beside it, and the count itself at the far end of that one.

The line under the pill is where to be, once being somewhere is the question:

| While it is | The line reads |
| --- | --- |
| Days off | `Fri 18 Sep 18:00 EDT` |
| On the day | `TERM 4 · GATE B22 · SEAT 14A` |
| Pushed back, or in the air | `SEAT 14A` |
| Parked | `Baggage claim 7` |
| Cancelled, or lost by the feed | nothing; the pill has said it |

The terminal, the gate and the seat join the line only on the day of the flight, which
is the one stretch where a person is on their way to use them, and the first two are
dashed rather than dropped while the airport has not said: a line that comes and goes as
gates are published is a row that moves under the eye. Where to be leads, so a row too
narrow for the whole line loses the seat rather than the gate.

When it goes is the other end of the row, and never on this line: the board's own words
for the rung ahead - `Departs in`, `Due to land` - with the count under or beside them.
Every word and tone comes from the server; the script picks nothing on its own.

### Whose clock

The time is the one on the phone's own clock, because that is the watch the person
reading it is going to check it against. `18:00 PDT` on a phone in Ottawa is arithmetic,
not information.

So the widget sends the zone it is set to with every request, and the server renders the
time in it - once, and with no second reading of the same instant beside it. A row is
looked at for a second and a half, and a line that states two times states neither. The
zone is not named either, because the clock it is on is the clock in the same hand. A
phone that will not say where it is gets the airport's clock instead, and that one keeps
its zone: it is the one case where the time is not on the reader's own.

The day goes in front whenever the time is not today's **on the phone's clock**. `02:00`
on its own reads as today's, and a flight leaving tomorrow morning would otherwise look
hours overdue all evening.

The pill is the board's, in the board's tones, word for word and with no exceptions. It
used to carry two - "Departed" in place of "Taxiing", and "Scheduled" in place of the
board's "Today" - on the grounds that a widget redrawn every quarter of an hour would
show a ten-minute word after the fact. That traded one wrong reading for a worse one: a
phone saying Departed beside a page saying Taxiing is two answers to one question, and
whoever is holding both has no way to tell which is the stale one. Same flight, same
word, everywhere.

The widget lists the flights the board has on it, in the board's order, and lets each one
go at the moment the board files it under Flown: two hours after it reached the gate, or
was due to.

The airline's mark is fetched once per carrier, from the address the server names, and
kept in Scriptable's documents folder from then on, so it is drawn whether or not the
network is there. The Lock Screen widget goes without it, because iOS draws everything
there in a single tint. A mark that cannot be fetched is left out; the number beside it
already names the airline.

The colours are the web UI's own, light and dark, and follow the phone's appearance.
The Lock Screen widget is drawn in the Lock Screen's own tint, as iOS requires.

## How it stays accurate

iOS decides when a widget actually reloads: `refreshAfterDate` is a hint it can and does
ignore, and it budgets reloads across all widgets on the device. Expect roughly
quarter-hourly in practice. That is why no number on the widget is worked out from the
phone's clock at draw time. Two of them would be wrong within the minute and a quarter
of an hour wrong by the next reload, so both are handed to WidgetKit as dates for it to
tick on its own: the instant a flight is counting to, and the moment the data on screen
arrived. Every other time is a clock face, which is right until the estimate itself
moves, and whoever reads it measures it against their own clock.

A word is not a date, though, and that is the one thing the phone cannot repair for
itself. `Departs in` is drawn once and stays drawn while the count beside it ticks up
past zero, which reads as three minutes to go when the flight is three minutes overdue.
So the server does not ask for its usual cadence when a rung is closer than that: it
asks for the reload at the instant the wording changes, and the row is wrong only for as
long as iOS makes it wait. Otherwise the cadence is the server's own polling cadence for
the closest flight on the list.

The bottom of the widget always says how old what is drawn is - `Updated 04:12 ago`,
counting up as you look at it. The last good response is cached to the Scriptable
documents folder, so if the server is unreachable the widget draws the cached data and
that line reads `Cached` instead of `Updated`, rather than going blank. A rejected token
is the one failure that is never cached over: the widget says so, because no reload will
fix it. If the server says its own data is degraded, because the AeroAPI budget breaker
tripped or polling has stalled, that reason sits on a line above.
