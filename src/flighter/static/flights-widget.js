// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: deep-blue; icon-glyph: plane-departure;

// Flight tracker widget.
//
// The server decides everything: which flights, in what order, which one of them is
// under way and gets the whole screen as its card, what the pill says and in which
// tone, what the next milestone is and the instant it is expected, and where the
// airline's mark is to be fetched from. This file draws that and nothing else. The two
// things it works out for itself depend on the phone's clock: the figure beside the
// milestone, and where the aircraft sits on the card's rule. The figure is built from
// the same units the web page uses: whole days once a day or more away, hours and
// minutes inside that, never seconds, and "ago" once the instant has gone by, at which
// point the label turns into the one the server said to use for a milestone that is due.
//
// The figure is a string, so it is only as fresh as the last reload, and iOS reloads
// widgets when it feels like it rather than when we ask. Nothing drawn here is finer
// than a minute, and the ask for a reload is made on the minute only while a milestone
// is close enough for a stale figure to be the first thing anyone notices.
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
// Inside this the figure is minutes, and a minute stale is a minute wrong.
const IMMINENT_MS = 60 * 60 * 1000;
// The milestone column on the home screen rows. Fixed, because a row with a flexible
// spacer in it hands each text an equal share of the width before the spacer takes the
// rest, and the detail line is cut short with room to spare. With the column's width
// known, the rest of the row is the card's to fill.
const MILESTONE_COLUMN = 84;
// The rule between the card's two airports, for the same reason: the aircraft's place
// on it is a share of a width the script has to know, and it takes what the codes
// leave of the row, so the widget's width has to be known too. Scriptable does not say
// how big the widget is, so the screen does: the home screen's widget sizes by screen,
// in points, as the side of the small size and the width of the medium. The small and
// medium sizes are as tall as the small is wide, and the large is as wide as the
// medium. A screen not listed gets the narrowest, which fits on every phone.
const WIDGET_SIZES = {
  "440x956": [170, 364],
  "430x932": [170, 364],
  "428x926": [170, 364],
  "414x896": [169, 360],
  "414x736": [159, 348],
  "402x874": [158, 338],
  "393x852": [158, 338],
  "390x844": [158, 338],
  "375x812": [155, 329],
  "375x667": [148, 321],
  "360x780": [155, 329],
  "320x568": [141, 292],
};
const NARROWEST = [148, 321];
const PLANE = 11;
// Either side of the rule, between it and the codes.
const RULE_GUTTER = 8;
// SF Mono advances this much of the point size per glyph, whatever the weight, which
// is what lets a monospaced text be given a box exactly its own width.
const MONO_ADVANCE = 0.62;
// The medium card's rows with nothing between them: the header, the codes, the times,
// the places on their two lines, and the figure, each as tall as its type.
const CARD_ROWS = 18 + 24 + 22 + 26 + 22;

// The web UI's palette, each value as styles/app.css defines it for the light and the
// dark scheme, rendered from oklch to sRGB. The phone's appearance picks the side, the
// same way the page does, so the widget is the card it would be on the board.
const BACKGROUND = Color.dynamic(new Color("#ffffff"), new Color("#14171e"));
const TEXT = Color.dynamic(new Color("#111720"), new Color("#e9edf2"));
const MUTED = Color.dynamic(new Color("#5f656e"), new Color("#989fa9"));
// The card's rule, before the aircraft has got to a point on it: the muted colour let
// half through, as the page draws it.
const RULE = Color.dynamic(new Color("#5f656e", 0.5), new Color("#989fa9", 0.5));

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
const TINT_ALPHA = 0.14;

// The one repair for a missing or rejected token, and the same sentence for both.
const RECONNECT_TEXT = "Open the settings page on this phone and tap Connect.";

// Declared up here because the widget is built before the rest of the file has run, and
// a class, unlike a function, does not exist until its line does.
class TokenRejected extends Error {}

const family = config.widgetFamily || "medium";
const isAccessory = family.startsWith("accessory");
// Per-element tap targets exist only on medium and large. Everywhere else the whole
// widget gets one URL.
const supportsRowLinks = family === "medium" || family === "large";
// The widget's margin. The small size is six lines tall once the route has its own
// line, so it gives up a little of it to keep the last of them on screen.
const INSET = family === "small" ? 12 : 14;
// The room inside the margin: the width at this size, and the height of the sizes a
// single flight has to itself.
const CONTENT = contentSize();
// The space between the card's rows on the large size, which has the board under the
// card and so cannot spread them over the height the way the medium size does: the
// points the medium size's rows are spread by on this phone, the height they leave
// over shared out between the gaps, and never tighter than the rows would be on their
// own.
const LARGE_GAP = Math.max(8, Math.floor((CONTENT.height - CARD_ROWS - 3) / 3));

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

async function load(server) {
  try {
    const data = await request(server);
    writeCache(data);
    return { data, stale: false, cachedAt: null, rejected: false, error: null };
  } catch (error) {
    // Not the cache: yesterday's flights with a small "Cached" mark would hide that the
    // token is wrong, and that is the one failure a reload never fixes.
    if (error instanceof TokenRejected) {
      return { data: null, stale: false, cachedAt: null, rejected: true, error: null };
    }
    // A stale widget beats a blank one. The flight has almost certainly not changed,
    // and the figure is measured against the phone's clock whether or not the network
    // came back.
    const cached = readCache();
    if (cached) {
      return { data: cached.data, stale: true, cachedAt: cached.cachedAt, rejected: false, error: null };
    }
    return {
      data: null,
      stale: false,
      cachedAt: null,
      rejected: false,
      error: String(error.message || error),
    };
  }
}

async function request({ api, token }) {
  const req = new Request(`${api}/api/widget`);
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
    message(widget, "No upcoming flights", staleNote(result));
    return widget;
  }

  // The flight the server gave the card to is the one the sizes with room for a
  // single flight show, so a leg about to leave takes over the Lock Screen the same
  // moment it takes over the home screen.
  const featured = flights.find((flight) => flight.card) || flights[0];

  // The Lock Screen draws everything in its own tint, which would turn a mark into a
  // blot, so only the home screen sizes carry one.
  if (isAccessory) {
    renderAccessory(widget, featured, result);
    return widget;
  }
  const logos = await loadLogos(flights);
  if (featured.card) {
    renderCard(widget, featured, logos);
    // Only the large size has room under the card for the rest of the board.
    const rest = family === "large" ? flights.filter((flight) => flight !== featured) : [];
    if (rest.length) {
      widget.addSpacer(10);
      divider(widget);
      widget.addSpacer(10);
      renderList(widget, rest, logos);
    }
  } else if (family === "small") {
    renderSmall(widget, featured, logos);
  } else {
    renderList(widget, flights, logos);
  }
  // The card spreads itself over the sizes it has to itself; elsewhere the rows keep
  // to the top and the footer goes to the bottom.
  if (!featured.card || family === "large") {
    widget.addSpacer();
  }
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

  const row = widget.addStack();
  row.centerAlignContent();
  row.spacing = 5;
  if (hasMilestone(flight)) {
    const label = row.addText(milestoneLabel(flight));
    label.font = Font.systemFont(11);
    label.textOpacity = 0.7;
    figureText(row, flight, Font.boldMonospacedSystemFont(17), null);
  } else {
    const state = row.addText(flight.status_label);
    state.font = Font.semiboldSystemFont(15);
    state.lineLimit = 1;
  }

  // The third line is the card's detail, else the pill's word, which the second line
  // has already used when there was nothing to count to.
  const detail = flight.detail || (hasMilestone(flight) ? flight.status_label : null);
  if (detail) {
    const text = widget.addText(detail);
    text.font = Font.systemFont(11);
    text.textOpacity = 0.7;
    text.lineLimit = 1;
  }
}

function renderSmall(widget, flight, logos) {
  // Small widgets get a single tap target, set on the widget rather than a row.
  widget.url = flight.detail_url;

  // Too narrow for the number and the route on one line at a size anyone can read, so
  // the route takes the line under the number instead of shrinking beside it.
  titleRow(widget, flight, logos, 13, false);
  const route = widget.addText(flight.route);
  route.font = Font.regularMonospacedSystemFont(11);
  route.textColor = MUTED;
  route.lineLimit = 1;

  widget.addSpacer(4);
  const line = widget.addStack();
  pill(line, flight);
  line.addSpacer();

  if (hasMilestone(flight)) {
    widget.addSpacer(4);
    const label = widget.addText(milestoneLabel(flight));
    label.font = Font.systemFont(11);
    label.textColor = MUTED;
    label.lineLimit = 1;
    figureText(widget, flight, Font.boldMonospacedSystemFont(24), TEXT);
  }

  if (flight.detail) {
    widget.addSpacer(2);
    const detail = widget.addText(flight.detail);
    detail.font = Font.systemFont(11);
    detail.textColor = MUTED;
    detail.lineLimit = 2;
    detail.minimumScaleFactor = 0.8;
  }
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
    row.centerAlignContent();
    row.spacing = 8;

    const left = row.addStack();
    left.layoutVertically();

    titleRow(left, flight, logos, 14, true);
    left.addSpacer(3);

    const line = left.addStack();
    line.centerAlignContent();
    line.spacing = 6;
    pill(line, flight);
    if (flight.detail) {
      // No scale factor: a text that can shrink is sized before the pill and handed
      // half the line, and is cut short with room beside it. One that only truncates
      // is sized after the pill and gets everything the pill left.
      const detail = line.addText(flight.detail);
      detail.font = Font.systemFont(11);
      detail.textColor = MUTED;
      detail.lineLimit = 1;
    }

    // A line of nothing but a spacer is what stretches the column to every point the
    // milestone column leaves, without sharing a line with any text.
    const stretch = left.addStack();
    stretch.addSpacer();
    stretch.size = new Size(0, 1);

    if (hasMilestone(flight)) {
      const right = row.addStack();
      right.layoutVertically();
      right.spacing = 1;
      right.size = new Size(MILESTONE_COLUMN, 0);
      // Each line sits behind a spacer of its own: that is what puts it against the
      // right edge, since a text aligns only within its own width.
      const labelLine = right.addStack();
      labelLine.addSpacer();
      const label = labelLine.addText(milestoneLabel(flight));
      label.font = Font.systemFont(10);
      label.textColor = MUTED;
      label.lineLimit = 1;
      label.minimumScaleFactor = 0.8;
      const figureLine = right.addStack();
      figureLine.addSpacer();
      figureText(figureLine, flight, Font.boldMonospacedSystemFont(21), TEXT);
    }
  });
}

// The board's card, a little tighter: the number and the pill; the two codes with the
// rule between them and the aircraft on it; the time at each end in its tone, with its
// zone on the outside the way the card sets it; the terminal and gate at each end, the
// word over the value; and what it counts to, the label on the left and the figure on
// the right as in the card's footer. The rows are spread over the height on the sizes
// the card has to itself. The small size has room for the codes, the times and the
// count, in smaller type, with the pill and the count's label on lines of their own.
//
// A text in a row with a flexible spacer is handed a share of the row rather than the
// width it needs, and is cut short with room beside it. So every monospaced text here
// sits in a box exactly its own width, and nothing but the spacers is left to flex. The
// rows set no spacing of their own, since that would go between the pieces of the rule.
function renderCard(widget, flight, logos) {
  const card = flight.card;
  const compact = family === "small";
  const container = widget.addStack();
  container.layoutVertically();
  if (supportsRowLinks) {
    container.url = flight.detail_url;
  } else {
    widget.url = flight.detail_url;
  }

  const size = compact ? 12 : 14;
  const header = container.addStack();
  header.centerAlignContent();
  const logo = logos[flight.logo_url];
  if (logo) {
    const mark = header.addImage(logo);
    mark.imageSize = new Size(size + 3, size + 3);
    mark.cornerRadius = 3;
    header.addSpacer(5);
  }
  mono(header, flight.number, size, "semibold", TEXT);
  if (compact) {
    container.addSpacer(3);
    pill(container.addStack(), flight);
  } else {
    header.addSpacer();
    pill(header, flight);
  }

  gap(container);
  routeRow(container, card, compact);
  // The times stay close under the codes: each end's code and time read as one.
  container.addSpacer(compact ? 2 : 3);
  timesRow(container, card, compact);
  if (family === "large") {
    container.addSpacer(1);
    daysRow(container, card);
  }
  if (!compact) {
    gap(container);
    placesRow(container, card);
  }

  if (hasMilestone(flight)) {
    gap(container);
    if (compact) {
      const label = container.addText(milestoneLabel(flight));
      label.font = Font.systemFont(10);
      label.textColor = MUTED;
      label.lineLimit = 1;
      figureText(container, flight, Font.boldMonospacedSystemFont(18), TEXT);
    } else {
      const line = container.addStack();
      line.bottomAlignContent();
      const label = line.addText(milestoneLabel(flight));
      label.font = Font.systemFont(11);
      label.textColor = MUTED;
      label.lineLimit = 1;
      line.addSpacer();
      figureText(line, flight, Font.boldMonospacedSystemFont(18), TEXT);
    }
  }
}

// A monospaced text in a box exactly its own width, so nothing in the row can hand it
// less than it needs.
function mono(row, text, size, weight, color) {
  const box = row.addStack();
  box.size = new Size(monoWidth(text, size), 0);
  const element = box.addText(text);
  element.font = monoFont(size, weight);
  element.textColor = color;
  element.lineLimit = 1;
  return element;
}

function monoWidth(text, size) {
  return Math.ceil(text.length * size * MONO_ADVANCE) + 1;
}

function monoFont(size, weight) {
  if (weight === "bold") return Font.boldMonospacedSystemFont(size);
  if (weight === "semibold") return Font.semiboldMonospacedSystemFont(size);
  if (weight === "medium") return Font.mediumMonospacedSystemFont(size);
  return Font.regularMonospacedSystemFont(size);
}

// The space between the card's rows: on the sizes the card has to itself, whatever
// spreads the rows over the widget's height; on the large size, the points the medium
// size's rows are spread by.
function gap(container) {
  if (family === "large") {
    container.addSpacer(LARGE_GAP);
  } else {
    container.addSpacer();
  }
}

function routeRow(container, card, compact) {
  const row = container.addStack();
  row.centerAlignContent();
  const origin = code(row, card.origin.iata, compact, null);
  row.addSpacer();
  // The rule takes what the codes leave of the row, bar a gutter either side; the
  // spacers soak up the point or two of rounding.
  const destination = codeWidth(card.destination.iata, compact, card.booked_destination);
  rule(row, card, CONTENT.width - origin - destination - 2 * RULE_GUTTER, compact);
  row.addSpacer();
  code(row, card.destination.iata, compact, card.booked_destination);
}

// A diverted flight's new airport stands where the code goes, in red, with the one it
// was booked for small beside it. Returns the width the group takes.
function code(row, iata, compact, booked) {
  const group = row.addStack();
  group.bottomAlignContent();
  mono(group, iata, compact ? 16 : 20, "bold", booked ? toneColor("stop") : TEXT);
  if (booked) {
    group.addSpacer(3);
    mono(group, booked, compact ? 9 : 11, "regular", MUTED);
  }
  return codeWidth(iata, compact, booked);
}

function codeWidth(iata, compact, booked) {
  const main = monoWidth(iata, compact ? 16 : 20);
  return booked ? main + 3 + monoWidth(booked, compact ? 9 : 11) : main;
}

// The rule from code to code. With the aircraft in the air it is drawn as far as the
// aircraft has got, in the page's plan colour, with the aircraft at the end of that and
// the rest faint; before wheels-up it carries how long the hop is, where the rule is
// long enough to hold the words; and with nothing to say it is a faint line.
function rule(row, card, width, compact) {
  const progress = ruleProgress(card);
  if (progress === null) {
    if (!card.block_time || compact) {
      stroke(row, width, RULE);
      return;
    }
    const label = monoWidth(card.block_time, 9);
    const half = Math.floor((width - label - 8) / 2);
    stroke(row, half, RULE);
    row.addSpacer(4);
    mono(row, card.block_time, 9, "regular", MUTED);
    row.addSpacer(4);
    stroke(row, half, RULE);
    return;
  }
  const span = width - PLANE - 2;
  const flown = Math.round(span * progress);
  stroke(row, flown, toneColor("plan"));
  row.addSpacer(1);
  const plane = row.addImage(SFSymbol.named("airplane").image);
  plane.imageSize = new Size(PLANE, PLANE);
  plane.tintColor = toneColor("plan");
  row.addSpacer(1);
  stroke(row, span - flown, RULE);
}

// A stack of a fixed width and one point of height is a line; a width of zero would
// mean "as wide as there is room", so the shortest line is a point long.
function stroke(row, width, color) {
  const bar = row.addStack();
  bar.size = new Size(Math.max(1, width), 1);
  bar.backgroundColor = color;
}

// A line the width of the widget, between the card and the rows under it.
function divider(widget) {
  const bar = widget.addStack();
  bar.addSpacer();
  bar.size = new Size(0, 1);
  bar.backgroundColor = RULE;
}

function timesRow(container, card, compact) {
  const row = container.addStack();
  row.bottomAlignContent();
  clock(row, card.origin, compact, false);
  row.addSpacer();
  clock(row, card.destination, compact, true);
}

// The time at one end, in its tone, with the zone small and grey on the outside: after
// a departure and before an arrival. The small size has no room for the zone.
function clock(row, end, compact, right) {
  const group = row.addStack();
  group.bottomAlignContent();
  const time = () => {
    mono(group, end.time, compact ? 15 : 18, "bold", end.tone ? toneColor(end.tone) : TEXT);
  };
  const zone = () => {
    if (compact || !end.zone) return;
    // Bottom-aligned, the zone's descender room is shallower than the time's, so its
    // baseline lands below the time's; a spacer under it makes up the difference.
    const lift = group.addStack();
    lift.layoutVertically();
    const text = lift.addText(end.zone);
    text.font = Font.mediumSystemFont(9);
    text.textColor = MUTED;
    text.lineLimit = 1;
    lift.addSpacer(2);
  };
  if (right) {
    zone();
    if (!compact && end.zone) group.addSpacer(3);
    time();
  } else {
    time();
    if (!compact && end.zone) group.addSpacer(3);
    zone();
  }
}

function daysRow(container, card) {
  const row = container.addStack();
  mono(row, card.origin.day || "", 10, "regular", MUTED);
  row.addSpacer();
  mono(row, card.destination.day || "", 10, "regular", MUTED);
}

// Where in the building at each end: terminal then gate where it leaves from, gate then
// terminal where it arrives, so the terminal sits on the outside at both ends and the
// gate beside the rule, as on the card, and as on the card each word stands over its
// value.
function placesRow(container, card) {
  const row = container.addStack();
  row.centerAlignContent();
  places(row, [["Term", card.origin.terminal, false], ["Gate", card.origin.gate, true]]);
  row.addSpacer();
  places(row, [["Gate", card.destination.gate, true], ["Term", card.destination.terminal, false]]);
}

function places(row, pairs) {
  const group = row.addStack();
  pairs.forEach(([name, value, isGate], index) => {
    if (index) group.addSpacer(8);
    const cell = group.addStack();
    cell.layoutVertically();
    cell.spacing = 1;
    const label = cell.addText(name.toUpperCase());
    label.font = Font.mediumSystemFont(8);
    label.textColor = MUTED;
    label.lineLimit = 1;
    const tone = value ? (isGate ? toneColor("plan") : TEXT) : MUTED;
    mono(cell, value || "-", 12, isGate && value ? "semibold" : "medium", tone);
  });
}

// The card's heading: the airline's mark, when it came, then the number, and the route
// beside it where the row is wide enough to hold both.
function titleRow(container, flight, logos, size, withRoute) {
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
  return row;
}

// The board's badge: the word in its tone, on the same tone let through the card.
function pill(container, flight) {
  const badge = container.addStack();
  badge.centerAlignContent();
  badge.setPadding(2, 6, 2, 6);
  badge.cornerRadius = 7;
  badge.backgroundColor = toneColor(flight.status_tone, TINT_ALPHA);
  const text = badge.addText(flight.status_label);
  text.font = Font.semiboldSystemFont(10);
  text.textColor = toneColor(flight.status_tone);
  text.lineLimit = 1;
  return badge;
}

function figureText(container, flight, font, color) {
  const element = container.addText(milestoneFigure(flight));
  element.font = font;
  if (color) {
    element.textColor = color;
  }
  element.lineLimit = 1;
  element.minimumScaleFactor = 0.6;
  return element;
}

function footer(widget, data, result) {
  const degraded = data.degraded ? data.degraded_reason || "Status may be out of date" : null;
  const note = degraded || staleNote(result);
  if (!note) {
    return;
  }
  const text = widget.addText(note);
  text.font = Font.systemFont(family === "small" ? 9 : 10);
  text.textColor = MUTED;
  text.lineLimit = 1;
  text.minimumScaleFactor = 0.7;
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
  const now = Date.now();
  const cadence = Math.max(data.refresh_seconds || 900, MIN_REFRESH_SECONDS) * 1000;
  // The feed's times are whole minutes, so a figure only ever changes on the minute.
  // Close to a milestone the figure is minutes and every one of them counts, so the
  // ask is the next minute; iOS grants it or not. Further out, the server's cadence
  // is the one to keep, since the data does not move faster than that.
  const imminent = (data.flights || []).some(
    (flight) => flight.milestone_to && Math.abs(new Date(flight.milestone_to).getTime() - now) < IMMINENT_MS,
  );
  const when = imminent ? now - (now % 60000) + 60000 : now + cadence;
  widget.refreshAfterDate = new Date(when);
}

// --- text ------------------------------------------------------------------------------

function toneColor(name, alpha = 1) {
  const [light, dark] = TONES[name] || TONES.quiet;
  return Color.dynamic(new Color(light, alpha), new Color(dark, alpha));
}

function hasMilestone(flight) {
  return Boolean(flight.milestone_to || flight.milestone_text);
}

// The same string the page builds from the same milliseconds: whole days past a day,
// then hours and minutes, then minutes, and never a second.
function figure(ms) {
  const total = Math.floor(Math.abs(ms) / 60000);
  const days = Math.floor(total / 1440);
  const hours = Math.floor(total / 60) % 24;
  const minutes = total % 60;
  if (days) return `${days}d`;
  if (hours) return `${hours}h ${minutes < 10 ? "0" : ""}${minutes}m`;
  if (minutes) return `${minutes}m`;
  return "<1m";
}

function until(instant) {
  const ms = new Date(instant).getTime() - Date.now();
  // A target in the past counts up: "20m ago" is a fact, "-20m" is arithmetic.
  return figure(ms) + (ms < 0 ? " ago" : "");
}

// The figure beside the milestone: counted to the instant, or the belt as given.
function milestoneFigure(flight) {
  return flight.milestone_to ? until(flight.milestone_to) : flight.milestone_text;
}

// The words in front of it change with it, so "at the gate in" becomes "due at the
// gate" the minute it passes, as they do on the page, without waiting for a reload.
function milestoneLabel(flight) {
  const passed = flight.milestone_to && new Date(flight.milestone_to).getTime() < Date.now();
  return passed && flight.milestone_due ? flight.milestone_due : flight.milestone_label;
}

function staleNote(result) {
  if (!result.stale) {
    return null;
  }
  return result.cachedAt ? `Cached ${timeOfDay(result.cachedAt)}` : "Cached";
}

// Where the aircraft sits on the card's rule, as a share of it, or null with nothing
// to place. Between wheels-up and the landing estimate the phone's clock moves it, the
// way the page moves it between loads; with no span to measure against the feed's own
// figure stands, which is also what a flight on the ground has.
function ruleProgress(card) {
  if (card.airborne_off && card.airborne_on) {
    const off = new Date(card.airborne_off).getTime();
    const on = new Date(card.airborne_on).getTime();
    if (on > off) {
      return Math.min(Math.max((Date.now() - off) / (on - off), 0), 1);
    }
  }
  return typeof card.progress === "number" ? card.progress / 100 : null;
}

function contentSize() {
  const screen = Device.screenSize();
  const key = `${Math.min(screen.width, screen.height)}x${Math.max(screen.width, screen.height)}`;
  const [side, medium] = WIDGET_SIZES[key] || NARROWEST;
  return { width: (family === "small" ? side : medium) - 2 * INSET, height: side - 2 * INSET };
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
