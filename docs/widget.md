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
| Small | Next flight: the number, the route under it, its status pill, the next milestone large, and the card's detail line underneath. While a flight is under way, the heart of its card: the codes with the rule between them, the times, and the count |
| Medium | Up to three flights, one row each, each row tappable. While a flight is under way, its card alone |
| Large | Same rows, with room to breathe. While a flight is under way, its card, a rule, and the other flights' rows under it |

Tapping opens the flight's page on your server. On medium and large each row deep-links
to its own flight; small and Lock Screen widgets get a single tap target, which is an iOS
restriction and not a choice made here.

## The card

While a flight is under way the widget stops being a list and becomes that flight's
card, the one the flight page opens with, drawn a little tighter: the number and the
pill; the two airport codes with the rule between them and the aircraft on it, as far
along as it has got; the time at each end in the card's tone, red once it has slipped
later than booked and green when it came forward, with its zone beside it; the terminal
and gate at each end; and what it counts to. A diverted flight's new airport stands in
red where the code goes, with the booked one small beside it.

"Under way" is the server's call, and it is made by the same rules the page uses. The
card opens once the aircraft has pushed back and stays until the flight leaves the
board, and it opens early for a flight inside the poller's close window, the last three
hours before departure, which is as near as the server itself calls a departure
imminent. A flight still days or hours off keeps its row.

When more than one flight could have the card, an aircraft still moving comes first,
taxiing in included. Then one about to leave. A flight already parked comes last,
because its card has nothing left to say but the belt: on a layover the leg just flown
hands the screen to the leg about to be, the moment it is at the gate and not before,
and keeps it while the next leg is still hours off. The flight with the card is also
the one the small and Lock Screen sizes show, so every size changes over at the same
moment.

The aircraft's place on the rule is the second thing the script works out for itself,
for the same reason as the figure: it moves between reloads. The server hands over
wheels-up and the landing estimate, and the phone puts the aircraft where its own clock
says it is, as the page does between loads.

## What it shows

Each flight is the board's card in miniature: the airline's mark, the flight number and
route, the same status pill the web UI shows ("On time", "Departure delayed", "In the air",
"Arriving late", "Landed" and so on, in the same tone), one line of detail, and on the
right the same milestone the card's footer carries: "Departs in", "Lands in" or "At the
gate in" with a time against it, and "Bags" with the belt once the aircraft is parked. A
flight still days away has no milestone, on the card or here. The detail line is what
matters in that phase: the day and time it leaves while it is days off; the gate and
the seat on the day; nothing once it has pushed back, since the milestone and the belt
say what is left. Every word and tone comes from the server; the script picks nothing
on its own.

The widget lists the flights the board has on it, in the board's order, and lets each one
go at the moment the board files it under Flown: two hours after it reached the gate, or
was due to.

The airline's mark is fetched once per carrier, from the address the server names, and
kept in Scriptable's documents folder from then on, so it is drawn whether or not the
network is there. The Lock Screen widget goes without it, because iOS draws everything
there in a single tint. A mark that cannot be fetched is left out; the number beside it
already names the airline.

The figure beside the milestone is worked out on the phone, because it depends on the
phone's clock. It follows the page's rules: whole days once a day or more
away, then hours and minutes (`1h 05m`), then minutes, `<1m` inside a minute, and
`20m ago` once the instant has passed, at which point "Lands in" turns into "Due to
land" the way it does on the page. There are never seconds.

The colours are the web UI's own, light and dark, and follow the phone's appearance.
The Lock Screen widget is drawn in the Lock Screen's own tint, as iOS requires.

## How it stays accurate

The figure is text drawn at reload, so it is only as fresh as the last reload. iOS
decides when a widget actually reloads: `refreshAfterDate` is a hint it can and does
ignore, and it budgets reloads across all widgets on the device. Expect roughly
quarter-hourly in practice. Within an hour of a milestone the script asks for a reload on
the minute, which is when the figure changes; further out it asks at the cadence the
server names, since the data moves no faster than that.

The last good response is cached to the Scriptable documents folder. If the server is
unreachable the widget draws the cached data with a `Cached HH:MM` marker instead of
going blank. A rejected token is the one failure that is never cached over: the widget
says so, because no reload will fix it. If the server says its own data is degraded,
because the AeroAPI budget breaker tripped or polling has stalled, that reason replaces
the marker.
