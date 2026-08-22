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
and under them the board's status pill and one line beside it.

The line is the next thing worth knowing, and it always leads with a time:

| While it is | The line reads |
| --- | --- |
| Days off | `Fri 18 Sep 18:00 EDT` |
| Leaving tomorrow | `Tomorrow 02:00 EDT · Seat 14A` |
| On the day | `14:40 EDT · Gate B22 · Seat 14A` |
| Pushed back, or in the air | `Lands 18:15 EDT · 15:15 PDT` |
| On the ground | `At the gate 01:15 EDT · 22:15 PDT` |
| Parked | `Baggage claim 7` |
| Cancelled, or lost by the feed | nothing; the pill has said it |

The gate and the seat join the line only on the day of the flight, which is the one
stretch where a person is on their way to use them. The time leads, so a row too narrow
for the whole line loses the seat rather than the flight. Every word and tone comes from
the server; the script picks nothing on its own.

### Whose clock

The time is the one on the phone's own clock, because that is the watch the person
reading it is going to check it against. `Lands 15:15 PDT` on a phone in Ottawa is
arithmetic, not information.

So the widget sends the zone it is set to with every request, and the server renders
every time in it. Where the airport's clock reads differently, it follows on the same
line: `Lands 18:15 EDT · 15:15 PDT` is when it lands for you, and what the clock will
say when you step off. Where the two read the same, which is nearly always true of a
departure, there is only one time. A phone that will not name its zone gets the
airport's clock alone, which is right, just harder work.

Neither time ever loses its zone, and the day goes in front whenever the time is not
today's **on the phone's clock**, since that is the clock the line leads with. `02:00`
on its own reads as today's, and a flight leaving tomorrow morning would otherwise look
hours overdue all evening.

The pill is the board's, in the board's tones, with two exceptions the board does not
need. The board says "Taxiing" for the ten minutes between pushback and wheels up; a
widget redrawn every quarter of an hour would show that word after the fact as often as
not, so here it says "Departed", which stays true until the gate at the other end. And
the board names the day in the pill for a flight the feed has not picked up yet; here
the day is already on the line, so the pill says "Scheduled".

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
quarter-hourly in practice. That is why nothing on the widget is counted from the
phone's clock. A countdown is wrong a minute after it is drawn and a quarter of an hour
wrong by the next reload; a time at the airport is right until the estimate itself
moves, and whoever reads it measures it against their own clock. For the same reason
there is no "due" wording once a time has passed: `Lands 22:40` read at 22:50 says so on
its own. The script asks for a reload at the cadence the server names, which is the
server's own polling cadence for the closest flight on the list.

The last good response is cached to the Scriptable documents folder. If the server is
unreachable the widget draws the cached data with a `Cached HH:MM` marker instead of
going blank. A rejected token is the one failure that is never cached over: the widget
says so, because no reload will fix it. If the server says its own data is degraded,
because the AeroAPI budget breaker tripped or polling has stalled, that reason replaces
the marker.
