// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: deep-blue; icon-glyph: plane-departure;

// Flight tracker widget.
//
// The server decides everything: which flights, in what order, what the pill says and
// in which tone, the line under the heading, the words that end the row and the time
// beside them, and where the airline's mark is to be fetched from. This file draws that
// and nothing else.
//
// Nothing on it moves. iOS reloads a widget when it feels like it, about every quarter
// of an hour, so any figure counted from the clock at draw time - how long until the
// flight goes, how old what is on screen is - is that far out by the time it is read,
// and out in the flattering direction, which is worse than saying nothing. So the widget
// states times rather than counting to them: a flight due at 18:40 is due at 18:40
// whether the row is read at noon or at seven, and it goes on being true while iOS makes
// the widget wait for its next reload. The only word that has to keep up is the one in
// front of the time, which turns from "Departs" to "Due to depart" as the time goes by -
// and the server asks for a reload at that instant rather than at its usual cadence.
//
// Every time drawn here is on the phone's own clock, which is what the footer says. The
// one exception is the day a flight leaves, which is on the clock at the airport it
// leaves from and carries that airport's zone to say so.
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

// A friend's disc and the initial on it, as styles/app.css mixes them: the hue is the
// one the server took from their name, and these are the saturation, the lightness and
// the strength the page reads it at, light scheme then dark. Mixed here rather than
// rendered to a colour up here like the rest of the palette, because a hue that belongs
// to a name is not known until the name is.
const FRIEND_DISC = [
  { s: 55, l: 50, a: 0.12 },
  { s: 45, l: 65, a: 0.16 },
];
const FRIEND_INITIAL = [
  { s: 35, l: 38, a: 1 },
  { s: 42, l: 78, a: 1 },
];

// The one repair for a missing or rejected token, and the same sentence for both.
const RECONNECT_TEXT = "Open the settings page on this phone and tap Connect.";

// What every time on the widget has in common, said once at the foot of it rather than
// as a zone after each of them. The short form is the small size's, where the phrase
// shares its line with the moment the data landed.
const CLOCK_NOTE = "Times on your phone's clock";
const CLOCK_NOTE_SHORT = "your clock";

// Declared up here because the widget is built before the rest of the file has run, and
// a class, unlike a function, does not exist until its line does.
class TokenRejected extends Error {}

// What a small widget holds: two flights, and the same distance under every line of
// them. It is the one size with no room to spare, so the figures it does have room for
// are the ones the board leads with.
const SMALL_FLIGHTS = 2;
const SMALL_GAP = 3;

const family = config.widgetFamily || "medium";
const isAccessory = family.startsWith("accessory");
// Per-element tap targets exist only on medium and large. Everywhere else the whole
// widget gets one URL.
const supportsRowLinks = family === "medium" || family === "large";

// What a row is set in: the heading carrying the number and the route, the line under it
// saying where to be, the pill's own word, and the words and the time that end the row.
// A small widget is the same four things at the sizes a 155pt square has room for.
const TYPE =
  family === "small"
    ? { heading: 12, detail: 10, pill: 10, label: 10, time: 13 }
    : { heading: 14, detail: 11, pill: 10, label: 11, time: 13 };

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
      // No times above it, so nothing to say which clock they are on.
      footer(widget, data, result, false);
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
  footer(widget, data, result, true);
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
    // A little tighter on the small size, which holds two flights of three lines each.
    // Tighter, and not as tight as it will go: a widget whose words start against its
    // own rounded corner reads as one that ran out of room, whatever it is holding.
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

  // The word for the flight and where to be share a line rather than taking one each:
  // three lines is what a lock screen has, and what the flight is due to do next is
  // worth one of them on its own.
  const state = widget.addStack();
  state.centerAlignContent();
  const word = state.addText(flight.status_label);
  word.font = Font.semiboldSystemFont(15);
  word.lineLimit = 1;
  if (flight.detail) {
    state.addSpacer();
    const where = state.addText(flight.detail);
    where.font = Font.systemFont(11);
    where.textOpacity = 0.7;
    where.lineLimit = 1;
    where.minimumScaleFactor = 0.8;
  }

  // The rung and its time keep their own line with the words in front of them: "Lands"
  // and a bare 22:15 across a row from it is a line that has to be guessed at.
  if (flight.target_label) {
    const line = widget.addStack();
    line.centerAlignContent();
    const label = line.addText(flight.target_label);
    label.font = Font.systemFont(11);
    label.textOpacity = 0.7;
    label.lineLimit = 1;
    label.minimumScaleFactor = 0.8;
    line.addSpacer();
    const value = line.addText(flight.target_value);
    value.font = Font.boldMonospacedSystemFont(13);
    value.lineLimit = 1;
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
    titleRow(widget, flight, logos);

    // Then the two ends of a wide row, a line each rather than a column each - there is
    // no width here for a heading and a pill side by side. The pill leads its line and
    // the rung ends it; where to be leads the next and the time ends it. So the words
    // and the time they name still stand one above the other against the right-hand
    // edge, and the left of both lines still reads down the flight.
    widget.addSpacer(SMALL_GAP);
    const state = widget.addStack();
    state.centerAlignContent();
    pill(state, flight);
    state.addSpacer();
    targetLabel(state, flight);

    if (flight.detail || flight.target_value) {
      widget.addSpacer(SMALL_GAP);
      const line = widget.addStack();
      line.centerAlignContent();
      detailText(line, flight);
      line.addSpacer();
      targetValue(line, flight);
    }
  });
}

function renderList(widget, flights, logos) {
  flights.forEach((flight, index) => {
    if (index > 0) {
      widget.addSpacer(family === "large" ? 12 : 8);
    }
    const row = widget.addStack();
    row.layoutVertically();
    if (supportsRowLinks) {
      row.url = flight.detail_url;
    }

    // The heading with the pill at the far end of it, which is where the card puts its
    // own - and where a column of rows can be read straight down for the word alone.
    const head = row.addStack();
    head.centerAlignContent();
    titleRow(head, flight, logos);
    pill(head, flight);

    // Under it, where to be at the near end and what the flight is next due to do at the
    // far one. Both lines run the whole width of the row, so the pill and the time below
    // it stand against the same edge without either of them being measured: what holds
    // them there is the spacer in the middle of each line, which is the one thing on a
    // widget that costs nothing to be exactly as wide as it has to be.
    if (flight.detail || flight.target_value) {
      // Well under the gap between two flights, because these two lines are one flight:
      // what the widget is sorted into by eye is rows rather than lines.
      row.addSpacer(3);
      const line = row.addStack();
      line.centerAlignContent();
      detailText(line, flight);
      line.addSpacer();
      targetLabel(line, flight);
      if (flight.target_value) {
        line.addSpacer(5);
        targetValue(line, flight);
      }
    }
  });
}

// The heading: whose flight it is where it is not the reader's own, the airline's mark
// when it came, then the number, and the route beside it.
function titleRow(container, flight, logos) {
  const row = container.addStack();
  row.centerAlignContent();
  row.spacing = 5;
  friendMark(row, flight);
  const logo = logos[flight.logo_url];
  if (logo) {
    // Squared to the number's own point size rather than over it. A mark drawn taller
    // than the line of type beside it sets the height of the heading itself, and what is
    // held at the other end of that line then reads as adrift in a gap that is the
    // mark's rather than its own.
    const mark = row.addImage(logo);
    mark.imageSize = new Size(TYPE.heading, TYPE.heading);
    mark.cornerRadius = 3;
  }
  const number = row.addText(flight.number);
  number.font = Font.semiboldMonospacedSystemFont(TYPE.heading);
  number.textColor = TEXT;
  number.lineLimit = 1;
  row.addSpacer(3);
  const route = row.addText(flight.route);
  route.font = Font.regularMonospacedSystemFont(TYPE.heading);
  route.textColor = MUTED;
  route.lineLimit = 1;
  route.minimumScaleFactor = 0.7;
  // What holds whatever shares this line - the pill on the wide sizes - against the far
  // end of it, and what leaves the heading itself hard against the near one.
  row.addSpacer();
  return row;
}

// Whose flight it is, drawn the way the board draws it: their initial in a disc tinted
// by the hue the server takes from their name, so one person is one colour on the phone
// and on the page both.
function friendMark(container, flight) {
  if (!flight.friend_initial) {
    return null;
  }
  const side = TYPE.heading;
  const disc = container.addStack();
  disc.size = new Size(side, side);
  disc.cornerRadius = side / 2;
  disc.backgroundColor = friendColor(flight.friend_hue, FRIEND_DISC);
  disc.centerAlignContent();
  // A spacer either side rather than an alignment: `centerAlignContent` is the stack's
  // answer for the one axis it does not lay out along, and the letter has to be centred
  // on both or it sits against the side of its own disc.
  disc.addSpacer();
  const initial = disc.addText(flight.friend_initial);
  initial.font = Font.semiboldSystemFont(Math.round(side * 0.6));
  initial.textColor = friendColor(flight.friend_hue, FRIEND_INITIAL);
  initial.lineLimit = 1;
  disc.addSpacer();
  return disc;
}

// Where to be, at the near end of the second line: the terminal, the gate and the seat
// while somebody is walking to them, and the day the flight leaves while nobody is.
function detailText(container, flight) {
  if (!flight.detail) {
    return null;
  }
  // One line and no shrinking: what a narrow widget loses is the end of the line - the
  // seat, or the terminal at the other end - rather than the size of every word on it.
  const text = container.addText(flight.detail);
  text.font = Font.systemFont(TYPE.detail);
  text.textColor = TEXT;
  text.lineLimit = 1;
  return text;
}

// The board's own words for the rung ahead - "Departs", "Due to land" - or for the one
// thing a parked flight has left to point at, which is where its bag is.
function targetLabel(container, flight) {
  if (!flight.target_label) {
    return null;
  }
  const word = container.addText(flight.target_label);
  word.font = Font.systemFont(TYPE.label);
  word.textColor = MUTED;
  word.lineLimit = 1;
  // On the narrow size these words share their line with the pill, and the longest word
  // for a status is most of what a 155pt square has. Being read small beats being cut in
  // half: this is a label, and the figure it names is the news.
  word.minimumScaleFactor = 0.7;
  return word;
}

// The other half: the time the flight is due, or the figure a parked one has instead of
// a time. Monospaced, so a column of them lines up digit under digit down the widget,
// and set heavier than anything else on the row, because it is what the row is read for.
function targetValue(container, flight) {
  if (!flight.target_value) {
    return null;
  }
  const value = container.addText(flight.target_value);
  value.font = Font.boldMonospacedSystemFont(TYPE.time);
  value.textColor = TEXT;
  value.lineLimit = 1;
  return value;
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
  text.font = Font.semiboldSystemFont(TYPE.pill);
  text.textColor = toneColor(flight.status_tone);
  text.lineLimit = 1;
  // The longest of them - "Departure delayed" - shares its line with the rung the flight
  // is on, and a word cut in half is a status nobody can read.
  text.minimumScaleFactor = 0.8;
  return badge;
}

// The bottom of the widget: why the numbers might be wrong, when there is a reason, and
// under it when they were fetched and which clock they are all on. Two lines rather than
// one because a budget breaker's sentence does not share a row with anything.
function footer(widget, data, result, timed) {
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
  updatedLine(widget, result, timed);
}

// "Last updated 04:12" at one end of the line, and at the other the one thing every time
// above it has in common.
//
// A clock face rather than an age. A widget is redrawn a few times an hour, so "4 min
// ago" written at draw time is the one figure on screen guaranteed to be wrong by the
// time anybody reads it, and wrong in the flattering direction; the time it was fetched
// at is simply a fact, and stays one for as long as iOS leaves the widget alone.
//
// "Cached" rather than "Last updated" when the server could not be reached, because then
// the time is when a file on the phone was written rather than when a server last spoke.
//
// The note is the other half of the deal the rest of the widget makes: no time up there
// carries a zone, so this says once, quietly, whose clock they are on.
function updatedLine(widget, result, timed) {
  const size = footerSize();
  // A cache with no modification date on it can still be drawn; it just cannot be dated,
  // and then the word is the whole phrase.
  const word = result.stale ? "Cached" : "Last updated";
  const stamp = result.fetchedAt ? `${word} ${timeOfDay(result.fetchedAt)}` : word;

  if (family === "small") {
    // No room on a 155pt square for two phrases held apart, so they are one phrase, both
    // halves of it said as shortly as they can be, and the line centred under the
    // flights the way a footnote is.
    const brief = result.fetchedAt
      ? `${result.stale ? "Cached" : "Updated"} ${timeOfDay(result.fetchedAt)}`
      : word;
    const line = widget.addText(timed ? `${brief} · ${CLOCK_NOTE_SHORT}` : brief);
    line.font = Font.systemFont(size);
    line.textColor = MUTED;
    line.centerAlignText();
    line.lineLimit = 1;
    line.minimumScaleFactor = 0.7;
    return;
  }

  const line = widget.addStack();
  line.centerAlignContent();
  const stated = line.addText(stamp);
  stated.font = Font.systemFont(size);
  stated.textColor = MUTED;
  stated.lineLimit = 1;
  if (!timed) {
    return;
  }
  // The spacer is what makes this a footer rather than a sentence: one phrase against
  // each edge, the way the rows above it are held apart.
  line.addSpacer();
  const note = line.addText(CLOCK_NOTE);
  note.font = Font.systemFont(size);
  note.textColor = MUTED;
  note.lineLimit = 1;
  note.minimumScaleFactor = 0.8;
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
  // and nothing drawn here moves on its own between reloads. It shortens the cadence of
  // its own accord for the one thing that does go out of date on the phone - a rung
  // whose time is about to go by, and whose words change when it does.
  const cadence = Math.max(data.refresh_seconds || 900, MIN_REFRESH_SECONDS) * 1000;
  widget.refreshAfterDate = new Date(Date.now() + cadence);
}

// --- text ------------------------------------------------------------------------------

function toneColor(name, alpha = 1) {
  const [light, dark] = TONES[name] || TONES.quiet;
  return Color.dynamic(new Color(light, alpha), new Color(dark, alpha));
}

// One friend's hue, read the two ways the page reads it, and handed to the phone to pick
// between by its appearance.
function friendColor(hue, [light, dark]) {
  return Color.dynamic(
    new Color(hsl(hue, light.s, light.l), light.a),
    new Color(hsl(hue, dark.s, dark.l), dark.a)
  );
}

// A hue, a saturation and a lightness as the six hex digits Scriptable takes. The page
// hands the three of them straight to CSS; this is the same conversion, written out.
function hsl(hue, saturation, lightness) {
  const s = saturation / 100;
  const l = lightness / 100;
  const chroma = (1 - Math.abs(2 * l - 1)) * s;
  const second = chroma * (1 - Math.abs(((hue / 60) % 2) - 1));
  const base = l - chroma / 2;
  const wheel = [
    [chroma, second, 0],
    [second, chroma, 0],
    [0, chroma, second],
    [0, second, chroma],
    [second, 0, chroma],
    [chroma, 0, second],
  ];
  const channels = wheel[Math.floor(hue / 60) % 6].map((part) =>
    Math.round((part + base) * 255)
      .toString(16)
      .padStart(2, "0")
  );
  return `#${channels.join("")}`;
}

// The lock screen's version of the footer, squeezed into the one line a message has.
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
