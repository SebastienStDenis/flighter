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
| Small | Two flights, three lines each |
| Medium | Up to three flights, two lines each, each row tappable |
| Large | Up to seven of the same rows, with a little more air between them |

The widget tells the server which size it is, and the server sends a list that long. How
many rows fit is the one thing about the layout the server cannot work out for itself,
and a list cut to the smallest size that might be asking is a large widget with its
bottom half empty.

Tapping opens the flight's page on your server. On medium and large each row deep-links
to its own flight; small and Lock Screen widgets get a single tap target for the whole
widget, which is an iOS restriction and not a choice made here. The Lock Screen's opens
the flight it draws, which is the only one it draws. The small size draws two, so its tap
opens the board instead: pointed at either of them it would be the wrong flight half the
time, and both of them are on the board, in the order the square has them.

## What it shows

A list. Each flight is a row of two lines, both of them running the full width of the
widget. The first carries the flight - whose it is when it is not yours, the airline's
mark, the number and the route - and ends with the board's status pill. The second
carries where to be, and ends with what the flight is next due to do and the time it is
due.

Whose it is, is their initial in a disc in the colour the board gives that person, the
same disc the page draws. The disc is squared to the number beside it, so the heading is
the height of its own type, and the letter on it is drawn at three fifths of that -
drawn, into an image the size of the disc, rather than set as a line of type on it.
Centring type in a stack takes a spacer either side of it, and a spacer holds a length of
its own before it gives any room away; two of them ask for more than a disc this size
has, which left the letter with nothing and no letter is drawn small when it has nowhere
to go - it is dropped, and the disc came out bare.

Every row is drawn at one size, on every size of widget. The type sizes are chosen for
the widget rather than measured against the room a line has left over, because a route or
a status that shrinks to fit its own row is a flight drawn larger than the flight above it
for no reason a reader can see - the row at the foot of a large widget with the shortest
number on it, most visibly. The route on the small size was the last of them to give way:
it shares its line with the number there, so a longer number drew the same seven
characters smaller. It is set two points under the number instead, and its arrow comes
from the server without the spaces around it - `JFK→LAX` rather than `JFK → LAX` - which
is the width those two points and the air between the runs were buying.

What still gives on the small size is the pill and the rung beside it: the longest status
and the longest rung do not fit on one line of a 155pt square, and a word cut in half is
worse read than a word read small.

The small size draws its two flights as blocks of three lines, and what the square has
left over after those six lines and the footer goes between the two blocks rather than
under them: they stand apart and fill the widget instead of sitting pinned to the top of
it. Its words keep the same distance from the rounded corner as every other size's, which
they did not while the room was thought to be spare.

The row is the board's card, narrowed. The pill is the card's pill, the places are the
card's places and the end of the row is the card's footer, each drawn on exactly the
rows the card draws it on, because a widget that answers a question the card has already
answered is a second answer to it and the reader has no way to tell which is stale. What
is the widget's own is what the width forces: the card has room to show both ends of a
flight at once and a row has one line, so it shows the end being walked to; the card
heads its places with words where the row has a mark; the card holds an unnamed gate open
with a dash where the row leaves it out; and the card counts down to a rung where the row
states the time it is due. The figures are the same figures either way.

The line under the heading is where to be, for as long as there is anything on the
flight to watch:

| While it is | The line reads |
| --- | --- |
| Days off, with nothing to watch yet | `Fri 18 Sep 18:00 EDT` |
| Still at the gate it leaves from | *plane climbing* `T4 • B22`, *seat* `14A` |
| Pushed back or in the air | *seat* `14A`, *plane landing* `TB • 12` |
| Down, taxiing in or parked | *plane landing* `TB • 12` |
| Called off, or given up on | nothing; the pill has said it |

Every row stands the same height whether or not it has all of that to show. The line
under the heading is drawn on every one of them and holds the height of the tallest thing
it can carry - the time at the far end of it - so a flight with only a date under its
heading, and a flight with nothing at all, take the same room as a flight with a time.
The gap between two rows is a fixed one, and a row that closed up its own spare space
would sit nearer its neighbour than the rest of the column for no reason a reader could
see.

On the large size the gap between two rows is nine points rather than the twelve it was.
Twelve was air the size happened to have rather than air the column needed - beside the
medium's eight it read no differently - and three points off each of six gaps is a
seventh flight, which the size now holds. It is the tightest figure in the layout: seven
rows, the footer and the widget's own inset come to within a point or two of what the
large widget holds on a 6.1in phone, so it is the first figure to put back should a row
ever come out clipped.

The marks are `plane-takeoff`, `plane-landing` and `armchair` from Lucide, which is the
icon set the web UI is drawn with, and they stand where TERM, GATE and SEAT used to. The
line
has room for figures or for labels and not for both, and the labels were most of it
spelling out the one thing nobody has to be told: which of three figures is the gate.
What a mark says instead is which end of the flight the row is naming, which is the half
of it a reader can get wrong. They are carried in the script rather than fetched from the
server, because `T4 • B22` with no plane in front of it is a line read wrong rather than
read short - unlike an airline's mark, which is decoration the number beside it has
already covered.

The terminal and the gate are in the order a boarding pass prints them, and the terminal
keeps the `T` that pass prints and runs it into the figure the way the pass sets it,
because a bare `4` beside a bare `B22` is two figures with nothing to tell them apart.
Between the two of them is a dot: `T4 B22` set with nothing but a space is three runs of
figures separated by two gaps of one width, and the dot says which of those gaps is the
one that divides. A place the airport has not named is left out rather than
dashed: a dash is an empty box, and a row with three boxes at most and two of them empty
says nothing in the space where it says everything. What holds the line still while gates
are published is the mark, which is drawn as soon as one figure lands behind it. Once the
aircraft has left the gate the same three are read the other way about, because the seat
is where the reader is and the terminal and the gate are where they are going. The runs
turn round; what is inside one does not, so a terminal and a gate are the same pair in
the same order at both ends of the flight.

Wheels down, the seat comes off and the far end has the line to itself, where it stays
while the aircraft taxis in and after it parks - and the terminal there is the one the
belt is in. A seat is where the reader is only for as long as they are in it: on the
ground the row has one thing left to say, which is the way out, and a seat number
standing in front of that is a figure they have finished with taking room from the ones
they have not. It comes off a diversion that is down at its alternate for the same
reason, and off a flight the poller closed the book on without ever seeing it land.

When it goes is the other end of the row, and never on this line: the board's own words
for the rung ahead, with the time it is due beside them.

| While it is | The end of the row reads |
| --- | --- |
| Ahead of its time | `Departs 14:40`, `Lands 22:40`, `At the gate 22:15` |
| Past it, with no word that it happened | `Due to depart 14:40`, `Due to land 22:40` |
| Parked, with a belt named | `Baggage claim 7` |
| Parked, with none named yet | nothing, until the airport says which |
| Days off, or called off | nothing, the way the card draws no footer for one |

A flight days out is on a rung - the ladder starts at its departure - but nobody is
waiting on it yet, so neither the card nor the row names it. The pill has already said it
is booked, and the day it leaves is under the heading.

A belt nobody has named yet leaves the end of the row empty rather than dashed. The card
has the width to hold the words with a dash where the carousel goes; a row has one line,
and words on it with nothing to read beside them are the news that there is no news. The
line arrives with the belt.

Every word, tone and figure comes from the server; the script picks nothing on its own
and works nothing out.

### Whose clock

The time at the end of the row is the one on the phone's own clock, because that is the
watch the person reading it is going to check it against. `18:00 PDT` on a phone in
Ottawa is arithmetic, not information. So the widget sends the zone it is set to with
every request, and the server renders that time in it. The zone is not named beside it,
because the clock it is on is the clock in the same hand; the foot of the widget says so
once, for all of them. The day goes in front whenever the time is not today's **on the
phone's clock**: `02:00` on its own reads as today's, and a flight leaving tomorrow
morning would otherwise look hours overdue all evening.

The day a flight leaves, days out, is the one exception, and it is on the airport's
clock with the airport's zone after it. It is the only time on that row - there is no rung
being waited on yet - and the day a flight leaves is the day where it leaves from, so a
bare reading would be taken for the reader's own. A phone that will not say where it is
gets the airport's clock everywhere, which is the one case where a time at the end of a
row is not the reader's and does not say so; there is nothing better to draw.

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
quarter-hourly in practice. That is why nothing on the widget is a countdown, and why
nothing on it moves. A figure counted from the phone's clock at draw time would be wrong
within the minute and a quarter of an hour wrong by the next reload - and wrong in the
flattering direction, which is worse than saying nothing. A stated time is not: a flight
due at 18:40 is due at 18:40 whether the row is read at noon or at seven, and it stays
true for as long as iOS leaves the widget alone.

One word does have to keep up. `Departs 14:40` is drawn once and stays drawn, so at 14:44
it is still saying the flight departs, when what the reader needs to see is that it is
due and has not. So the server does not ask for its usual cadence when a rung is closer
than that: it asks for the reload at the instant the wording changes, and the row is
behind only for as long as iOS makes it wait. Outside that, the cadence is the server's
own polling cadence for the closest flight on the list.

The bottom of the widget says when what is drawn was fetched - `Last updated 04:12` - and
beside it, that every time above it is on the phone's own clock. The last good response
is cached to the Scriptable documents folder, so if the server is unreachable the widget
draws the cached data and that line reads `Cached` instead, rather than going blank. A
rejected token is the one failure that is never cached over: the widget says so, because
no reload will fix it. If the server says its own data is degraded, because the AeroAPI
budget breaker tripped or polling has stalled, that reason sits on a line above.
