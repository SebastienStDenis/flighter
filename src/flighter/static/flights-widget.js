// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: deep-blue; icon-glyph: plane-departure;

// Flight tracker widget.
//
// The server decides everything: which flights, in what order, what the pill says and
// in which tone, the one line under it, what the flight is counting to, and where the
// airline's mark is to be fetched from. This file draws that and nothing else.
//
// Two figures are not worked out here at all. iOS reloads a widget when it feels like it,
// about every quarter of an hour, so any number counted from the clock at draw time is
// that far wrong by the time it is read - and how long until the flight goes, and how old
// what is on screen is, are the two that have to be right to the minute. WidgetKit ticks a
// date on its own, between reloads and without waking anything, so both are drawn as dates
// and it counts them: the server's instant for the first, and the moment this data landed
// for the second. Every other time in the payload is already a clock face, right until the
// estimate moves.
//
// A word is not a date, though. The label beside a countdown is frozen at the moment it was
// drawn, so "Departs in" is still there while the count ticks upwards past zero, and the
// row reads as three minutes to go when it is three minutes overdue. Nothing on the phone
// can repair that: swapping a drawn glyph for another one needs a drawing, WidgetKit hands
// out one per reload, and a reload re-runs this file from the top and asks the server again
// - by which point the label has rolled over on its own and the count is meant to climb.
// So the reload is the whole of the answer: the server asks for one at the instant itself
// rather than at its usual cadence, and the row is wrong for as long as iOS makes it wait.
//
// There is nothing to edit here. The server's address and the token arrive through the
// Connect button on the settings page, which runs this script with both in the URL, and
// live in the Keychain from then on.

const API_KEY = "flighter-api";
const TOKEN_KEY = "flighter-token";
const SCRIPT_PATH = "/static/flights-widget.js";
const CACHE_FILE = "flighter-widget.json";
// An airline's mark does not change, so one fetch per carrier is kept for good and the
// widget draws it whether or not the network is there.
const LOGO_PREFIX = "flighter-logo-";
const REQUEST_TIMEOUT_SECONDS = 15;
// iOS budgets reloads and ignores an eager request anyway, so do not ask for one.
const MIN_REFRESH_SECONDS = 60;
// The web UI's palette, each value as styles/app.css defines it for the light and the
// dark scheme, rendered from oklch to sRGB. The phone's appearance picks the side, the
// same way the page does.
const BACKGROUND = Color.dynamic(new Color("#ffffff"), new Color("#14171e"));
const TEXT = Color.dynamic(new Color("#111720"), new Color("#e9edf2"));
const MUTED = Color.dynamic(new Color("#5f656e"), new Color("#989fa9"));

// The six tones a status can be drawn in, light then dark. The pill's background is the
// same colour let through at the strength the page mixes it into its card.
const TONES = {
  quiet: ["#565b63", "#9fa5ae"],
  plan: ["#0c60a3", "#67b0f9"],
  live: ["#7146ad", "#bd96ff"],
  ok: ["#00683f", "#58c38b"],
  warn: ["#815200", "#eea743"],
  stop: ["#ae282b", "#ff7871"],
};
// What the pill lets through behind the word, as the page mixes it into its card.
const TINT_ALPHA = 0.14;

// The one repair for a missing or rejected token, and the same sentence for both.
const RECONNECT_TEXT = "Open the settings page on this phone and tap Connect.";

// Declared up here because the widget is built before the rest of the file has run, and
// a class, unlike a function, does not exist until its line does.
class TokenRejected extends Error {}

// What a small widget holds: two flights, and the same distance under every line of
// them. It is the one size with no room to spare, so the figures it does have room for
// are the ones the board leads with.
const SMALL_FLIGHTS = 2;
const SMALL_GAP = 2;

// The count, at one size on every home screen size. It was sized against the room each
// widget had going spare, which made the figure smallest on the widget with the most
// room on it - a medium one holding three flights - and left the same number drawn three
// different ways across a home screen. It is the figure every one of them is looked at
// for, so it is the same figure on all of them.
const COUNT_SIZE = 18;
// How far the count is pulled up on the sizes that draw the words naming it on the row
// above. A line of type carries air over its glyphs, and at half again the size of every
// other word on the widget the count carries half again as much: left where it falls, it
// puts most of a blank line between "Departs in" and the figure it belongs to. Rather
// less than that air, so the digits tuck under their label rather than up against it.
const COUNT_LIFT = 6;

const family = config.widgetFamily || "medium";
const isAccessory = family.startsWith("accessory");
// Per-element tap targets exist only on medium and large. Everywhere else the whole
// widget gets one URL.
const supportsRowLinks = family === "medium" || family === "large";

const server = connect();
const result = server ? await load(server) : null;
const widget = result ? await buildWidget(result) : setupWidget();

if (config.runsInWidget) {
  Script.setWidget(widget);
  // A reload that reached the server is also the moment to take its newer script, so
  // the widget follows the server without anyone opening the app. Quietly: a widget
  // has nobody to tell, and the next reload simply runs the new copy.
  if (result && !result.stale && !result.rejected) {
    await updateScript(server.api);
  }
} else {
  await present(widget);
  if (server && (await updateScript(server.api))) {
    await notify("Widget updated", "The server's newer version is installed and runs from now on.");
  }
}
Script.complete();

// --- data ------------------------------------------------------------------------------

function connect() {
  // Opened from the settings page: scriptable:///run/Flighter?api=...&token=... Each
  // tap overwrites what is stored, which is how a regenerated token gets onto the phone.
  const params = args.queryParameters || {};
  if (params.api && params.token) {
    Keychain.set(API_KEY, params.api.replace(/\/+$/, ""));
    Keychain.set(TOKEN_KEY, params.token);
  }
  // Keychain.get() throws on a missing key, so contains() is not optional.
  if (Keychain.contains(API_KEY) && Keychain.contains(TOKEN_KEY)) {
    return { api: Keychain.get(API_KEY), token: Keychain.get(TOKEN_KEY) };
  }
  return null;
}

// `fetchedAt` is the whole point of the footer: the moment the flights being drawn came
// from, which is now when the server answered and the age of the cache when it did not.
async function load(server) {
  try {
    const data = await request(server);
    writeCache(data);
    return { data, stale: false, fetchedAt: new Date(), rejected: false, error: null };
  } catch (error) {
    // Not the cache: yesterday's flights with a small "Cached" mark would hide that the
    // token is wrong, and that is the one failure a reload never fixes.
    if (error instanceof TokenRejected) {
      return { data: null, stale: false, fetchedAt: null, rejected: true, error: null };
    }
    // A stale widget beats a blank one. The flight has almost certainly not changed,
    // and the footer says how old what is drawn is.
    const cached = readCache();
    if (cached) {
      return { data: cached.data, stale: true, fetchedAt: cached.cachedAt, rejected: false, error: null };
    }
    return {
      data: null,
      stale: false,
      fetchedAt: null,
      rejected: false,
      error: String(error.message || error),
    };
  }
}

async function request({ api, token }) {
  // The zone goes with the ask so the times come back on this phone's clock rather than
  // on the airport's. It is the one thing the server cannot know and the phone cannot
  // work out for itself once the strings are built.
  const req = new Request(`${api}/api/widget?tz=${encodeURIComponent(timeZone())}`);
  req.headers = { Authorization: `Bearer ${token}` };
  req.timeoutInterval = REQUEST_TIMEOUT_SECONDS;
  const body = await req.loadJSON();
  const status = req.response.statusCode;
  if (status === 401) {
    throw new TokenRejected("token rejected");
  }
  if (status !== 200) {
    throw new Error(`server returned ${status}`);
  }
  return body;
}

async function updateScript(api) {
  // Scripts are plain files in Scriptable's documents folder, so this one can replace
  // itself with the server's copy and the widget ships with the server.
  try {
    const req = new Request(`${api}${SCRIPT_PATH}`);
    req.timeoutInterval = REQUEST_TIMEOUT_SECONDS;
    const latest = await req.loadString();
    // The header is the one line every copy of this script starts with, so anything
    // else is an error page and must not be written over the working script.
    if (req.response.statusCode !== 200 || !latest.startsWith("// Variables used by Scriptable")) {
      return false;
    }
    const path = module.filename;
    const fm = fileManagerFor(path);
    if (fm.readString(path) === latest) {
      return false;
    }
    fm.writeString(path, latest);
    return true;
  } catch (error) {
    console.warn(`could not update the script: ${error}`);
    return false;
  }
}

function fileManagerFor(path) {
  // A script kept in iCloud Drive can only be written through the iCloud manager, and
  // asking for that manager throws on a phone without iCloud Drive turned on.
  try {
    const icloud = FileManager.iCloud();
    if (path.startsWith(icloud.documentsDirectory())) {
      return icloud;
    }
  } catch (error) {
    // Fall through: the script is local.
  }
  return FileManager.local();
}

async function notify(title, text) {
  const alert = new Alert();
  alert.title = title;
  alert.message = text;
  alert.addAction("OK");
  await alert.present();
}

function cachePath() {
  const fm = FileManager.local();
  return fm.joinPath(fm.documentsDirectory(), CACHE_FILE);
}

function writeCache(data) {
  try {
    FileManager.local().writeString(cachePath(), JSON.stringify(data));
  } catch (error) {
    console.warn(`could not write cache: ${error}`);
  }
}

function readCache() {
  const fm = FileManager.local();
  const path = cachePath();
  if (!fm.fileExists(path)) {
    return null;
  }
  try {
    return { data: JSON.parse(fm.readString(path)), cachedAt: fm.modificationDate(path) };
  } catch (error) {
    console.warn(`could not read cache: ${error}`);
    return null;
  }
}

// Each carrier's mark, by the address the server gave for it. A mark that cannot be had
// is simply not drawn: the number beside it already names the airline.
async function loadLogos(flights) {
  const urls = [...new Set(flights.map((flight) => flight.logo_url).filter(Boolean))];
  const images = await Promise.all(urls.map(loadLogo));
  const logos = {};
  urls.forEach((url, index) => {
    if (images[index]) {
      logos[url] = images[index];
    }
  });
  return logos;
}

async function loadLogo(url) {
  const fm = FileManager.local();
  const path = fm.joinPath(fm.documentsDirectory(), LOGO_PREFIX + url.split("/").pop());
  try {
    if (fm.fileExists(path)) {
      return fm.readImage(path);
    }
    const req = new Request(url);
    req.timeoutInterval = REQUEST_TIMEOUT_SECONDS;
    const image = await req.loadImage();
    fm.writeImage(path, image);
    return image;
  } catch (error) {
    console.warn(`could not load the logo at ${url}: ${error}`);
    return null;
  }
}

// --- widget ----------------------------------------------------------------------------

async function buildWidget(result) {
  const widget = newWidget();

  if (result.rejected) {
    message(widget, "Reconnect needed", RECONNECT_TEXT);
    return widget;
  }
  if (!result.data) {
    message(widget, "Flights unavailable", result.error);
    return widget;
  }

  const data = result.data;
  const flights = data.flights || [];
  scheduleRefresh(widget, data);

  if (flights.length === 0) {
    // The lock screen has no room for a footer, so there the age goes in the message.
    message(widget, "No upcoming flights", isAccessory ? staleNote(result) : null);
    if (!isAccessory) {
      footer(widget, data, result);
    }
    return widget;
  }

  // The Lock Screen draws everything in its own tint, which would turn a mark into a
  // blot, so only the home screen sizes carry one.
  if (isAccessory) {
    renderAccessory(widget, flights[0], result);
    return widget;
  }
  const logos = await loadLogos(flights);
  if (family === "small") {
    renderSmall(widget, flights.slice(0, SMALL_FLIGHTS), logos);
  } else {
    renderList(widget, flights, logos);
  }
  widget.addSpacer();
  // On every home screen size, two flights of four lines included. The height this was
  // held off the small one for is height the small one turns out to have: the figures
  // here are worked out from the point sizes, and the phone is what says how tall a
  // line of them really is.
  footer(widget, data, result);
  return widget;
}

function newWidget() {
  const widget = new ListWidget();
  if (isAccessory) {
    // Lets the Lock Screen paint its own adaptive backdrop behind the content.
    widget.addAccessoryWidgetBackground = true;
    widget.setPadding(2, 2, 2, 2);
  } else {
    widget.backgroundColor = BACKGROUND;
    // A little tighter on the small size, which holds two flights of four lines each with
    // the count on them set at the size every other widget draws it. Tighter, and not as
    // tight as it will go: a widget whose words start against its own rounded corner
    // reads as one that ran out of room, whatever it is holding.
    const inset = family === "small" ? 11 : 14;
    widget.setPadding(inset, inset, inset, inset);
  }
  return widget;
}

function renderAccessory(widget, flight, result) {
  // Roughly three lines above the clock, and one tap target for the lot. The Lock
  // Screen draws everything in its own tint, so nothing here is given a colour.
  widget.url = flight.detail_url;

  const heading = `${flight.number}  ${flight.route}`;
  const title = widget.addText(result.stale ? `${heading} ·` : heading);
  title.font = Font.semiboldMonospacedSystemFont(13);
  title.lineLimit = 1;
  title.minimumScaleFactor = 0.7;

  // The word and the count share a line here rather than taking one each: three lines
  // is what a lock screen has, and the gate is worth one of them.
  const state = widget.addStack();
  state.centerAlignContent();
  const word = state.addText(flight.status_label);
  word.font = Font.semiboldSystemFont(15);
  word.lineLimit = 1;
  if (flight.milestone_at) {
    state.addSpacer();
    countdown(state, flight, 13);
  }

  if (flight.detail) {
    const text = widget.addText(flight.detail);
    text.font = Font.systemFont(11);
    text.textOpacity = 0.7;
    text.lineLimit = 1;
    text.minimumScaleFactor = 0.8;
  }
}

function renderSmall(widget, flights, logos) {
  // One tap target, set on the widget rather than a row: a small widget has only the one
  // to give, and the flight at the top is the one it is being looked at for.
  widget.url = flights[0].detail_url;

  flights.forEach((flight, index) => {
    if (index > 0) {
      // The one distance here that is not SMALL_GAP. Every line of a flight is the same
      // distance under the line above it, so the only gap that reads as a break is the
      // one between two flights.
      widget.addSpacer(SMALL_GAP * 2);
    }
    // The number and the route on one line. The two do not fit at the size the rest of
    // this widget is read at, so the route is the half that gives: it shrinks against
    // the number rather than taking a line of its own.
    titleRow(widget, flight, logos, 11, true);

    // Then the board's own order, a line each: what state it is in, where to be, and
    // what it is counting to with the count itself at the end of the line.
    widget.addSpacer(SMALL_GAP);
    const state = widget.addStack();
    pill(state, flight);
    state.addSpacer();

    if (flight.detail) {
      widget.addSpacer(SMALL_GAP);
      const detail = widget.addText(flight.detail);
      detail.font = Font.systemFont(9);
      detail.textColor = TEXT;
      // One line and no shrinking: what a narrow widget loses is the end of the line -
      // the seat - rather than the size of every word on it.
      detail.lineLimit = 1;
    }

    if (flight.milestone_at) {
      widget.addSpacer(SMALL_GAP);
      const line = widget.addStack();
      line.centerAlignContent();
      // A point under the words on the lines above it, because this one shares its line
      // with a figure set twice its size: at the wider margins the two of them together
      // are the whole of the line, and the words are the half of it that can afford to
      // be quieter.
      milestoneWord(line, flight, 9);
      line.addSpacer();
      // No slack on this one: the words naming the count share its line, and at this
      // width the widest reading a count can take is already most of what there is.
      countdown(line, flight, COUNT_SIZE, 0).textColor = TEXT;
    }
  });
}

function renderList(widget, flights, logos) {
  flights.forEach((flight, index) => {
    if (index > 0) {
      widget.addSpacer(family === "large" ? 12 : 8);
    }
    const row = widget.addStack();
    if (supportsRowLinks) {
      row.url = flight.detail_url;
    }
    row.layoutVertically();

    // The board's footer, at the two ends of a row rather than the two ends of a card:
    // what the flight is counting to on the right of its number, and the count itself
    // on the right of what there is to find.
    titleRow(row, flight, logos, 14, true, (heading) => {
      milestoneWord(heading, flight, 11);
    });
    row.addSpacer(4);

    const line = row.addStack();
    line.centerAlignContent();
    line.spacing = 6;
    pill(line, flight);
    if (flight.detail) {
      // No scale factor: a text that can shrink is sized before the pill and handed
      // half the line, and is cut short with room beside it. One that only truncates
      // is sized after the pill and gets everything the pill left. Where to be leads the
      // line, so what a narrow row loses is the seat rather than the gate.
      const detail = line.addText(flight.detail);
      detail.font = Font.systemFont(11);
      detail.textColor = TEXT;
      detail.lineLimit = 1;
    }
    line.addSpacer();
    if (flight.milestone_at) {
      // The one size that draws the words naming the count on the row over it, so the
      // one size the count is pulled up on.
      countdown(line, flight, COUNT_SIZE, 1, COUNT_LIFT).textColor = TEXT;
    }
  });
}

// The heading: the airline's mark, when it came, then the number, and the route beside
// it where the row is wide enough to hold both. Whatever `trailing` draws is held at the
// far end of the line by the spacer between them.
function titleRow(container, flight, logos, size, withRoute, trailing) {
  const row = container.addStack();
  row.centerAlignContent();
  row.spacing = 5;
  const logo = logos[flight.logo_url];
  if (logo) {
    const mark = row.addImage(logo);
    mark.imageSize = new Size(size + 3, size + 3);
    mark.cornerRadius = 3;
  }
  const number = row.addText(flight.number);
  number.font = Font.semiboldMonospacedSystemFont(size);
  number.textColor = TEXT;
  number.lineLimit = 1;
  if (withRoute) {
    row.addSpacer(3);
    const route = row.addText(flight.route);
    route.font = Font.regularMonospacedSystemFont(size);
    route.textColor = MUTED;
    route.lineLimit = 1;
    route.minimumScaleFactor = 0.7;
  }
  row.addSpacer();
  if (trailing) {
    trailing(row);
  }
  return row;
}

// The board's own words for the rung ahead - "Departs in", "Due to land" - which is the
// half of its footer that does not move.
function milestoneWord(container, flight, size) {
  if (!flight.milestone_label) {
    return null;
  }
  const word = container.addText(flight.milestone_label);
  word.font = Font.systemFont(size);
  word.textColor = MUTED;
  word.lineLimit = 1;
  // On the narrow size these words share their line with a count set half again their
  // size, and the widest reading a count can take leaves them barely room. Being read
  // small beats being cut in half: this is a label, and the figure beside it is the news.
  word.minimumScaleFactor = 0.7;
  return word;
}

// The other half, which iOS draws for itself: the instant the server named, counted down
// where it stands and ticking between reloads. Monospaced, so nothing beside it shuffles
// as the digits change, and it goes on counting up once the time has gone by - which is
// the state the word beside it is already saying is a wait.
//
// Aligned right explicitly. A timer is the one element whose text WidgetKit cannot measure
// before it draws it, so it is handed the whole of what the spacer left rather than only
// what the digits need; the preview in Scriptable, which measures a snapshot, hands it the
// digits. Left to itself the count therefore sits at the right-hand end of the row in the
// app and part-way along it on the home screen, which is the same widget disagreeing with
// itself. Right is the end the spacer was put there to hold it against.
function countdown(container, flight, size, slack = 1, lift = 0) {
  const at = new Date(flight.milestone_at);
  // The box is what the count is moved by, as well as what it is measured into.
  // `lift` is for the sizes that draw the words naming the count on the row above it:
  // the air a line of this size carries over its glyphs lands between the two of them
  // there, and reads as the figure having come adrift of its own label. Pulling the box
  // up by that air is the only way to take it back - WidgetKit gives a line of text no
  // say in its own height - and it costs the row nothing, because the air is over the
  // digits rather than between them and anything else.
  const box = container.addStack();
  if (lift) {
    box.setPadding(-lift, 0, 0, 0);
  }
  // Boxed to the width of the reading it is about to show. A timer is the one element
  // WidgetKit cannot measure before it draws it, so left to itself it is handed all the
  // room the spacer left - and what it does not use it keeps, off the end of the line
  // where the gate and the seat are being cut short to make it. Scriptable measures a
  // snapshot and hands it the digits, which is why the same row reads differently in the
  // app and on the home screen.
  box.size = new Size(timerWidth(at, size, slack), 0);
  const date = box.addDate(at);
  date.applyTimerStyle();
  // Bold rather than semibold. The two are the same weight to look at when a Mac draws
  // an iPhone's widget, so the count reads as heavier than the row on a mirrored screen
  // and no different from it on the phone itself - and the phone is the screen this is
  // for. The weight is the point: the count is what the row is looked at for.
  date.font = Font.boldMonospacedSystemFont(size);
  // Held against the end of its box, which is the end of the row: what the box has over
  // is the difference between the reading and the widest one it could turn into.
  date.rightAlignText();
  date.lineLimit = 1;
  // Only a hair of give. A count that shrinks to fit is a count drawn smaller than the
  // size it was asked for, and the size is the point; the box is measured wide so this
  // never has to happen.
  date.minimumScaleFactor = 0.95;
  return date;
}

// The board's badge: the word in its tone, on the same tone let through the card.
function pill(container, flight) {
  const badge = container.addStack();
  badge.centerAlignContent();
  // No padding of its own on the small size. Every line of a flight there sits the same
  // distance under the one above it, and air the pill carries lands inside that distance
  // - so the gap above and below the pill read wider than the gaps around every other
  // line. The word keeps the room it needs either side of it, which is the only room a
  // pill on a line of its own actually needs.
  const pad = family === "small" ? 0 : 2;
  badge.setPadding(pad, 6, pad, 6);
  badge.cornerRadius = 7;
  badge.backgroundColor = toneColor(flight.status_tone, TINT_ALPHA);
  const text = badge.addText(flight.status_label);
  text.font = Font.semiboldSystemFont(10);
  text.textColor = toneColor(flight.status_tone);
  text.lineLimit = 1;
  return badge;
}

// The bottom of the widget: why the numbers might be wrong, when there is a reason, and
// under it how old the numbers are, always. Two lines rather than one because a budget
// breaker's sentence and a running clock do not share a row on a small widget.
function footer(widget, data, result) {
  if (data.degraded) {
    const text = widget.addText(data.degraded_reason || "Status may be out of date");
    text.font = Font.systemFont(footerSize());
    text.textColor = MUTED;
    // Centred with the line under it: the two of them are one block, and a sentence
    // starting where the flights start reads as another row of the list.
    text.centerAlignText();
    text.lineLimit = 1;
    text.minimumScaleFactor = 0.7;
  }
  updatedLine(widget, result);
}

// "Last updated 04:12", counted by WidgetKit rather than worked out here.
//
// The figure is the reason this line exists: a widget is redrawn a few times an hour, so
// "4 min ago" written at draw time is the one number on screen guaranteed to be wrong by
// the time anybody reads it - and wrong in the flattering direction, which is worse than
// saying nothing. A date drawn in the timer style counts upwards on its own, so the age
// on screen is the real one whenever the phone is looked at, whether iOS has reloaded
// this widget in the last minute or has not touched it for half an hour.
//
// "Cached" rather than "Last updated" when the server could not be reached, because then
// the figure is the age of a file on the phone rather than of a conversation with a server.
//
// The word leads and the figure ends it, and the phrase sits in the middle of the widget:
// it is the one line that belongs to the whole of it rather than to a flight, and every
// row over it starts hard against the left. Centred, it reads as the widget's own footnote
// rather than as one more row of the list.
function updatedLine(widget, result) {
  const size = footerSize();
  const line = widget.addStack();
  line.centerAlignContent();
  line.spacing = 3;
  // A spacer at each end rather than one at the right: the two of them share what the
  // phrase leaves and hold it in the middle between them.
  line.addSpacer();

  const word = line.addText(result.stale ? "Cached" : "Last updated");
  word.font = Font.systemFont(size);
  word.textColor = MUTED;
  word.lineLimit = 1;

  // A cache with no modification date on it can still be drawn; it just cannot be aged -
  // and then the word is the whole phrase, centred on its own between the same spacers.
  if (result.fetchedAt) {
    // Boxed like the count, and for the same reason: a timer is the one element WidgetKit
    // cannot measure before it draws it, so left to itself it takes the rest of the line
    // and holds the digits at whichever end it is told to. No glyph of slack on this one,
    // the way there is on a count held against the end of a row: a centred phrase is
    // measured from both ends, so room the box has over is room the words are pushed left
    // by rather than room that falls off where the line stops.
    const box = line.addStack();
    box.size = new Size(timerWidth(result.fetchedAt, size), 0);
    const age = box.addDate(result.fetchedAt);
    age.applyTimerStyle();
    age.font = Font.regularMonospacedSystemFont(size);
    age.textColor = MUTED;
    age.leftAlignText();
    age.lineLimit = 1;
    // The line reads as one phrase, so the figure in it is set at the size of the word in
    // front of it. A hair of give for the phone that has not been asked to redraw this
    // since the reading gained a digit, and no more than that.
    age.minimumScaleFactor = 0.95;
  }
  line.addSpacer();
}

// How wide a counting timer is about to be, whichever way it runs. WidgetKit counts in
// hours, minutes and seconds, so a reading is "59:59" inside the hour, "9:59:59" inside
// the day and "23:59:59" over one - and nothing here counts past a day, because a flight
// further off than that is not being counted to at all.
//
// A monospaced glyph is six tenths of its point size - a figure, not a guess, which is
// what monospaced means - and the box is figured at sixty-four hundredths: enough over
// for rounding and for a face that is not quite the one assumed here, and not so much
// that the padding starts crowding whatever shares the line. `margin` is a glyph of
// slack on top of that, for a count drawn against the end of a wide row where nothing
// reads the gap; a narrow row, where the words naming the count are beside it, gets
// none. Nothing may shrink into these boxes by more than a twentieth, so a box that is
// still wrong shows up as a box that is wrong rather than as a figure quietly drawn
// small.
function timerWidth(at, size, margin = 0) {
  const away = Math.abs(Date.now() - new Date(at).getTime()) + 10 * 60 * 1000;
  const hours = away / (60 * 60 * 1000);
  const glyphs = (hours < 1 ? 5 : hours < 10 ? 7 : 8) + margin;
  return Math.ceil(glyphs * size * 0.64);
}

function footerSize() {
  return family === "small" ? 9 : 10;
}

function message(widget, headline, detail) {
  widget.addSpacer();
  const title = widget.addText(headline);
  title.font = Font.semiboldSystemFont(isAccessory ? 13 : 15);
  if (!isAccessory) {
    title.textColor = TEXT;
  }
  title.centerAlignText();
  if (detail) {
    const body = widget.addText(detail);
    body.font = Font.systemFont(isAccessory ? 10 : 11);
    if (!isAccessory) {
      body.textColor = MUTED;
    }
    body.centerAlignText();
    body.lineLimit = 2;
    body.minimumScaleFactor = 0.7;
  }
  widget.addSpacer();
}

function setupWidget() {
  const widget = newWidget();
  message(widget, "Not connected", RECONNECT_TEXT);
  return widget;
}

function scheduleRefresh(widget, data) {
  // The server's cadence is the one to keep: the data does not move faster than that,
  // and nothing drawn here moves on its own between reloads.
  const cadence = Math.max(data.refresh_seconds || 900, MIN_REFRESH_SECONDS) * 1000;
  widget.refreshAfterDate = new Date(Date.now() + cadence);
}

// --- text ------------------------------------------------------------------------------

function toneColor(name, alpha = 1) {
  const [light, dark] = TONES[name] || TONES.quiet;
  return Color.dynamic(new Color(light, alpha), new Color(dark, alpha));
}

// The lock screen's version of the footer, squeezed into the one line a message has: no
// room there for a word, a running count and "ago", so it states the clock face instead.
function staleNote(result) {
  if (!result.stale) {
    return null;
  }
  return result.fetchedAt ? `Cached ${timeOfDay(result.fetchedAt)}` : "Cached";
}

// The IANA name of the zone the phone is set to, or nothing if it will not say, which
// the server reads as "use the airport's clock".
function timeZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch (error) {
    console.warn(`could not read the time zone: ${error}`);
    return "";
  }
}

function timeOfDay(date) {
  const formatter = new DateFormatter();
  formatter.useNoDateStyle();
  formatter.useShortTimeStyle();
  return formatter.string(date);
}

async function present(widget) {
  if (family === "small") {
    await widget.presentSmall();
  } else if (family === "large") {
    await widget.presentLarge();
  } else if (family === "accessoryRectangular") {
    await widget.presentAccessoryRectangular();
  } else if (family === "accessoryCircular") {
    await widget.presentAccessoryCircular();
  } else if (family === "accessoryInline") {
    await widget.presentAccessoryInline();
  } else {
    await widget.presentMedium();
  }
}
