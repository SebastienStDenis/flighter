// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: deep-blue; icon-glyph: plane-departure;

// Flight tracker widget.
//
// The server decides everything: which flights, in what order, what the pill says and
// in which tone, the line under the heading and which mark heads each place on it, the
// words that end the row and the time beside them, and where the airline's mark is to be
// fetched from. This file draws that and nothing else - and holds the three glyphs the
// places are headed with, which are the one thing on the widget that is not a string.
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
// Every time drawn here is on the phone's own clock. The one exception is the day a
// flight leaves, which is on the clock at the airport it leaves from and carries that
// airport's zone to say so.
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
// How much of the disc the letter on it takes, and how much taller than its own point
// size a line of the system font stands - which is what has to be centred on the disc
// rather than the point size, because the line is what the letter is drawn inside.
const FRIEND_INITIAL_SCALE = 0.6;
const LINE_HEIGHT = 1.2;

// The one repair for a missing or rejected token, and the same sentence for both.
const RECONNECT_TEXT = "Open the settings page on this phone and tap Connect.";

// Declared up here because the widget is built before the rest of the file has run, and
// a class, unlike a function, does not exist until its line does.
class TokenRejected extends Error {}

// The air on the line under the heading: what a mark keeps from its own figures, and
// what one end of the flight keeps from the seat. Two gaps rather than one, because
// what the line is read as is two things and not four.
const BESIDE_MARK = 3;
const BETWEEN_RUNS = 8;

// What a small widget holds: two flights, and the same distance under every line of
// them. It is the one size with no room to spare, so the figures it does have room for
// are the ones the board leads with.
const SMALL_FLIGHTS = 2;
const SMALL_GAP = 3;

// What separates one flight from the next, on every size that draws more than one.
//
// It was not a distance at all for a while: whatever the widget had left once its rows
// were drawn, shared equally between the gaps, so the flights filled the square and the
// column edge to edge. What that missed is that a widget is hardly ever holding as many
// flights as its size takes. The large size draws seven and most weeks has two, and two
// flights sharing a large widget's leftovers stand one against the top edge and one
// against the bottom with half a widget of nothing between them - a gap saying how empty
// the widget was rather than where one flight ended and the next began. It also moved: a
// third flight booked, or a first one landed and dropped off the list, redrew every row
// at a different pitch, so the same flights sat in different places from one reload to
// the next.
//
// So it is a fixed distance again, and what the widget has left over is held between the
// last flight and the stamp instead. The column starts at the top whatever the size and
// however many flights are on it, the break between two flights is the same break on a widget holding
// two as on one holding seven, and room that is not spoken for reads as what it is: a
// widget with three flights on it has three flights on it.
//
// Eight points is what the tightest size holds with the stamp under it. A row is a shade
// under 36 - the heading, the line under it and the three between them - and the stamp
// takes twelve, so the large's seven rows, six of these gaps and the line beneath them
// fit inside what a 6.1in phone's large widget holds, and the medium's three rows and two
// of them do the same. It is the figure to cut first if a row ever comes out clipped.
const FLIGHT_GAP = 8;

// What every size but the Lock Screen keeps between its words and its own edge. A widget
// whose words start against its rounded corner reads as one that ran out of room,
// whatever it is holding.
const INSET = 14;

const family = config.widgetFamily || "medium";
const isAccessory = family.startsWith("accessory");
// Per-element tap targets exist only on medium and large. Everywhere else the whole
// widget gets one URL.
const supportsRowLinks = family === "medium" || family === "large";

// What a row is set in: the heading carrying the number, the route beside it, the line
// under them saying where to be, the pill's own word, and the words and the time that
// end the row. A small widget is the same things at the sizes a 155pt square has room
// for.
//
// The route is set below the number rather than level with it, and set rather than left
// to fit. Given the number's own size it is the longest thing on the heading, so it was
// the half that shrank - and it shrank by however much the line it landed on had left
// over, which is not the same amount on a medium widget as on a large one. The same
// flight was then drawn larger on the bigger widget, for no reason a reader could see.
// A size chosen here is the same size on both.
const TYPE =
  family === "small"
    ? { heading: 12, route: 10, detail: 10, pill: 10, label: 10, time: 13 }
    : { heading: 14, route: 12, detail: 11, pill: 10, label: 11, time: 13 };

// How tall the line under the heading stands, on every row, whatever lands on it.
//
// Left to its contents that line is as tall as the tallest thing on it, and the things
// it carries are not the same height: the time is the largest type on the row and where
// to be is among the smallest, so a flight with a time to show took a taller line than a
// flight with only a date under its heading - and a flight with neither, days out with
// nothing to walk to or called off, took no second line at all. The gap between two
// rows is a fixed one, so what moved was the rows themselves: a column of them came out
// unevenly spaced, and the spacing said nothing about the flights, only about which of
// them happened to have a time.
//
// So the line is given the height of the tallest thing it can ever hold and keeps it
// whether or not anything that tall is on it. A row is then the same height as the row
// above it, the fixed gaps between them read as one distance, and the eye goes down the
// column on the words rather than on the spacing.
const UNDER_HEADING = Math.ceil(TYPE.time * LINE_HEIGHT);

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
    await notify("Widget updated", "The widget was updated to the latest version from the server.");
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

// `fetchedAt` is the moment the flights being drawn came from: now when the server
// answered, and the cache file's own modification date when it did not. `stale` says
// which of the two it is - the word in front of the stamp, the lock screen's dot, and
// whether the script may replace itself with the server's copy.
async function load(server) {
  try {
    const data = await request(server);
    writeCache(data);
    return { data, stale: false, fetchedAt: new Date(), rejected: false, error: null };
  } catch (error) {
    // Not the cache: yesterday's flights with a small mark on them would hide that the
    // token is wrong, and that is the one failure a reload never fixes.
    if (error instanceof TokenRejected) {
      return { data: null, stale: false, fetchedAt: null, rejected: true, error: null };
    }
    // A stale widget beats a blank one. The flight has almost certainly not changed, and
    // the lock screen marks its heading with a dot to say the reading is the cache's.
    const cached = readCache();
    if (cached) {
      return { data: cached.data, stale: true, fetchedAt: cached.cachedAt, rejected: false, error: null };
    }
    return { data: null, stale: false, fetchedAt: null, rejected: false, error: String(error.message || error) };
  }
}

async function request({ api, token }) {
  // The zone goes with the ask so the times come back on this phone's clock rather than
  // on the airport's, and the size goes with it so the list comes back as long as this
  // widget has room for. They are the two things the server cannot know and the phone
  // cannot work out for itself once the strings are built: a large widget holds twice
  // the rows a medium one does, and a list cut to the smaller of them is a large widget
  // with its bottom half empty.
  const query = `tz=${encodeURIComponent(timeZone())}&family=${encodeURIComponent(family)}`;
  const req = new Request(`${api}/api/widget?${query}`);
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
    message(widget, "No upcoming flights");
    if (!isAccessory) {
      updatedLine(widget, result);
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
    renderSmall(widget, flights.slice(0, SMALL_FLIGHTS), logos, data.board_url);
  } else {
    renderList(widget, flights, logos);
  }
  updatedLine(widget, result);
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
    // The same inset on every size. The small one was drawn three points tighter, on
    // the reading that two flights of three lines each is what a 155pt square barely
    // holds - but the lines are what they are, and what that widget actually has is
    // room to spare below them. Tightening the one size with air left over was three
    // points spent to make a widget look full, and what it bought was words nearer the
    // rounded corner than any other size sets them.
    widget.setPadding(INSET, INSET, INSET, INSET);
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
  if (hasDetail(flight)) {
    state.addSpacer();
    detailText(state, flight, { size: 11, opacity: 0.7, shrink: 0.8 });
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

function renderSmall(widget, flights, logos, board) {
  // One tap target for the whole square, set on the widget rather than on a row: iOS
  // gives a small widget only the one, whichever row the thumb landed on. It used to
  // carry the top flight, which meant a tap on the second one opened the first - the
  // reader is shown two flights and told, by every other size, that a row is a thing to
  // press. So it goes to the board instead, which is the one page that is not the wrong
  // answer to either tap: both flights are on it, in the order they are in here.
  widget.url = board;

  flights.forEach((flight, index) => {
    if (index > 0) {
      // The one distance here that is not SMALL_GAP: every line of a flight is the same
      // three points under the line above it, so the only gap that reads as a break is
      // the one between two flights, and this is it. It is the same figure the wide
      // sizes keep between two rows, because a break between two flights is the same
      // break whichever size is drawing it.
      widget.addSpacer(FLIGHT_GAP);
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

    // Kept on both flights whether or not either has anything for it, and kept at one
    // height: two flights on a 155pt square are read as two blocks of three lines, and a
    // flight that drops its last line is a block that has moved.
    widget.addSpacer(SMALL_GAP);
    const line = widget.addStack();
    line.centerAlignContent();
    line.size = new Size(0, UNDER_HEADING);
    detailText(line, flight);
    line.addSpacer();
    targetValue(line, flight);
  });

  // And whatever is left over goes here, between the flights and the stamp, rather than
  // between the flights themselves. A ListWidget centres what it holds when there is room
  // to spare, so without this the square would sink its blocks towards the middle as soon
  // as it had only one flight to draw.
  widget.addSpacer();
}

function renderList(widget, flights, logos) {
  flights.forEach((flight, index) => {
    if (index > 0) {
      // The same break the square keeps between its two blocks, and the same figure: a
      // gap between two flights is a gap between two flights, and the medium and the
      // large no longer set it eight points and nine apart for no reason a reader
      // holding both could see.
      widget.addSpacer(FLIGHT_GAP);
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
    //
    // The line is drawn on every row, at the same height on every row, even where the
    // flight has nothing to put on it: its height is the tallest thing it can ever carry
    // rather than the tallest thing this flight gave it, so a date under one heading and
    // a time under the next stand the same distance below both, and a row with neither
    // is not a row drawn short. The gap under the heading is well under the gap between
    // two flights, because these two lines are one flight: what the widget is sorted
    // into by eye is rows rather than lines.
    row.addSpacer(3);
    const line = row.addStack();
    line.centerAlignContent();
    line.size = new Size(0, UNDER_HEADING);
    detailText(line, flight);
    line.addSpacer();
    targetLabel(line, flight);
    if (flight.target_value) {
      line.addSpacer(5);
      targetValue(line, flight);
    }
  });

  // The room the size has left when it is holding fewer flights than it takes - which is
  // most of the time on the large. Held under the last row, so the first row is in the
  // same place on a widget with two flights as on one with seven, and so is the stamp the
  // room is held above.
  widget.addSpacer();
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
  // Two runs of figures on one line keep more air between them than a mark keeps from
  // its own - except on the square, where that air is part of what the route needs to be
  // drawn at the size below rather than at whatever this row had spare.
  if (family !== "small") {
    row.addSpacer(3);
  }
  const route = row.addText(flight.route);
  route.font = Font.regularMonospacedSystemFont(TYPE.route);
  route.textColor = MUTED;
  route.lineLimit = 1;
  // No shrink on any size, the square included. A shrink factor hands the size to
  // whatever else landed on that particular line - a longer number, a friend's disc -
  // so the same route came out smaller under one flight than under the flight above it,
  // which is a difference the reader has to account for and which says nothing. It was
  // the square's answer to a line holding a number and a route at once; the answers now
  // are a route set two points under the number, an arrow the server sends without the
  // spaces around it, and the air between the two runs spent on the figures instead.
  // Every route is the same seven characters, so one size fits all of them or none.

  // What holds whatever shares this line - the pill on the wide sizes - against the far
  // end of it, and what leaves the heading itself hard against the near one.
  row.addSpacer();
  return row;
}

// Whose flight it is, drawn the way the board draws it: their initial in a disc tinted
// by the hue the server takes from their name, so one person is one colour on the phone
// and on the page both.
//
// The letter is drawn into an image rather than set as text on the disc, because a disc
// this size has no way to hold a line of type in the middle of itself. Centring text in
// a stack takes a spacer either side of it, and a spacer keeps a length of its own
// before it gives any room away - the stack's default spacing, eight points, whatever
// the stack's own `spacing` is afterwards, because that figure is the gap between two
// things rather than the least a spacer will be. Two of them is sixteen points asked for
// inside a disc that is fourteen across; what is left for the letter is nothing, and a
// letter with nowhere to be drawn is not drawn small but dropped. Which is why the disc
// kept coming out bare however little padding it was given.
//
// Drawn, the letter is centred by the context rather than by the layout, and the disc is
// the size it was going to be either way. It is drawn white and tinted on the way in,
// the same deal the marks on the line below make: one image is then the light scheme's
// colour and the dark one's both, and the friend keeps one hue across the two.
function friendMark(container, flight) {
  if (!flight.friend_initial) {
    return null;
  }
  const side = TYPE.heading;
  const disc = container.addStack();
  disc.size = new Size(side, side);
  disc.cornerRadius = side / 2;
  disc.backgroundColor = friendColor(flight.friend_hue, FRIEND_DISC);
  disc.setPadding(0, 0, 0, 0);
  const letter = disc.addImage(initialImage(flight.friend_initial, side));
  letter.imageSize = new Size(side, side);
  letter.tintColor = friendColor(flight.friend_hue, FRIEND_INITIAL);
  return disc;
}

// One letter, centred in a square the size of the disc it is going on.
//
// `drawTextInRect` hangs the line from the top of the rect it is given, so the rect it
// is given is one line tall and set down the square by half of what is left over - which
// is the centring the stack could not do. A line of the system font stands about a fifth
// taller than its own point size, and that is the height being centred rather than the
// point size, or the letter sits low on the disc by half the difference.
function initialImage(letter, side) {
  const size = Math.round(side * FRIEND_INITIAL_SCALE);
  const line = size * LINE_HEIGHT;
  const context = new DrawContext();
  context.size = new Size(side, side);
  context.opaque = false;
  context.respectScreenScale = true;
  context.setFont(Font.semiboldSystemFont(size));
  context.setTextColor(Color.white());
  context.setTextAlignedCenter();
  context.drawTextInRect(letter, new Rect(0, (side - line) / 2, side, line));
  return context.getImage();
}

function hasDetail(flight) {
  return (flight.detail || []).length > 0;
}

function detailText(container, flight, options = {}) {
  const runs = flight.detail || [];
  if (runs.length === 0) {
    return null;
  }
  const size = options.size || TYPE.detail;
  const line = container.addStack();
  line.centerAlignContent();
  runs.forEach((run, index) => {
    if (index > 0) {
      // Between one end of the flight and the seat. Wider than the air a mark keeps
      // from its own figures, or the two runs read as one run of four things.
      line.addSpacer(BETWEEN_RUNS);
    }
    const glyph = mark(run.icon);
    if (glyph) {
      const drawn = line.addImage(glyph);
      // Squared to the type beside it and drawn in the row's quieter colour: the mark
      // says which figures these are and the figures are what is read.
      drawn.imageSize = new Size(size + 1, size + 1);
      if (isAccessory) {
        drawn.imageOpacity = options.opacity || 1;
      } else {
        drawn.tintColor = MUTED;
      }
      line.addSpacer(BESIDE_MARK);
    }
    // One line and no shrinking: what a narrow widget loses is the end of the line - the
    // seat, or the terminal at the other end - rather than the size of every word on it.
    const text = line.addText(run.text);
    text.font = Font.systemFont(size);
    if (isAccessory) {
      text.textOpacity = options.opacity || 1;
    } else {
      text.textColor = TEXT;
    }
    text.lineLimit = 1;
    if (options.shrink) {
      text.minimumScaleFactor = options.shrink;
    }
  });
  return line;
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
  // half: this is a label, and the figure it names is the news. On the wide sizes the
  // word has its line to itself bar the places and the time, and a size that moves from
  // row to row is the one thing a column of rows must not do.
  if (family === "small") {
    word.minimumScaleFactor = 0.7;
  }
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
  // On the narrow size the longest of them - "Departure delayed" - shares its line with
  // the rung the flight is on, and a word cut in half is a status nobody can read. On
  // the wide sizes the pill ends the heading with the spacer in the middle of that line
  // giving it whatever it asks for, so a shrink there only ever fires on the row that
  // happened to be fullest - and a pill drawn smaller than the pill above it reads as a
  // quieter status rather than as a longer word.
  if (family === "small") {
    text.minimumScaleFactor = 0.8;
  }
  return badge;
}

// The bottom of the widget: when what is drawn above it was got, on the phone's own
// clock, centred under the flights the way a footnote is.
//
// A clock face rather than an age. A widget is redrawn a few times an hour, so "4 min
// ago" written at draw time is the one figure on screen guaranteed to be wrong by the
// time anybody reads it, and wrong in the flattering direction. The time it was fetched
// at is simply a fact, and stays one for as long as iOS leaves the widget alone.
//
// "Cached" rather than "Last updated" when the server could not be reached, because then
// the time is when a file on the phone was written rather than when a server last spoke.
// A cache the phone will not date can still be drawn, and then the word is the whole of
// the line.
//
// It stands against the bottom edge rather than a fixed distance under the last flight:
// the height a size has spare sits between the two, so the stamp is in the same place on
// a widget holding two flights as on one holding seven. The Lock Screen has no line to
// spare for it, and says what it can with the dot after its heading.
function updatedLine(widget, result) {
  const word = result.stale ? "Cached" : "Last updated";
  const stamp = result.fetchedAt ? `${word} ${timeOfDay(result.fetchedAt)}` : word;
  const line = widget.addText(stamp);
  line.font = Font.systemFont(stampSize());
  line.textColor = MUTED;
  line.centerAlignText();
  line.lineLimit = 1;
  line.minimumScaleFactor = 0.7;
}

// Smaller than anything on a row, and smaller again on the square, where the type it is
// under is already the size a 155pt widget can hold.
function stampSize() {
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

// A time of day as the phone writes one: its own zone, and 04:12 or 4:12 AM according to
// how the reader has it set.
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

// --- marks -------------------------------------------------------------------------------

// The three glyphs the line under the heading draws in front of a place: a plane
// climbing, a plane coming down, and a seat. Lucide's plane-takeoff, plane-landing and
// armchair, which are the icons the web UI draws, rendered white at 48px and carried in
// the script rather than fetched from the server.
//
// Carried, because these are not decoration the way an airline's mark is. The row says
// "T4 • B22" and the glyph in front of it is the whole of what says those are the terminal
// and the gate this flight leaves from; a mark that had not arrived yet would leave a
// line that is read wrong rather than read short. So they cost the script two kilobytes
// and are on every widget from the first draw, network or no network.
//
// White, because the phone tints them: one file is then the light scheme's colour and
// the dark one's both, and on the lock screen it is whatever tint iOS is drawing that
// screen in. Lucide is ISC licensed; the paths are the library's own, unchanged.
function mark(name) {
  const MARKS = {
    takeoff:
      "iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAYAAABXAvmHAAAACXBIWXMAAAsTAAALEwEAmpwYAAACwklEQVRo"
      + "3u2YTUhVQRTHr+Z3ZmqptXEtmJs20TYMIozAdRjupEKJwo960qIWRUQfFERQQYtoU5t21dIWbgVxGdp79vnU"
      + "PmwX0//AEW6XuXPPnTvP95T5ww8e782dc+aemXPOvCDw8vLy8vISSilVBQ6AfnAQ1GwVx1tADiyq/7UGLoHG"
      + "Snb+BCgqs+ZAT6UuIK9k+gWGMtqqBYPgIXgDXoEr4DDYYTvposZRmvxzzEKegKaUNurBWbBkeEEFMEKLTLuA"
      + "XGSiIp+JZvAsxtg86BXM3QjG2DmpXlMySXuAo2cgF/p9GKxrDNF3wzFz7gQXDFFM0qiTKIR+7+W3rhNFqZnH"
      + "7QKT4KvBuT/gPjgOToPnmhc4a5NGY6PAY5p4/+v0BbwDqwbHf4NbYJ/Gfhv4G1lktdMohMYN8UFXKbLXDdBp"
      + "sH0k8swH22JmjEJobA/XBpN+gGtgT4LdajATefaxbUoVRYHHNnCVXos8s8S5vU1grw7c0yz+UJaWQhSF0DM1"
      + "3Df1cx9VJawLZ2LqwsuslVkcBYu5KWrnwEdDMdsrrYwnwRRDn+ttoyB0fDShbVkAfZItMg2+ayb4xmnugSbL"
      + "LFs6vlGJlxPqwm0qfi66zjjlUzpONeM8+CRwfL/rrlMXmQGhDUkLsc6R7sradVL6u8PoKig5MUHtgWBuavzG"
      + "uSKbKvFNU0Gzzi68Vwf5QI9zn1IrdHySo5RUiTtcXBudZBd2fCxhq5Djd3W9T5ZFTEeM0NZpLZHjXUFQmst7"
      + "6ihwmzyRkMXI8eugvdR3YHEUQo6vGBz/uSmOJ0SBOstHvD2OUjMFrmoatbBWuGlrDYLN/ycip+xV5CjuLvcf"
      + "WWmrMrUfl101di4WMWDoCqOVeEpS0Mq1kG5wDFwET8F78Ba8AKc2LuxeXl5eTtJmQVW+CtoLVIYbWTmU35YL"
      + "2NpbyKtM+gexJKWvs56E8gAAAABJRU5ErkJggg==",
    landing:
      "iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAYAAABXAvmHAAAACXBIWXMAAAsTAAALEwEAmpwYAAACk0lEQVRo"
      + "3u1Zvy8EQRQ+EhHiTikUGoUEiWio6AlRkAg6iYT4D9ydH4XyGiGhUWhpRKFBtEL0Or8rdwiNIOub5EnWy+zO"
      + "3LqZ3ZN9yZe7zHv7Zr53b2bnvUskYoklWuI4zhBwD1wBaSBZTotPAXnntzwC00BFORDION6yWY7R5zJlc0GD"
      + "lMvXFNlUkdF/AZ7Y2LWVVMIktZS7bhHRnZEtwCP6glAL8MrGO2wQmPVJg3WN6Od/fjF87jHdhOnFVwKXilwe"
      + "VkXfpc956Uye4255Bwps7FAn+qSfY/ot0wSO2YTbQBtfpE70yaaf6U9MLr5Lki5dpOMLbVdFn55rZTY3Jgls"
      + "s8mOXbojpivQUemb3xirBr5cNuJ7tYnFN1G+u2XIpR9WbOy817tCRJ3ZtpogsMImESdRJbNZ9yGQ8fF9wmz7"
      + "TRC4ZZPkJDYV9DKTXdhSPr63mP2cCQJ3bJIPYMzn3iM28ANdNwaLvGbkTBAYoUVrkSjS9wTzu2fqFBrzILH0"
      + "l8JE3H+YT3E/arFJ4ifP54MQob3DT6JnYAGot0kiMBFRC3j4KxghAoe9kqhxIkXVvLDd8PFXeiLCGbAoKUwC"
      + "EaFUmpbUGm55MkVkQXIr5W/itKpyI39JSkMVEeGvKgwimRISOQWaS70/bBO5AGpMnFaCSFaDSLYERNZMt1RU"
      + "RApkU6/pb5c9/wl02ugNZRX9IS0iVEfw2vzAZpMro0HE97iEboA9I4qhRtvduj8Rwfg5s+8Lq+2YVhAR5/4y"
      + "0AN0A6P0zBuz60wkwuufJmlRj04wueVVYphE5gMQGY9aR1uXiEijmSi35uuASWAHOKONuw+s0jW8If7vK5ZY"
      + "9P9SirrI+1CSxlaU5e5fEijvFIolJPkGoRujl1V5R9MAAAAASUVORK5CYII=",
    seat:
      "iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAYAAABXAvmHAAAACXBIWXMAAAsTAAALEwEAmpwYAAABRklEQVRo"
      + "3u1YQVKDQBAED/gDMfglniGYi3mG5j3R8qTJzcgpPoJ4j3pfhyouzgEW6BnWcrqqb+x0N1VssxtFBoPB8Cfg"
      + "nIuJN8SK+O3waGbuibfEBG0+I26dHg6NJsr8GXHn9NG8sBgRYOnmQ4kIULGhj8RU4Bu7IG6Y1h4x+IsNvRTc"
      + "KBZM6xMx9BcUdjusngWwANOblwPflGg9j+bFNSVab0DzQpoSrjewectIt+nL0c0r1ZRwva7mlWhKuF7fNobe"
      + "VuF6/y4AGhoBPgT91xoB7gUD3GkESIhr4hFo/Ni+mEQ8wAy3IRbAAmgH4NWezmg+Y15OPove2KIHyZuInhuK"
      + "J+bl1WfhyoWLwifAOfE9QPPP3gcoevCK+BKY+cWYQ3YRgPnrSUdXuxcKIEDd9bsrEACrR0Py9m+xYa4QQFXP"
      + "YDAYpuEHBby2Hukm43IAAAAASUVORK5CYII=",
  };
  const data = name ? MARKS[name] : null;
  return data ? Image.fromData(Data.fromBase64String(data)) : null;
}
