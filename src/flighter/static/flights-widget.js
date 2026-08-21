// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: deep-blue; icon-glyph: plane-departure;

// Flight tracker widget.
//
// The server decides everything: which flights, in what order, what each line says and
// which instant to count down to. This file draws that and nothing else, with one
// exception that matters more than all the drawing put together: the countdown is an
// iOS timer element, not a string. A string is wrong within a minute of being drawn,
// and iOS reloads widgets when it feels like it rather than when we ask.
//
// The matching trap: a timer whose date has passed counts *up*, with no sign and no
// marker, so "2:32" reads identically as "departs in 2m32s" and "departed 2m32s ago".
// Every timer here is gated on its date still being in the future.
//
// There is nothing to edit here. The server's address and the token arrive through the
// Connect button on the settings page, which runs this script with both in the URL, and
// live in the Keychain from then on. The script also keeps itself current: every run
// compares this file with the server's copy and replaces it when they differ.

const API_KEY = "flighter-api";
const TOKEN_KEY = "flighter-token";
const SCRIPT_PATH = "/static/flights-widget.js";
const CACHE_FILE = "flighter-widget.json";
const REQUEST_TIMEOUT_SECONDS = 15;
// iOS budgets reloads and ignores an eager request anyway, so do not ask for one.
const MIN_REFRESH_SECONDS = 60;

// Every colour is a light/dark pair, so the widget sits on whatever wallpaper it is
// given instead of always being a dark tile. Lock Screen widgets use none of these:
// iOS tints them itself.
const BG_TOP = pair("#f7f8fb", "#101725");
const BG_BOTTOM = pair("#e9edf5", "#070a12");
const TEXT = pair("#0b1220", "#ffffff");
const MUTED = pair("#5b6575", "#8a94a6");
const TRACK = Color.dynamic(new Color("#0b1220", 0.12), new Color("#ffffff", 0.18));
const DELAYED = pair("#b45309", "#ffb454");

const PHASE_COLOR = {
  upcoming: pair("#2f5fd0", "#7aa2f7"),
  day_of: pair("#2f5fd0", "#7aa2f7"),
  taxiing: pair("#b45309", "#ffb454"),
  airborne: pair("#0f766e", "#4ec9b0"),
  landed: pair("#5b6575", "#8a94a6"),
  cancelled: pair("#b91c1c", "#ff6b6b"),
  diverted: pair("#c2410c", "#ff9e64"),
};

// The neutral name of a phase, for status lines with nothing better to say.
const PHASE_TEXT = {
  upcoming: "Upcoming",
  day_of: "Today",
  taxiing: "Taxiing",
  airborne: "In the air",
  landed: "Landed",
  cancelled: "Cancelled",
  diverted: "Diverted",
};

// What stands in for the countdown once its instant has gone by, in the present tense
// it has moved into. Never a number, because a number here would be counting upwards.
const OVERDUE_TEXT = {
  upcoming: "Departing",
  day_of: "Departing",
  airborne: "Landing",
};

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

const server = connect();
const widget = server ? await buildWidget(server) : setupWidget();

if (config.runsInWidget) {
  Script.setWidget(widget);
  if (server) {
    // Silently: the new copy draws the next refresh, and nobody is watching this one.
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
  // Opened from the settings page: scriptable:///run/Flights?api=...&token=... Each
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
    // and the live timer keeps ticking whether or not the network came back.
    const cached = readCache();
    if (cached) {
      return { data: cached.data, stale: true, cachedAt: cached.cachedAt, rejected: false, error: null };
    }
    return { data: null, stale: false, cachedAt: null, rejected: false, error: describe(error, server.api) };
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

// A sentence for the widget rather than the exception's own words.
function describe(error, api) {
  const text = String(error.message || error);
  const answered = text.match(/^server returned (\d+)/);
  if (answered) {
    return `The server answered ${answered[1]}.`;
  }
  const host = api.match(/^https?:\/\/([^/]+)/);
  return `Could not reach ${host ? host[1] : api}.`;
}

async function updateScript(api) {
  // Scripts are plain files in Scriptable's documents folder, so this one can replace
  // itself with the server's copy, and a server deploy reaches every phone by itself.
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

// --- widget ----------------------------------------------------------------------------

async function buildWidget(server) {
  const result = await load(server);
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
    message(widget, "No upcoming flights", footnote(data, result));
    return widget;
  }

  if (isAccessory) {
    renderAccessory(widget, flights[0], data, result);
  } else if (family === "small") {
    renderSmall(widget, flights[0], data, result);
  } else {
    renderList(widget, flights, data, result);
  }
  return widget;
}

function newWidget() {
  const widget = new ListWidget();
  if (isAccessory) {
    // Lets the Lock Screen paint its own adaptive backdrop behind the content.
    widget.addAccessoryWidgetBackground = true;
    widget.setPadding(2, 2, 2, 2);
  } else {
    const gradient = new LinearGradient();
    gradient.colors = [BG_TOP, BG_BOTTOM];
    gradient.locations = [0, 1];
    widget.backgroundGradient = gradient;
    widget.setPadding(14, 14, 14, 14);
  }
  return widget;
}

function renderAccessory(widget, flight, data, result) {
  // Roughly three lines above the clock, and one tap target for the lot. No colours:
  // the Lock Screen tints everything itself, so a delay has to be said in words.
  widget.url = flight.detail_url;

  const head = widget.addStack();
  head.centerAlignContent();
  head.spacing = 5;
  const route = head.addText(routeOf(flight));
  route.font = Font.semiboldSystemFont(13);
  route.lineLimit = 1;
  route.minimumScaleFactor = 0.8;
  const number = head.addText(flight.flight_number);
  number.font = Font.systemFont(11);
  number.textOpacity = 0.7;
  number.lineLimit = 1;

  const middle = widget.addStack();
  middle.centerAlignContent();
  middle.spacing = 5;
  timeSlot(middle, flight, {
    labelFont: Font.systemFont(11),
    labelOpacity: 0.7,
    timerFont: Font.boldRoundedSystemFont(17),
    wordFont: Font.semiboldSystemFont(15),
  });

  const detail = widget.addText([statusFor(flight), footnote(data, result)].filter(Boolean).join(" · "));
  detail.font = Font.systemFont(11);
  detail.textOpacity = 0.7;
  detail.lineLimit = 1;
  detail.minimumScaleFactor = 0.8;
}

function renderSmall(widget, flight, data, result) {
  // Small widgets get a single tap target, set on the widget rather than a row.
  widget.url = flight.detail_url;

  const route = widget.addText(routeOf(flight));
  route.font = Font.semiboldSystemFont(16);
  route.textColor = TEXT;
  route.lineLimit = 1;
  route.minimumScaleFactor = 0.7;
  const number = widget.addText(flight.flight_number);
  number.font = Font.systemFont(11);
  number.textColor = MUTED;

  widget.addSpacer(4);
  timeSlot(widget, flight, {
    labelFont: Font.systemFont(11),
    labelColor: PHASE_COLOR[flight.phase] || MUTED,
    timerFont: Font.boldRoundedSystemFont(30),
    timerColor: TEXT,
    wordFont: Font.boldRoundedSystemFont(24),
    wordColor: PHASE_COLOR[flight.phase] || TEXT,
  });

  const status = widget.addText(statusFor(flight));
  status.font = Font.systemFont(11);
  status.textColor = MUTED;
  status.lineLimit = 2;

  if (hasProgress(flight)) {
    widget.addSpacer(6);
    progressBar(widget, flight.progress_percent, 110);
  }

  widget.addSpacer();
  footer(widget, data, result);
}

function renderList(widget, flights, data, result) {
  const barWidth = family === "large" ? 150 : 110;

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

    const marker = row.addStack();
    marker.size = new Size(3, family === "large" ? 34 : 28);
    marker.cornerRadius = 1.5;
    marker.backgroundColor = PHASE_COLOR[flight.phase] || MUTED;

    const left = row.addStack();
    left.layoutVertically();
    left.spacing = 2;

    const route = left.addText(routeOf(flight));
    route.font = Font.semiboldSystemFont(14);
    route.textColor = TEXT;
    route.lineLimit = 1;
    route.minimumScaleFactor = 0.7;

    const detail = left.addText(`${flight.flight_number} · ${statusFor(flight)}`);
    detail.font = Font.systemFont(11);
    detail.textColor = MUTED;
    detail.lineLimit = 1;

    if (hasProgress(flight)) {
      left.addSpacer(4);
      progressBar(left, flight.progress_percent, barWidth);
    }

    row.addSpacer();

    const right = row.addStack();
    right.layoutVertically();
    right.spacing = 1;
    timeSlot(right, flight, {
      align: "right",
      labelFont: Font.systemFont(10),
      labelColor: MUTED,
      timerFont: Font.boldRoundedSystemFont(21),
      timerColor: TEXT,
      wordFont: Font.boldRoundedSystemFont(15),
      wordColor: PHASE_COLOR[flight.phase] || MUTED,
    });
  });

  widget.addSpacer();
  footer(widget, data, result);
}

// The middle of every layout, and the same decision in all of them: the date for a
// flight still days away, the live timer while there is something to count, and the
// present-tense word once there is not. A layout passes fonts and colours, not logic.
function timeSlot(container, flight, style) {
  if (flight.departs) {
    label(container, "Departs", style);
    const when = container.addText(flight.departs);
    when.font = style.wordFont;
    tint(when, style.timerColor);
    when.lineLimit = 1;
    when.minimumScaleFactor = 0.5;
    align(when, style);
    return;
  }
  if (isLive(flight)) {
    label(container, flight.countdown_label, style);
    countdown(container, flight, style);
    return;
  }
  const word = container.addText(overdueText(flight));
  word.font = style.wordFont;
  tint(word, style.wordColor);
  word.lineLimit = 1;
  word.minimumScaleFactor = 0.7;
  align(word, style);
}

function label(container, text, style) {
  const element = container.addText(text);
  element.font = style.labelFont;
  tint(element, style.labelColor);
  if (style.labelOpacity) {
    element.textOpacity = style.labelOpacity;
  }
  element.lineLimit = 1;
  align(element, style);
}

// The whole point of the file: a system timer element, which ticks with no reload and
// no network. Only ever called for an instant that is still ahead of us.
function countdown(container, flight, style) {
  const element = container.addDate(new Date(flight.countdown_to));
  element.applyTimerStyle();
  element.font = style.timerFont;
  tint(element, flight.delay_minutes ? DELAYED : style.timerColor);
  element.lineLimit = 1;
  element.minimumScaleFactor = 0.6;
  align(element, style);
  return element;
}

function tint(element, color) {
  // Lock Screen elements are left untinted, and pass no colour, so iOS can do its own.
  if (color) {
    element.textColor = color;
  }
}

function align(element, style) {
  if (style.align === "right") {
    element.rightAlignText();
  }
}

function progressBar(container, percent, width) {
  const clamped = Math.max(0, Math.min(100, percent));
  const track = container.addStack();
  track.size = new Size(width, 4);
  track.cornerRadius = 2;
  track.backgroundColor = TRACK;
  const fill = track.addStack();
  fill.size = new Size((width * clamped) / 100, 4);
  fill.cornerRadius = 2;
  fill.backgroundColor = PHASE_COLOR.airborne;
}

function footer(widget, data, result) {
  const note = footnote(data, result);
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
  tint(title, isAccessory ? null : TEXT);
  title.centerAlignText();
  if (detail) {
    const body = widget.addText(detail);
    body.font = Font.systemFont(isAccessory ? 10 : 11);
    tint(body, isAccessory ? null : MUTED);
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
  let when = now + Math.max(data.refresh_seconds || 900, MIN_REFRESH_SECONDS) * 1000;
  for (const flight of data.flights || []) {
    if (!flight.countdown_to) {
      continue;
    }
    // Ask for a reload at the moment each countdown expires, which is when the drawing
    // has to change from a timer to a word. WidgetKit treats this as a hint, which is
    // exactly why the gate in isLive() has to exist as well.
    const target = new Date(flight.countdown_to).getTime();
    if (target > now + MIN_REFRESH_SECONDS * 1000 && target < when) {
      when = target;
    }
  }
  widget.refreshAfterDate = new Date(when);
}

// --- text ------------------------------------------------------------------------------

function pair(light, dark) {
  return Color.dynamic(new Color(light), new Color(dark));
}

function routeOf(flight) {
  return `${flight.origin} → ${flight.dest}`;
}

function isLive(flight) {
  return Boolean(flight.countdown_to) && new Date(flight.countdown_to).getTime() > Date.now();
}

function hasProgress(flight) {
  return flight.progress_percent !== null && flight.progress_percent !== undefined;
}

function phaseText(flight) {
  return PHASE_TEXT[flight.phase] || flight.phase;
}

function overdueText(flight) {
  return OVERDUE_TEXT[flight.phase] || phaseText(flight);
}

// Gate, belt or seat, and how late, on every family alike.
function statusFor(flight) {
  const parts = [];
  if (flight.subtitle) {
    parts.push(flight.subtitle);
  }
  if (flight.delay_minutes) {
    parts.push(`Delayed ${durationText(flight.delay_minutes)}`);
  }
  return parts.join(" · ") || phaseText(flight);
}

// `45m`, `1h 20m`: the same shape the web pages use.
function durationText(minutes) {
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return hours ? `${hours}h ${String(rest).padStart(2, "0")}m` : `${minutes}m`;
}

// What the server says is wrong with its numbers, or else that these are old ones.
function footnote(data, result) {
  if (data.degraded) {
    return data.degraded_reason || "Status may be out of date";
  }
  if (!result.stale) {
    return null;
  }
  return result.cachedAt ? `Cached ${timeOfDay(result.cachedAt)}` : "Cached";
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
  } else if (isAccessory) {
    await widget.presentAccessoryRectangular();
  } else {
    await widget.presentMedium();
  }
}
