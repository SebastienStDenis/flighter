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
// marker, so "2:32" reads identically as "boards in 2m32s" and "boarded 2m32s ago".
// Every timer here is gated on its date still being in the future.

const API = "https://flights.example.com"; // your own hostname, no trailing slash
const TOKEN_KEY = "flighter-token";
const CACHE_FILE = "flighter-widget.json";
const REQUEST_TIMEOUT_SECONDS = 15;
// iOS budgets reloads and ignores an eager request anyway, so do not ask for one.
const MIN_REFRESH_SECONDS = 60;

const BG_TOP = new Color("#101725");
const BG_BOTTOM = new Color("#070a12");
const TEXT = new Color("#ffffff");
const MUTED = new Color("#8a94a6");
const TRACK = new Color("#ffffff", 0.18);

const PHASE_COLOR = {
  upcoming: new Color("#7aa2f7"),
  day_of: new Color("#7aa2f7"),
  boarding: new Color("#ffb454"),
  airborne: new Color("#4ec9b0"),
  landed: new Color("#8a94a6"),
  cancelled: new Color("#ff6b6b"),
  diverted: new Color("#ff9e64"),
};

// The neutral name of a phase, for subtitles and status lines.
const PHASE_TEXT = {
  upcoming: "Upcoming",
  day_of: "Today",
  boarding: "Boarding",
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
  boarding: "Boarding",
  airborne: "Landing",
};

const family = config.widgetFamily || "medium";
const isAccessory = family.startsWith("accessory");
// Per-element tap targets exist only on medium and large. Everywhere else the whole
// widget gets one URL.
const supportsRowLinks = family === "medium" || family === "large";

const token = await resolveToken();
const widget = token ? await buildWidget(token) : setupWidget();

if (config.runsInWidget) {
  Script.setWidget(widget);
} else {
  await present(widget);
}
Script.complete();

// --- data ------------------------------------------------------------------------------

async function resolveToken() {
  // Keychain.get() throws on a missing key, so contains() is not optional.
  if (Keychain.contains(TOKEN_KEY)) {
    return Keychain.get(TOKEN_KEY);
  }
  // A widget cannot show an alert, so only an in-app run can fix a missing token.
  if (config.runsInApp) {
    return await promptForToken();
  }
  return null;
}

async function promptForToken() {
  const alert = new Alert();
  alert.title = "Flight widget token";
  alert.message = "Paste the value of WIDGET_TOKEN from your flight tracker server.";
  alert.addSecureTextField("token", "");
  alert.addAction("Save");
  alert.addCancelAction("Cancel");
  const choice = await alert.present();
  if (choice !== 0) {
    return null;
  }
  const value = alert.textFieldValue(0).trim();
  if (!value) {
    return null;
  }
  Keychain.set(TOKEN_KEY, value);
  return value;
}

async function load(token) {
  try {
    const data = await request(token);
    writeCache(data);
    return { data, stale: false, cachedAt: null, error: null };
  } catch (error) {
    // A stale widget beats a blank one. The flight has almost certainly not changed,
    // and the live timer keeps ticking whether or not the network came back.
    const cached = readCache();
    if (cached) {
      return { data: cached.data, stale: true, cachedAt: cached.cachedAt, error: null };
    }
    return { data: null, stale: false, cachedAt: null, error: String(error.message || error) };
  }
}

async function request(token) {
  const req = new Request(`${API}/api/widget`);
  req.headers = { Authorization: `Bearer ${token}` };
  req.timeoutInterval = REQUEST_TIMEOUT_SECONDS;
  const body = await req.loadJSON();
  const status = req.response.statusCode;
  if (status === 401) {
    throw new Error("token rejected");
  }
  if (status !== 200) {
    throw new Error(`server returned ${status}`);
  }
  return body;
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

async function buildWidget(token) {
  const result = await load(token);
  const widget = newWidget();

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
    const gradient = new LinearGradient();
    gradient.colors = [BG_TOP, BG_BOTTOM];
    gradient.locations = [0, 1];
    widget.backgroundGradient = gradient;
    widget.setPadding(14, 14, 14, 14);
  }
  return widget;
}

function renderAccessory(widget, flight, result) {
  // Roughly three lines above the clock, and one tap target for the lot.
  widget.url = flight.detail_url;

  const title = widget.addText(result.stale ? `${flight.title} ·` : flight.title);
  title.font = Font.semiboldSystemFont(13);
  title.lineLimit = 1;
  title.minimumScaleFactor = 0.7;

  const row = widget.addStack();
  row.centerAlignContent();
  row.spacing = 5;
  if (isLive(flight)) {
    const label = row.addText(flight.countdown_label);
    label.font = Font.systemFont(11);
    label.textOpacity = 0.7;
    countdown(row, flight, Font.boldRoundedSystemFont(17), null);
  } else {
    const state = row.addText(overdueText(flight));
    state.font = Font.semiboldSystemFont(15);
    state.lineLimit = 1;
  }

  const detail = widget.addText(flight.subtitle || phaseText(flight));
  detail.font = Font.systemFont(11);
  detail.textOpacity = 0.7;
  detail.lineLimit = 1;
}

function renderSmall(widget, flight, data, result) {
  // Small widgets get a single tap target, set on the widget rather than a row.
  widget.url = flight.detail_url;

  const title = widget.addText(flight.title);
  title.font = Font.semiboldSystemFont(13);
  title.textColor = TEXT;
  title.lineLimit = 1;
  title.minimumScaleFactor = 0.7;

  widget.addSpacer(2);
  if (isLive(flight)) {
    const label = widget.addText(flight.countdown_label);
    label.font = Font.systemFont(11);
    label.textColor = PHASE_COLOR[flight.phase] || MUTED;
    countdown(widget, flight, Font.boldRoundedSystemFont(30), TEXT);
  } else {
    const state = widget.addText(overdueText(flight));
    state.font = Font.boldRoundedSystemFont(24);
    state.textColor = PHASE_COLOR[flight.phase] || TEXT;
    state.lineLimit = 1;
    state.minimumScaleFactor = 0.6;
  }

  if (flight.subtitle) {
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

    const marker = row.addStack();
    marker.size = new Size(3, family === "large" ? 34 : 28);
    marker.cornerRadius = 1.5;
    marker.backgroundColor = PHASE_COLOR[flight.phase] || MUTED;

    const left = row.addStack();
    left.layoutVertically();
    left.spacing = 2;

    const title = left.addText(flight.title);
    title.font = Font.semiboldSystemFont(14);
    title.textColor = TEXT;
    title.lineLimit = 1;
    title.minimumScaleFactor = 0.7;

    const subtitle = left.addText(subtitleFor(flight));
    subtitle.font = Font.systemFont(11);
    subtitle.textColor = MUTED;
    subtitle.lineLimit = 1;

    if (hasProgress(flight)) {
      left.addSpacer(4);
      progressBar(left, flight.progress_percent, barWidth);
    }

    row.addSpacer();

    const right = row.addStack();
    right.layoutVertically();
    right.spacing = 1;

    if (isLive(flight)) {
      const label = right.addText(flight.countdown_label);
      label.font = Font.systemFont(10);
      label.textColor = MUTED;
      label.rightAlignText();
      label.lineLimit = 1;
      countdown(right, flight, Font.boldRoundedSystemFont(21), TEXT, { align: "right" });
    } else {
      const state = right.addText(overdueText(flight));
      state.font = Font.boldRoundedSystemFont(15);
      state.textColor = PHASE_COLOR[flight.phase] || MUTED;
      state.rightAlignText();
      state.lineLimit = 1;
      state.minimumScaleFactor = 0.7;
    }
  });

  widget.addSpacer();
  footer(widget, data, result);
}

// The whole point of the file: a system timer element, which ticks with no reload and
// no network. Only ever called for an instant that is still ahead of us.
function countdown(container, flight, font, color, options = {}) {
  const element = container.addDate(new Date(flight.countdown_to));
  element.applyTimerStyle();
  element.font = font;
  if (color) {
    element.textColor = flight.delayed ? PHASE_COLOR.diverted : color;
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
  fill.backgroundColor = PHASE_COLOR.airborne;
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
  message(widget, "Setup needed", "Run this script in the Scriptable app to store your token.");
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

function subtitleFor(flight) {
  const parts = [];
  if (flight.subtitle) {
    parts.push(flight.subtitle);
  }
  if (flight.delayed) {
    parts.push("Delayed");
  }
  if (!flight.is_self) {
    parts.push(flight.passenger);
  }
  return parts.join(" · ") || phaseText(flight);
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
