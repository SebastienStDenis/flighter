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
| Small | Next flight, its status pill, the next milestone large, gate or carousel underneath |
| Medium | Up to three flights, one row each, each row tappable |
| Large | Same rows with more room for the progress bar |

Tapping opens the flight's page on your server. On medium and large each row deep-links
to its own flight; small and Lock Screen widgets get a single tap target, which is an iOS
restriction and not a choice made here.

## What it shows

Each flight is the board's card in miniature: the flight number and route, the same
status pill the web UI shows ("On time", "Departure delayed", "In the air", "Arriving
late", "Landed" and so on, in the same tone), the gate or carousel, and on the right the
next milestone with a time against it: "Departs in", "Lands in", "At the gate in", or
"Scheduled" with a whole-day figure while the flight is still days away. Every word and
tone comes from the server; the script picks nothing on its own.

The figure beside the milestone is the one thing the script works out, because it
depends on the phone's clock. It follows the page's rules: whole days once a day or more
away, then hours and minutes (`1h 05m`), then minutes, `<1m` inside a minute, and
`20m ago` once the instant has passed. There are never seconds.

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
