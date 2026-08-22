// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: deep-blue; icon-glyph: plane-departure;

// Flight tracker widget.
//
// The server decides everything: which flights, in what order, what the pill says and
// in which tone, the one line under it, and where the airline's mark is to be fetched
// from. This file draws that and nothing else. Nothing here is measured against the
// phone's clock: iOS reloads a widget when it feels like it, about every quarter of an
// hour, and a figure counted from the clock is that far wrong by the time it is drawn
// again. A time read at the airport is right until the estimate itself moves.
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
    // and the footer says how old what is drawn is.
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
    message(widget, "No upcoming flights", staleNote(result));
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
    renderSmall(widget, flights[0], logos);
  } else {
    renderList(widget, flights, logos);
  }
  widget.addSpacer();
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
    // The small size is six lines tall once the route has its own line, so it gives up
    // a little margin to keep the last of them on screen.
    const inset = family === "small" ? 12 : 14;
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

  const state = widget.addText(flight.status_label);
  state.font = Font.semiboldSystemFont(15);
  state.lineLimit = 1;

  if (flight.detail) {
    const text = widget.addText(flight.detail);
    text.font = Font.systemFont(11);
    text.textOpacity = 0.7;
    text.lineLimit = 1;
    text.minimumScaleFactor = 0.8;
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

  widget.addSpacer(6);
  const line = widget.addStack();
  pill(line, flight);
  line.addSpacer();

  if (flight.detail) {
    widget.addSpacer(4);
    // The width here is one column, so the line wraps rather than being cut: the
    // gate and the seat are worth a second line when there is nothing else on screen.
    const detail = widget.addText(flight.detail);
    detail.font = Font.systemFont(12);
    detail.textColor = TEXT;
    detail.lineLimit = 3;
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
    row.layoutVertically();

    titleRow(row, flight, logos, 14, true);
    row.addSpacer(4);

    const line = row.addStack();
    line.centerAlignContent();
    line.spacing = 6;
    pill(line, flight);
    if (flight.detail) {
      // No scale factor: a text that can shrink is sized before the pill and handed
      // half the line, and is cut short with room beside it. One that only truncates
      // is sized after the pill and gets everything the pill left. The time leads the
      // line, so what a narrow row loses is the seat rather than the flight.
      const detail = line.addText(flight.detail);
      detail.font = Font.systemFont(11);
      detail.textColor = TEXT;
      detail.lineLimit = 1;
    }
    line.addSpacer();
  });
}

// The heading: the airline's mark, when it came, then the number, and the route beside
// it where the row is wide enough to hold both.
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
  row.addSpacer();
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

function staleNote(result) {
  if (!result.stale) {
    return null;
  }
  return result.cachedAt ? `Cached ${timeOfDay(result.cachedAt)}` : "Cached";
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
