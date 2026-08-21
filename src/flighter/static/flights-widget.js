// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: deep-blue; icon-glyph: plane-departure;

// Flight tracker widget.
//
// The server decides everything: which flights, in what order, what the pill says and
// in which tone, what the next milestone is and the instant it is expected. This file
// draws that and nothing else. The one thing it works out for itself is the figure
// beside the milestone, because that depends on the phone's clock. It is built from the
// same units the web page uses: whole days once a day or more away, hours and minutes
// inside that, never seconds, and "ago" once the instant has gone by.
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
const REQUEST_TIMEOUT_SECONDS = 15;
// iOS budgets reloads and ignores an eager request anyway, so do not ask for one.
const MIN_REFRESH_SECONDS = 60;
// Inside this the figure is minutes, and a minute stale is a minute wrong.
const IMMINENT_MS = 60 * 60 * 1000;

// The web UI's palette, each value as styles/app.css defines it for the light and the
// dark scheme, rendered from oklch to sRGB. The phone's appearance picks the side, the
// same way the page does, so the widget is the card it would be on the board.
const BACKGROUND = Color.dynamic(new Color("#ffffff"), new Color("#14171e"));
const TEXT = Color.dynamic(new Color("#111720"), new Color("#e9edf2"));
const MUTED = Color.dynamic(new Color("#5f656e"), new Color("#989fa9"));
const TRACK = Color.dynamic(new Color("#dce0e5"), new Color("#2c3039"));

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

const server = connect();
const widget = server ? await buildWidget(server) : setupWidget();

if (config.runsInWidget) {
  Script.setWidget(widget);
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
  // itself with the server's copy and the widget ships with the server. Only from a run
  // in the app: a widget has no way of saying it happened.
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
    message(widget, "No upcoming flights", staleNote(result));
    return widget;
  }

  if (isAccessory) {
    renderAccessory(widget, flights[0], result);
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
    widget.backgroundColor = BACKGROUND;
    widget.setPadding(14, 14, 14, 14);
  }
  return widget;
}

function renderAccessory(widget, flight, result) {
  // Roughly three lines above the clock, and one tap target for the lot. The Lock
  // Screen draws everything in its own tint, so nothing here is given a colour.
  widget.url = flight.detail_url;

  const title = widget.addText(result.stale ? `${flight.title} ·` : flight.title);
  title.font = Font.semiboldMonospacedSystemFont(13);
  title.lineLimit = 1;
  title.minimumScaleFactor = 0.7;

  const row = widget.addStack();
  row.centerAlignContent();
  row.spacing = 5;
  if (hasMilestone(flight)) {
    const label = row.addText(flight.milestone_label);
    label.font = Font.systemFont(11);
    label.textOpacity = 0.7;
    figureText(row, flight, Font.boldMonospacedSystemFont(17), null);
  } else {
    const state = row.addText(flight.status_label);
    state.font = Font.semiboldSystemFont(15);
    state.lineLimit = 1;
  }

  // The third line is where the aircraft is, else the pill's word, which the second
  // line has already used when there was nothing to count to.
  const detail = flight.subtitle || (hasMilestone(flight) ? flight.status_label : null);
  if (detail) {
    const text = widget.addText(detail);
    text.font = Font.systemFont(11);
    text.textOpacity = 0.7;
    text.lineLimit = 1;
  }
}

function renderSmall(widget, flight, data, result) {
  // Small widgets get a single tap target, set on the widget rather than a row.
  widget.url = flight.detail_url;

  const title = widget.addText(flight.title);
  title.font = Font.semiboldMonospacedSystemFont(13);
  title.textColor = TEXT;
  title.lineLimit = 1;
  title.minimumScaleFactor = 0.7;

  widget.addSpacer(4);
  const line = widget.addStack();
  pill(line, flight);
  line.addSpacer();

  if (hasMilestone(flight)) {
    widget.addSpacer(4);
    const label = widget.addText(flight.milestone_label);
    label.font = Font.systemFont(11);
    label.textColor = MUTED;
    figureText(widget, flight, Font.boldMonospacedSystemFont(28), TEXT);
  }

  if (flight.subtitle) {
    widget.addSpacer(2);
    const subtitle = widget.addText(flight.subtitle);
    subtitle.font = Font.systemFont(11);
    subtitle.textColor = MUTED;
    subtitle.lineLimit = 2;
  }

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

    const left = row.addStack();
    left.layoutVertically();
    left.spacing = 3;

    const title = left.addText(flight.title);
    title.font = Font.semiboldMonospacedSystemFont(14);
    title.textColor = TEXT;
    title.lineLimit = 1;
    title.minimumScaleFactor = 0.7;

    const line = left.addStack();
    line.centerAlignContent();
    line.spacing = 6;
    pill(line, flight);
    if (flight.subtitle) {
      const subtitle = line.addText(flight.subtitle);
      subtitle.font = Font.systemFont(11);
      subtitle.textColor = MUTED;
      subtitle.lineLimit = 1;
      subtitle.minimumScaleFactor = 0.7;
    }

    if (hasProgress(flight)) {
      left.addSpacer(2);
      progressBar(left, flight.progress_percent, barWidth);
    }

    row.addSpacer();

    if (hasMilestone(flight)) {
      const right = row.addStack();
      right.layoutVertically();
      right.spacing = 1;
      const label = right.addText(flight.milestone_label);
      label.font = Font.systemFont(10);
      label.textColor = MUTED;
      label.rightAlignText();
      label.lineLimit = 1;
      figureText(right, flight, Font.boldMonospacedSystemFont(21), TEXT, { align: "right" });
    }
  });

  widget.addSpacer();
  footer(widget, data, result);
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

function figureText(container, flight, font, color, options = {}) {
  const element = container.addText(until(flight.milestone_to));
  element.font = font;
  if (color) {
    element.textColor = color;
  }
  element.lineLimit = 1;
  element.minimumScaleFactor = 0.6;
  if (options.align === "right") {
    element.rightAlignText();
  }
  return element;
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
  fill.backgroundColor = toneColor("plan");
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
    (flight) => hasMilestone(flight) && Math.abs(new Date(flight.milestone_to).getTime() - now) < IMMINENT_MS,
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
  return Boolean(flight.milestone_to);
}

function hasProgress(flight) {
  return flight.progress_percent !== null && flight.progress_percent !== undefined;
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

function staleNote(result) {
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
