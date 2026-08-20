# Flights widget for Scriptable

An iOS home screen and Lock Screen widget for the flight tracker. It reads
`GET /api/widget` and draws it. Every display decision is made on the server, so the
script only changes when the layout changes.

## Install

1. Install [Scriptable](https://apps.apple.com/app/scriptable/id1405459188) from the App
   Store. It is free.
2. Copy `flights-widget.js` onto the phone. AirDrop it, put it in
   `iCloud Drive/Scriptable/`, or paste the contents into a new script in the app.
   The script must be named something you can find in the widget picker; `Flights` is
   fine.
3. Edit the first line of configuration:

   ```js
   const API = "http://your-host.your-tailnet.ts.net:8000"; // no trailing slash
   ```

   Use the same value as the public base URL on the server's settings page. The phone
   reaches the machine over your tailnet, so the Tailscale app has to be connected for
   the widget to refresh. If it drops, the widget draws the last data it had rather than
   failing loudly.
4. Run the script once inside the Scriptable app. It prompts for the widget token, which
   is printed at the bottom of the server's settings page, and stores it in the iOS
   Keychain. The token is never
   written into the script and never leaves the Keychain.
   After saving it, the same run renders a preview, which is how you check the hostname
   is right.
5. Add the widget:
   - **Home Screen**: long press the wallpaper, tap `+`, choose Scriptable, pick a size,
     then tap the placed widget and set *Script* to your script and *When Interacting* to
     *Run Script*.
   - **Lock Screen**: long press the Lock Screen, tap Customise, tap the area under the
     clock, choose Scriptable, and pick the rectangular widget. Set the script the same
     way.

To change the token later, run the script in-app after deleting the `flighter-token`
entry, or long press the script and use Scriptable's own settings.

## Sizes

| Family | What it shows |
| --- | --- |
| `accessoryRectangular` (Lock Screen) | Next flight only, three tight lines. **iOS 16 or later.** |
| Small | Next flight, large countdown, gate or carousel underneath |
| Medium | Up to three flights, one row each, each row tappable |
| Large | Same rows with more room for the progress bar |

Tapping opens the flight's page on your server. On medium and large each row deep-links
to its own flight; small and Lock Screen widgets get a single tap target, which is an iOS
restriction and not a choice made here.

## How it stays accurate

The countdown is a system timer element (`addDate()` + `applyTimerStyle()`), not text the
script formatted. It ticks every second on its own, with no widget reload and no network,
which is what keeps it right between refreshes. iOS decides when a widget actually
reloads: `refreshAfterDate` is a hint it can and does ignore, and it budgets reloads
across all widgets on the device. Expect roughly quarter-hourly in practice.

A timer whose instant has passed counts *up*, with no minus sign, so the script switches
to a word ("Departing", "Taxiing", "Landing") once the countdown expires rather than
showing a number that reads like a countdown but is not one.

The last good response is cached to the Scriptable documents folder. If the server is
unreachable the widget draws the cached data with a `Cached HH:MM` marker instead of
going blank. If the server says its own data is degraded, because the AeroAPI budget
breaker tripped or polling has stalled, that reason replaces the marker.
