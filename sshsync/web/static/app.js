/* SSH-Sync web interface.
 *
 * Talks to the FastAPI backend over REST for config and history, and over a
 * WebSocket for live run progress. No framework, no build step: the file is
 * served as-is and runs straight from the browser.
 */
"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

/* -- helpers ----------------------------------------------------------- */

const BINARY_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];

function formatBytes(bytes) {
  let value = Number(bytes) || 0;
  for (const unit of BINARY_UNITS) {
    if (value < 1024 || unit === "PiB") {
      return unit === "B" ? `${Math.round(value)} B` : `${value.toFixed(2)} ${unit}`;
    }
    value /= 1024;
  }
  return "0 B";
}

function countAndSize(count, bytes) {
  return `${count} (${formatBytes(bytes)})`;
}

function formatTimestamp(value) {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? String(value)
    : parsed.toLocaleString(undefined, { hour12: false });
}

// Largest-first, so the loop below picks the coarsest unit that still fits.
// Months and years are approximations; at that distance nobody is counting days.
const RELATIVE_UNITS = [
  ["year", 31536000],
  ["month", 2592000],
  ["week", 604800],
  ["day", 86400],
  ["hour", 3600],
  ["minute", 60],
];

/** Render a timestamp as "3 minutes ago" / "6 months ago", in the page locale. */
function formatRelative(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value || "");

  const seconds = (parsed.getTime() - Date.now()) / 1000;
  const magnitude = Math.abs(seconds);
  if (magnitude < 45) return "just now";

  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (const [unit, size] of RELATIVE_UNITS) {
    if (magnitude >= size) return formatter.format(Math.round(seconds / size), unit);
  }
  return formatter.format(Math.round(seconds), "second");
}

/** A span whose text ages on its own; the exact date stays in the tooltip. */
function relativeTime(value) {
  const element = document.createElement("span");
  element.dataset.relative = value;
  element.textContent = formatRelative(value);
  element.dataset.tip = formatTimestamp(value);
  return element;
}

// Ticked every second rather than lazily: right after a job finishes its card
// says "just now", and the second-by-second refresh is what makes that reading
// obviously live rather than a stale figure left over from the last render.
const RELATIVE_TICK_MS = 1000;

setInterval(() => {
  $$("[data-relative]").forEach((element) => {
    const text = formatRelative(element.dataset.relative);
    // Only touch the DOM on an actual change; most ticks are no-ops.
    if (element.textContent !== text) element.textContent = text;
  });
}, RELATIVE_TICK_MS);

function clockTime(value) {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? ""
    : parsed.toLocaleTimeString(undefined, { hour12: false });
}

/** Split a textarea into a trimmed list, dropping blank lines. */
function linesToList(text) {
  return String(text || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

/** Build an <svg><use> node referencing one of the inline Lucide symbols.
 *
 * The sprite holds geometry only; `.lucide` supplies the stroke, width and
 * caps, which inherit through <use> into the symbol.
 */
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#icon-${name}`);
  svg.append(use);
  svg.setAttribute("class", "lucide");
  svg.setAttribute("aria-hidden", "true");
  return svg;
}

/** Expand `data-icon="play"` on static markup into a real icon node.
 *
 * Buttons declare which icon they want and this fills it in, rather than every
 * one of them carrying four lines of inline SVG.
 */
function expandButtonIcons(root = document) {
  $$("[data-icon]", root).forEach((element) => {
    element.prepend(icon(element.dataset.icon));
  });
}

/* -- theme ---------------------------------------------------------------
 *
 * The palette itself lives in CSS, keyed off `data-theme` on <html>, which the
 * inline script in index.html sets before the first paint. This only keeps the
 * toggle in sync with it.
 */

const THEME_KEY = "sshsync-theme";
const systemTheme = matchMedia("(prefers-color-scheme: light)");

/** The user's explicit choice, or null while they are still following the OS. */
function storedTheme() {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch (error) {
    return null; // private mode: the toggle still works, it just won't persist
  }
}

/** Paint one theme, and point the button at the other one. */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;

  const button = $("#btn-theme");
  const next = theme === "dark" ? "light" : "dark";
  button.textContent = "";
  button.append(icon(theme === "dark" ? "sun" : "moon"));
  button.dataset.tip = `Switch to ${next} theme`;
  button.setAttribute("aria-label", `Switch to ${next} theme`);
}

function initTheme() {
  applyTheme(storedTheme() || (systemTheme.matches ? "light" : "dark"));

  $("#btn-theme").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch (error) {
      /* not fatal — the choice just lasts for this page */
    }
    applyTheme(next);
  });

  // Follow the OS, but only until the user picks a side of their own.
  systemTheme.addEventListener("change", (event) => {
    if (!storedTheme()) applyTheme(event.matches ? "light" : "dark");
  });
}

/** Read a JSON API response, surfacing FastAPI's `detail` as the error text. */
async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (!response.ok) {
    throw new Error(payload?.detail || `${response.status} ${response.statusText}`);
  }
  return payload;
}

/* -- tooltips ----------------------------------------------------------- */

/* Native `title` tooltips are slow to appear, unstyled, and truncate long
 * error text. Anything with `data-tip` gets this one instead: same delegation
 * for static markup and for rows rebuilt on every progress event.
 */

const TOOLTIP_DELAY_MS = 160;
const TOOLTIP_MARGIN = 8;
const TOOLTIP_GAP = 8;

let tooltipElement = null;
let tooltipTimer = null;

function tooltipNode() {
  if (!tooltipElement) {
    tooltipElement = document.createElement("div");
    tooltipElement.className = "tooltip";
    tooltipElement.setAttribute("role", "tooltip");
    document.body.append(tooltipElement);
  }
  return tooltipElement;
}

function placeTooltip(target) {
  const tip = tooltipNode();
  const anchor = target.getBoundingClientRect();
  const bubble = tip.getBoundingClientRect();

  // Prefer above; flip below when there is not enough headroom.
  const below = anchor.top - bubble.height - TOOLTIP_GAP < TOOLTIP_MARGIN;
  tip.classList.toggle("is-below", below);
  tip.style.top = `${below ? anchor.bottom + TOOLTIP_GAP : anchor.top - bubble.height - TOOLTIP_GAP}px`;

  const centred = anchor.left + anchor.width / 2 - bubble.width / 2;
  const maxLeft = window.innerWidth - bubble.width - TOOLTIP_MARGIN;
  const left = Math.max(TOOLTIP_MARGIN, Math.min(centred, maxLeft));
  tip.style.left = `${left}px`;

  // Keep the arrow under the target even after clamping. Re-measure first:
  // wrapping can settle a few subpixels narrower once the bubble is in place,
  // and clamping against the stale width lets the arrow drift off the edge.
  const settled = tip.getBoundingClientRect();
  const arrowX = anchor.left + anchor.width / 2 - settled.left;
  const inset = Math.min(10, settled.width / 2);
  tip.style.setProperty(
    "--arrow-x",
    `${Math.max(inset, Math.min(arrowX, settled.width - inset))}px`,
  );
}

function showTooltip(target) {
  const text = target.dataset.tip;
  if (!text) return;

  const tip = tooltipNode();
  tip.textContent = text;
  tip.style.left = "0px";
  tip.style.top = "0px";
  tip.classList.add("is-visible");
  placeTooltip(target);
}

function hideTooltip() {
  clearTimeout(tooltipTimer);
  if (tooltipElement) tooltipElement.classList.remove("is-visible");
}

function armTooltip(target) {
  clearTimeout(tooltipTimer);
  tooltipTimer = setTimeout(() => showTooltip(target), TOOLTIP_DELAY_MS);
}

document.addEventListener("mouseover", (event) => {
  const target = event.target.closest?.("[data-tip]");
  if (target) armTooltip(target);
});

document.addEventListener("mouseout", (event) => {
  if (event.target.closest?.("[data-tip]")) hideTooltip();
});

// Keyboard users reach the same tooltips by tabbing to the control.
document.addEventListener("focusin", (event) => {
  const target = event.target.closest?.("[data-tip]");
  if (target) showTooltip(target);
});

document.addEventListener("focusout", hideTooltip);
document.addEventListener("click", hideTooltip);
window.addEventListener("scroll", hideTooltip, true);

/* -- toasts ------------------------------------------------------------- */

const TOAST_MILLISECONDS = 3600;

function toast(message, kind = "ok") {
  const item = document.createElement("div");
  item.className = `toast toast-${kind}`;
  item.textContent = message;
  $("#toasts").append(item);

  setTimeout(() => {
    item.classList.add("is-leaving");
    item.addEventListener("animationend", () => item.remove());
    // Fallback for reduced-motion, where the animation never fires.
    setTimeout(() => item.remove(), 400);
  }, TOAST_MILLISECONDS);
}

/* -- application state -------------------------------------------------- */

const DEVICE_KEY = "sshsync-device";
const ALL_DEVICES = "all";

const STATUS_KEY = "sshsync-status";
const ALL_STATUS = "all";

function storedDevice() {
  try {
    return localStorage.getItem(DEVICE_KEY) || ALL_DEVICES;
  } catch (error) {
    return ALL_DEVICES;  /* not fatal — the filter just resets each visit */
  }
}

function storedStatus() {
  try {
    return localStorage.getItem(STATUS_KEY) || ALL_STATUS;
  } catch (error) {
    return ALL_STATUS;  /* not fatal — the filter just resets each visit */
  }
}

const state = {
  config: null,        // The saved config, as loaded from the server.
  jobs: [],            // Per-job summaries derived from the saved config.
  servers: [],         // Server entries from the saved config.
  serverStatus: {},    // name -> {online, reason}, from the SSH probe.
  serverCheckedAt: {}, // name -> ISO time the probe last returned for it.
  jobHistory: {},      // "name|type|server" -> aggregate from the history.
  live: {},            // selector -> live job state during a run.
  loaded: false,       // False until the config and its history have arrived.
  running: false,
  runElapsed: "",
  device: storedDevice(),  // Device filter: "all", "local" or a server name.
  status: storedStatus(),  // Status filter: see STATUS_FILTERS below.
};

function liveStateFor(job) {
  return state.live[job.selector] || null;
}

/* A finished job's progress bar has nothing left to report, so it lingers just
 * long enough to read and then gives the card back to the "last run" summary —
 * which by then says "just now". Statuses below are the terminal ones.
 */
const TERMINAL_STATUSES = new Set(["OK", "FAIL", "SKIPPED", "CANCELLED"]);
const RESULT_LINGER_MS = 3000;

const resultTimers = {};

function clearResultTimers() {
  Object.values(resultTimers).forEach(clearTimeout);
  Object.keys(resultTimers).forEach((key) => delete resultTimers[key]);
}

function retireResult(selector) {
  clearTimeout(resultTimers[selector]);
  resultTimers[selector] = setTimeout(async () => {
    delete resultTimers[selector];
    // Refresh the history first, so the card swaps straight to an accurate
    // "just now" instead of flashing the previous run's timestamp.
    await loadHistory();
    delete state.live[selector];
    renderJobs();
  }, RESULT_LINGER_MS);
}

function historyFor(job) {
  return state.jobHistory[`${job.name}|${job.type}|${job.server}`] || null;
}

/* -- tabs --------------------------------------------------------------- */

$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((other) => other.classList.toggle("is-active", other === tab));
    $$(".view").forEach((view) => {
      view.classList.toggle("is-active", view.id === `view-${tab.dataset.view}`);
    });
    if (tab.dataset.view === "history") loadHistory();
    if (tab.dataset.view === "logs") reloadLogs();
  });
});

/* -- loading placeholders ------------------------------------------------
 *
 * The cards need two requests before they can say anything useful — the config
 * for the jobs themselves, the history for "last run" — so the panels would
 * otherwise sit empty and then jump. Placeholders of the same shape hold the
 * space until both have landed.
 */

const SERVER_CARD_CLASS =
  "flex items-start justify-between gap-3 rounded-xl border border-edge " +
  "bg-raised px-4 py-3.5";
const JOB_CARD_CLASS =
  "flex items-stretch gap-3.5 rounded-xl border bg-raised p-4";

// Enough to fill the width on a typical screen without guessing at the config.
const SKELETON_SERVERS = 2;
const SKELETON_JOBS = 6;

function skeletonBar(classes) {
  const bar = document.createElement("div");
  bar.className = `skeleton ${classes}`;
  return bar;
}

function serverSkeleton() {
  const card = document.createElement("div");
  // The min-heights match a filled card, so nothing shifts when the real one
  // takes its place.
  card.className = `${SERVER_CARD_CLASS} min-h-[76px]`;

  const body = document.createElement("div");
  body.className = "flex min-w-0 flex-col gap-2";
  body.append(skeletonBar("h-4 w-28"), skeletonBar("h-3 w-44"), skeletonBar("h-3 w-20"));

  const controls = document.createElement("div");
  controls.className = "flex shrink-0 gap-1";
  controls.append(skeletonBar("size-8 rounded-full"), skeletonBar("size-8 rounded-full"));

  card.append(body, controls);
  return card;
}

function jobSkeleton() {
  const card = document.createElement("div");
  card.className = `${JOB_CARD_CLASS} border-edge min-h-[104px]`;

  const body = document.createElement("div");
  body.className = "flex min-w-0 flex-1 flex-col gap-2.5";
  body.append(
    skeletonBar("h-4 w-36"),
    skeletonBar("h-3 w-full"),
    skeletonBar("h-3 w-4/5"),
    skeletonBar("mt-auto h-3 w-40"),
  );

  const controls = document.createElement("div");
  controls.className = "flex flex-col items-center justify-between gap-2.5";
  controls.append(skeletonBar("size-11 rounded-full"), skeletonBar("size-8 rounded-full"));

  card.append(body, controls);
  return card;
}

/** Fill a card container with placeholders, and mark it busy for readers. */
function renderSkeletons(container, count, build) {
  container.textContent = "";
  container.setAttribute("aria-busy", "true");
  for (let index = 0; index < count; index += 1) container.append(build());
}

/* -- server cards ------------------------------------------------------- */

function renderServers() {
  const container = $("#server-cards");

  if (!state.loaded) {
    renderSkeletons(container, SKELETON_SERVERS, serverSkeleton);
    return;
  }

  container.textContent = "";
  container.removeAttribute("aria-busy");

  if (!state.servers.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No servers configured. Rclone jobs need one.";
    container.append(empty);
    return;
  }

  state.servers.forEach((server, index) => {
    const card = document.createElement("div");
    card.dataset.card = "server";
    card.className = SERVER_CARD_CLASS;

    const body = document.createElement("div");
    body.className = "min-w-0";

    const name = document.createElement("div");
    name.dataset.role = "server-name";
    name.className = "flex items-center gap-2 text-[15px] font-semibold";
    name.append(server.name || "(unnamed)");

    const status = state.serverStatus[server.name];
    const badge = document.createElement("span");
    if (status === "checking") {
      badge.className = "badge badge-busy";
      badge.textContent = "Checking";
    } else if (status === undefined) {
      badge.className = "badge badge-unknown";
      badge.textContent = "Unknown";
    } else {
      badge.className = `badge ${status.online ? "badge-ok" : "badge-fail"}`;
      badge.textContent = status.online ? "Online" : "Offline";
      if (status.reason) badge.dataset.tip = status.reason;
    }
    name.append(badge);

    const meta = document.createElement("div");
    meta.className = "mt-1 font-mono text-xs break-all text-dim";
    meta.textContent = `${server.user}@${server.host}:${server.port ?? 22}`;

    const jobCount = state.jobs.filter(
      (job) => job.type === "rclone" && job.server === server.name,
    ).length;
    const jobs = document.createElement("div");
    jobs.className = "mt-1.5 text-xs text-dim";
    jobs.append(`${jobCount} rclone job${jobCount === 1 ? "" : "s"}`);

    // When the probe last spoke for this server — self-ages like the job cards.
    const checkedAt = state.serverCheckedAt[server.name];
    if (status === "checking") {
      jobs.append(" · checking…");
    } else if (checkedAt) {
      jobs.append(" · checked ", relativeTime(checkedAt));
    }

    body.append(name, meta, jobs);

    const controls = document.createElement("div");
    controls.className = "flex shrink-0 gap-1";

    const recheck = document.createElement("button");
    recheck.className = "icon-button icon-button-edit";
    recheck.dataset.tip = `Re-check ${server.name}`;
    recheck.setAttribute("aria-label", `Re-check server ${server.name}`);
    recheck.disabled = status === "checking";
    recheck.append(icon("refresh"));
    recheck.addEventListener("click", () => checkServers([server.name]));

    const edit = document.createElement("button");
    edit.className = "icon-button icon-button-edit";
    edit.dataset.tip = `Edit ${server.name}`;
    edit.setAttribute("aria-label", `Edit server ${server.name}`);
    edit.append(icon("pencil"));
    edit.addEventListener("click", () => openServerModal(index));

    controls.append(recheck, edit);
    card.append(body, controls);
    container.append(card);
  });
}

/* -- device filter ------------------------------------------------------ */

/* Jobs are grouped by the machine they write to: "local" for robocopy mirrors,
 * the server name for rclone uploads. With a dozen jobs across three machines,
 * looking at one machine's worth is the common case.
 */

/** The device a job belongs to: its server, or "local" for robocopy jobs. */
function deviceOf(job) {
  return job.type === "rclone" ? job.server : "local";
}

/** Display label for a device: server names as-is, "local" shown as "Local". */
function deviceLabel(device) {
  return device === "local" ? "Local" : device;
}

/** Whether a device is reachable right now.
 *
 * "local" (robocopy) is always available. A server counts as reachable unless
 * its SSH probe has come back saying otherwise — a status that is still
 * "checking" or not yet known does not block, so nothing is gated on a probe
 * that hasn't returned.
 */
function deviceOnline(device) {
  if (device === "local") return true;
  const status = state.serverStatus[device];
  if (!status || status === "checking") return true;
  return status.online !== false;
}

/** A job whose target device the SSH probe has reported as offline. */
function jobOffline(job) {
  return !deviceOnline(deviceOf(job));
}

/* Status filter: each key is a predicate over a job. "enabled"/"disabled" read
 * the config flag; "online"/"offline" read the device's SSH probe. They are two
 * different axes, so a job can be, say, both enabled and offline.
 */
const STATUS_FILTERS = {
  all: () => true,
  enabled: (job) => job.enabled,
  disabled: (job) => !job.enabled,
  offline: (job) => jobOffline(job),
  online: (job) => !jobOffline(job),
};
const STATUS_ORDER = ["all", "enabled", "disabled", "offline", "online"];
const STATUS_LABELS = {
  all: "All",
  enabled: "Enabled",
  disabled: "Disabled",
  offline: "Offline",
  online: "Online",
};

/** Re-render everything the filters affect. */
function renderFiltered() {
  renderDeviceFilter();
  renderStatusFilter();
  renderJobs();
  renderRunAllButton();
}

function setDevice(device) {
  state.device = device;
  try {
    localStorage.setItem(DEVICE_KEY, device);
  } catch (error) {
    /* not fatal — the choice just lasts for this page */
  }
  renderFiltered();
}

function setStatus(status) {
  state.status = status;
  try {
    localStorage.setItem(STATUS_KEY, status);
  } catch (error) {
    /* not fatal — the choice just lasts for this page */
  }
  renderFiltered();
}

/** Whether a job passes the active device filter. */
function matchesDevice(job) {
  return state.device === ALL_DEVICES || deviceOf(job) === state.device;
}

/** Whether a job passes the active status filter. */
function matchesStatus(job) {
  return (STATUS_FILTERS[state.status] || STATUS_FILTERS.all)(job);
}

/** Jobs matching both active filters, in config order. */
function visibleJobs() {
  return state.jobs.filter((job) => matchesDevice(job) && matchesStatus(job));
}

/** Why the job list is empty, phrased around whichever filters are active. */
function emptyFilterMessage() {
  const status = state.status !== ALL_STATUS ? state.status : "";
  const device =
    state.device !== ALL_DEVICES ? ` on ${deviceLabel(state.device)}` : "";
  if (status || device) return `No ${status ? `${status} ` : ""}jobs${device}.`;
  return "No jobs match the current filters.";
}

/** Devices that have at least one job, local first and servers in config order. */
function knownDevices() {
  const order = ["local", ...state.servers.map((server) => server.name)];
  const used = new Set(state.jobs.map(deviceOf));
  return order.filter((device) => used.has(device));
}

function renderDeviceFilter() {
  const container = $("#job-filters");
  container.textContent = "";

  const devices = knownDevices();
  // A single device makes the filter a row of one — nothing to choose between.
  if (devices.length < 2) {
    state.device = ALL_DEVICES;
    return;
  }

  // A device can disappear when its server is deleted; do not filter to nothing.
  if (state.device !== ALL_DEVICES && !devices.includes(state.device)) {
    state.device = ALL_DEVICES;
  }

  [ALL_DEVICES, ...devices].forEach((device) => {
    // Counts reflect the active status filter, so each number matches what
    // choosing this device would actually show.
    const count = state.jobs.filter(
      (job) =>
        (device === ALL_DEVICES || deviceOf(job) === device) && matchesStatus(job),
    ).length;

    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `chip${device === state.device ? " is-active" : ""}`;
    chip.setAttribute("aria-pressed", String(device === state.device));
    chip.dataset.tip =
      device === ALL_DEVICES
        ? "Show every device"
        : `Show only jobs on ${deviceLabel(device)}`;
    chip.textContent = device === ALL_DEVICES ? "All" : deviceLabel(device);

    const badge = document.createElement("span");
    badge.className = "chip-count";
    badge.textContent = count;
    chip.append(badge);

    chip.addEventListener("click", () => setDevice(device));
    container.append(chip);
  });
}

/* -- status filter ------------------------------------------------------ */

function renderStatusFilter() {
  const container = $("#status-filters");
  container.textContent = "";

  // Nothing to filter until there are jobs to filter.
  if (!state.jobs.length) {
    state.status = ALL_STATUS;
    return;
  }

  // A stale localStorage value must not leave the list filtered to nothing.
  if (!STATUS_FILTERS[state.status]) state.status = ALL_STATUS;

  STATUS_ORDER.forEach((status) => {
    // Counts reflect the active device filter, so each number matches what
    // choosing this status would actually show.
    const count = state.jobs.filter(
      (job) => matchesDevice(job) && STATUS_FILTERS[status](job),
    ).length;

    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `chip${status === state.status ? " is-active" : ""}`;
    chip.setAttribute("aria-pressed", String(status === state.status));
    chip.dataset.tip =
      status === ALL_STATUS
        ? "Show every status"
        : `Show only ${status} jobs`;
    chip.textContent = STATUS_LABELS[status];

    const badge = document.createElement("span");
    badge.className = "chip-count";
    badge.textContent = count;
    chip.append(badge);

    chip.addEventListener("click", () => setStatus(status));
    container.append(chip);
  });
}

/** Label and targets for the run-all button, narrowed by the active filter.
 *
 * Jobs whose device is offline are left out — they cannot run, so run-all skips
 * them rather than failing partway through.
 */
function runAllTarget() {
  const jobs = visibleJobs().filter((job) => job.enabled && !jobOffline(job));
  if (state.device === ALL_DEVICES) {
    // A null selector tells the server "every enabled job"; only spell the list
    // out explicitly when some enabled job must be skipped for being offline.
    const anyOffline = state.jobs.some((job) => job.enabled && jobOffline(job));
    return {
      label: "all reachable jobs",
      selectors: anyOffline ? jobs.map((job) => job.selector) : null,
      text: "Run all enabled",
    };
  }
  return {
    label: `${jobs.length} job(s) on ${deviceLabel(state.device)}`,
    selectors: jobs.map((job) => job.selector),
    text: `Run all on ${deviceLabel(state.device)}`,
  };
}

function renderRunAllButton() {
  const button = $("#btn-run-all");
  const target = runAllTarget();
  button.lastChild.textContent = target.text;
  button.disabled = state.running || target.selectors?.length === 0;
}

/* -- job cards ---------------------------------------------------------- */

const STATUS_CLASSES = {
  OK: "status-ok",
  FAIL: "status-fail",
  RUNNING: "status-running",
  PENDING: "status-pending",
  SKIPPED: "status-skipped",
  CANCELLED: "status-cancelled",
};

function jobCardFooter(job) {
  const footer = document.createElement("div");
  footer.dataset.role = "footer";
  footer.className =
    "mt-auto flex flex-wrap items-center gap-2.5 pt-1 text-xs text-dim";
  const live = liveStateFor(job);

  if (live && live.status !== "PENDING") {
    const stats = live.stats;
    const progress = document.createElement("div");
    progress.className = "min-w-30 flex-1 basis-36";

    const label = document.createElement("div");
    label.textContent =
      live.status === "RUNNING"
        ? `${stats.checks_done}/${stats.checks_total} checked · ${countAndSize(stats.uploaded_files, stats.uploaded_bytes)} up · ${stats.elapsed}`
        : `${countAndSize(stats.uploaded_files, stats.uploaded_bytes)} up · ${countAndSize(stats.deleted_files, stats.deleted_bytes)} deleted · ${stats.elapsed}`;
    progress.append(label);

    if (stats.checks_total > 0) {
      const bar = document.createElement("div");
      bar.className = `bar${live.status === "OK" ? " bar-ok" : ""}${live.status === "FAIL" ? " bar-fail" : ""}`;
      const fill = document.createElement("span");
      fill.style.width = `${Math.min(100, (stats.checks_done / stats.checks_total) * 100)}%`;
      bar.append(fill);
      progress.append(bar);
    }

    footer.append(progress);

    if (stats.last_error) {
      const error = document.createElement("span");
      error.className = "status-fail";
      error.textContent = stats.last_error.slice(0, 60);
      error.dataset.tip = stats.last_error;
      footer.append(error);
    }
    return footer;
  }

  const history = historyFor(job);
  if (!history) {
    const never = document.createElement("span");
    never.textContent = "Never run";
    footer.append(never);
    return footer;
  }

  const failures = history.failures
    ? ` · ${history.failures} failure${history.failures === 1 ? "" : "s"}`
    : "";
  const summary = document.createElement("span");
  summary.append(
    "Last run ",
    relativeTime(history.last_ended_at),
    ` · ${history.runs} run${history.runs === 1 ? "" : "s"}${failures}`,
  );

  footer.append(summary);
  return footer;
}

function renderJobs() {
  const container = $("#job-cards");

  if (!state.loaded) {
    renderSkeletons(container, SKELETON_JOBS, jobSkeleton);
    return;
  }

  container.textContent = "";
  container.removeAttribute("aria-busy");

  // Disabled jobs sink to the bottom; config order is kept within each group.
  const ordered = [...visibleJobs()].sort(
    (first, second) => Number(second.enabled) - Number(first.enabled),
  );

  if (!ordered.length) {
    const empty = document.createElement("p");
    empty.className = "py-6 text-center text-sm text-dim";
    empty.textContent = state.jobs.length
      ? emptyFilterMessage()
      : "No jobs configured yet — use Add job to create one.";
    container.append(empty);
    return;
  }

  ordered.forEach((job) => {
    const live = liveStateFor(job);
    const isRunning = live?.status === "RUNNING";
    const offline = jobOffline(job);

    const card = document.createElement("div");
    card.dataset.card = "job";
    card.className = [
      JOB_CARD_CLASS,
      "transition-colors",
      // The border carries the job's state: amber mid-run or when the target
      // device is offline, red on failure.
      isRunning
        ? "border-warn/50"
        : live?.status === "FAIL"
          ? "border-fail/45"
          : offline
            ? "border-warn/40"
            : "border-edge hover:border-edge-strong",
      // Offline jobs are dimmed like disabled ones — they cannot run right now.
      job.enabled && !offline ? "" : "opacity-55",
    ].join(" ");

    /* Body ------------------------------------------------------------- */
    const body = document.createElement("div");
    body.className = "flex min-w-0 flex-1 flex-col gap-2";

    const head = document.createElement("div");
    head.dataset.role = "head";
    head.className = "flex flex-wrap items-center gap-2";

    const name = document.createElement("span");
    name.className = "text-base font-semibold";
    name.textContent = job.name;

    const tag = document.createElement("span");
    tag.className = `tag tag-${job.type}`;
    tag.textContent = job.type === "rclone" ? job.server : "local";
    tag.dataset.tip = job.type;

    head.append(name, tag);

    if (!job.enabled) {
      const disabled = document.createElement("span");
      disabled.className = "tag tag-disabled";
      disabled.textContent = "Disabled";
      head.append(disabled);
    }

    if (offline) {
      const off = document.createElement("span");
      off.className = "tag tag-offline";
      off.textContent = "Offline";
      off.dataset.tip = `${deviceOf(job)} is offline`;
      head.append(off);
    }

    if (live && live.status !== "PENDING") {
      const status = document.createElement("span");
      status.className = `tag tag-${live.status.toLowerCase()}`;
      status.textContent = live.status;
      head.append(status);
    }

    const paths = document.createElement("dl");
    paths.className =
      "grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 font-mono text-xs text-dim";
    [
      ["From", job.source],
      ["To", job.destination],
    ].forEach(([label, value]) => {
      const term = document.createElement("dt");
      term.className = "pt-0.5 text-[10px] uppercase tracking-wider text-edge-strong";
      term.textContent = label;
      const detail = document.createElement("dd");
      detail.className = "m-0 break-all";
      detail.textContent = value;
      paths.append(term, detail);
    });

    body.append(head, paths, jobCardFooter(job));

    /* Controls ---------------------------------------------------------- */
    const controls = document.createElement("div");
    controls.className =
      "flex flex-col items-center justify-between gap-2.5";

    const run = document.createElement("button");
    run.className = `icon-button icon-button-run${isRunning ? " is-running" : ""}`;
    run.append(icon(isRunning ? "pause" : "play"));
    if (isRunning) {
      run.dataset.tip = "Stop this run";
      run.setAttribute("aria-label", `Stop ${job.name}`);
      run.addEventListener("click", cancelRun);
    } else {
      // A job cannot run while its device is offline, and only one run happens
      // at a time — so every other job's play button waits.
      run.disabled = state.running || offline;
      run.dataset.tip = offline
        ? `${deviceOf(job)} is offline`
        : state.running
          ? "Another run is in progress"
          : `Run ${job.name}`;
      run.setAttribute("aria-label", `Run ${job.name}`);
      if (!offline) {
        run.addEventListener("click", () => startRun([job.selector], job.name));
      }
    }

    const edit = document.createElement("button");
    edit.className = "icon-button icon-button-edit";
    edit.dataset.tip = `Edit ${job.name}`;
    edit.setAttribute("aria-label", `Edit job ${job.name}`);
    edit.append(icon("pencil"));
    edit.addEventListener("click", () => openJobModal(job.index));

    controls.append(run, edit);
    card.append(body, controls);
    container.append(card);
  });
}

/* -- progress table ----------------------------------------------------- */

function renderProgress() {
  const body = $("#progress-body");
  const foot = $("#progress-foot");
  const jobs = Object.values(state.live).sort((a, b) => a.index - b.index);
  body.textContent = "";

  if (!jobs.length) {
    const row = body.insertRow();
    row.className = "empty";
    const cell = row.insertCell();
    cell.colSpan = 8;
    cell.textContent = "No run yet — press play on a job to start one.";
    foot.hidden = true;
    return;
  }

  const totals = {
    uploadedFiles: 0, uploadedBytes: 0,
    deletedFiles: 0, deletedBytes: 0,
    checksDone: 0, checksTotal: 0, sourceBytes: 0,
  };

  jobs.forEach((job) => {
    const stats = job.stats;
    totals.uploadedFiles += stats.uploaded_files;
    totals.uploadedBytes += stats.uploaded_bytes;
    totals.deletedFiles += stats.deleted_files;
    totals.deletedBytes += stats.deleted_bytes;
    totals.checksDone += stats.checks_done;
    totals.checksTotal += stats.checks_total;
    totals.sourceBytes += job.source_bytes || 0;

    const row = body.insertRow();
    row.insertCell().textContent = job.name;
    row.insertCell().textContent = job.server;

    const status = row.insertCell();
    status.className = STATUS_CLASSES[job.status] || "";
    status.textContent = job.status;

    const uploaded = row.insertCell();
    uploaded.className = "num";
    uploaded.textContent = countAndSize(stats.uploaded_files, stats.uploaded_bytes);

    const deleted = row.insertCell();
    deleted.className = "num";
    deleted.textContent = countAndSize(stats.deleted_files, stats.deleted_bytes);

    const files = row.insertCell();
    files.className = "num";
    const size = job.source_bytes === null ? "…" : formatBytes(job.source_bytes);
    files.textContent = `${stats.checks_done}/${stats.checks_total} (${size})`;

    const time = row.insertCell();
    time.className = "num";
    time.textContent = stats.elapsed;

    const errors = row.insertCell();
    errors.className = "wrap";
    errors.textContent = stats.last_error
      ? `${stats.error_count} — ${stats.last_error}`
      : String(stats.error_count);
    if (stats.last_error) errors.dataset.tip = stats.last_error;
  });

  foot.hidden = false;
  $("[data-total=uploaded]", foot).textContent = countAndSize(totals.uploadedFiles, totals.uploadedBytes);
  $("[data-total=deleted]", foot).textContent = countAndSize(totals.deletedFiles, totals.deletedBytes);
  $("[data-total=files]", foot).textContent =
    `${totals.checksDone}/${totals.checksTotal} (${formatBytes(totals.sourceBytes)})`;
  $("[data-total=elapsed]", foot).textContent = state.runElapsed;
}

/* -- event feed --------------------------------------------------------- */

function pushFeed(message, level = "info", at = null) {
  const feed = $("#event-feed");
  const item = document.createElement("li");
  item.className = "flex gap-2.5 border-b border-edge px-2 py-[5px] last:border-b-0";

  const time = document.createElement("span");
  time.className = "shrink-0 text-dim";
  time.textContent = clockTime(at || new Date().toISOString());

  const levelColours = {
    error: "text-fail",
    warning: "text-warn",
    success: "text-ok",
    info: "",
  };
  const text = document.createElement("span");
  text.className = levelColours[level] ?? "";
  text.textContent = message;

  item.append(time, text);
  feed.append(item);
  while (feed.children.length > 200) feed.firstChild.remove();
  feed.scrollTop = feed.scrollHeight;
}

$("#btn-clear-feed").addEventListener("click", () => {
  $("#event-feed").textContent = "";
});

/* -- run control -------------------------------------------------------- */

/** Toggle the run controls. The indicator keeps any outcome set afterwards. */
function setRunning(running) {
  state.running = running;
  renderRunAllButton();
  $("#btn-cancel").disabled = !running;

  const indicator = $("#run-indicator");
  indicator.className = `pill ${running ? "pill-running" : "pill-idle"}`;
  indicator.textContent = running ? "Running" : "Idle";
  delete indicator.dataset.tip;
}

function setRunOutcome(ok, summary) {
  const indicator = $("#run-indicator");
  indicator.className = `pill ${ok ? "pill-ok" : "pill-fail"}`;
  indicator.textContent = ok ? "Completed" : "Failed";
  indicator.dataset.tip = summary || "";
}

async function startRun(selectors, label) {
  try {
    setRunning(true);
    renderJobs();
    await api("/api/run", {
      method: "POST",
      body: JSON.stringify({
        selectors: selectors?.length ? selectors : null,
        dry_run: $("#dry-run").checked,
      }),
    });
    const dry = $("#dry-run").checked ? " (dry run)" : "";
    toast(`Started ${label}${dry}.`);
  } catch (error) {
    setRunning(false);
    renderJobs();
    toast(`Could not start run: ${error.message}`, "error");
    pushFeed(`Could not start run: ${error.message}`, "error");
  }
}

async function cancelRun() {
  try {
    await api("/api/cancel", { method: "POST" });
    toast("Stopping run…");
    pushFeed("Cancellation requested.", "warning");
  } catch (error) {
    toast(`Could not stop: ${error.message}`, "error");
  }
}

$("#btn-run-all").addEventListener("click", () => {
  const target = runAllTarget();
  startRun(target.selectors, target.label);
});
$("#btn-cancel").addEventListener("click", cancelRun);

/* -- server checks ------------------------------------------------------ */

/** Whether a probe is in flight, so the auto-recheck timer can stand down. */
let checkInFlight = false;

/** Probe servers over SSH. Pass names to re-check just those.
 *
 * `silent` suppresses the toasts, for the background auto-recheck that would
 * otherwise announce itself every interval.
 */
async function checkServers(names = null, { silent = false } = {}) {
  const targets = names || state.servers.map((server) => server.name);
  checkInFlight = true;
  // A background refresh keeps the last known badges up while it re-probes;
  // only a user-driven check shows the "Checking" placeholder.
  if (!silent) {
    targets.forEach((name) => {
      state.serverStatus[name] = "checking";
    });
    renderServers();
    renderFiltered();
  }

  try {
    const { servers } = await api("/api/servers/check", {
      method: "POST",
      body: JSON.stringify({ names }),
    });
    const now = new Date().toISOString();
    servers.forEach((server) => {
      state.serverStatus[server.name] = {
        online: server.online,
        reason: server.reason,
      };
      state.serverCheckedAt[server.name] = now;
    });

    const offline = servers.filter((server) => !server.online);
    if (silent) {
      /* background refresh — the cards speak for themselves */
    } else if (servers.length === 1) {
      const [server] = servers;
      toast(
        server.online ? `${server.name} is online.` : `${server.name} is offline.`,
        server.online ? "ok" : "error",
      );
    } else {
      toast(
        offline.length ? `${offline.length} server(s) offline.` : "All servers online.",
        offline.length ? "error" : "ok",
      );
    }
  } catch (error) {
    // A user-driven check drops its spinner rather than leaving stale
    // "Checking" badges; a silent refresh keeps the last known status instead
    // of blanking a card to "Unknown" over one transient failure.
    if (!silent) {
      targets.forEach((name) => delete state.serverStatus[name]);
      toast(`Server check failed: ${error.message}`, "error");
    }
  } finally {
    checkInFlight = false;
  }
  renderServers();
  renderFiltered();
}

$("#btn-check-servers").addEventListener("click", () => checkServers());

/* Keep server status fresh on its own, so offline devices (and the run buttons
 * they gate) don't go stale between manual checks. Stand down while a run is on,
 * while another check is in flight, and while the tab is hidden — no point
 * probing a page nobody is looking at.
 */
const AUTO_RECHECK_MS = 60000;

setInterval(() => {
  if (!state.loaded || state.running || checkInFlight) return;
  if (document.hidden || !state.servers.length) return;
  checkServers(null, { silent: true });
}, AUTO_RECHECK_MS);

/* -- live feed ---------------------------------------------------------- */

function replaceLiveJobs(jobs) {
  clearResultTimers();
  state.live = {};
  (jobs || []).forEach((job) => {
    state.live[job.selector] = job;
    if (TERMINAL_STATUSES.has(job.status)) retireResult(job.selector);
  });
}

function applyEvent(event) {
  switch (event.type) {
    case "snapshot":
      replaceLiveJobs(event.state.jobs);
      state.runElapsed = event.state.elapsed || "";
      setRunning(Boolean(event.state.running));
      if (event.state.result) {
        setRunOutcome(event.state.result.ok, event.state.result.summary);
      }
      renderProgress();
      renderJobs();
      break;

    case "run_started":
      replaceLiveJobs(event.jobs);
      setRunning(true);
      pushFeed(
        `Run started — ${event.jobs.length} job(s)${event.dry_run ? " (dry run)" : ""}.`,
        "info", event.at,
      );
      renderProgress();
      renderJobs();
      break;

    case "checking_servers":
      pushFeed(`Checking servers: ${event.servers.join(", ")}`, "info", event.at);
      break;

    case "job_updated": {
      const previous = state.live[event.job.selector];
      state.live[event.job.selector] = event.job;
      if (TERMINAL_STATUSES.has(event.job.status)) retireResult(event.job.selector);
      renderProgress();
      // Status changes alter the card's controls and styling; byte counters
      // only alter its footer text, so a full re-render is wasteful there.
      if (previous?.status !== event.job.status) renderJobs();
      else updateJobCardFooter(event.job);
      break;
    }

    case "log":
      pushFeed(event.message, event.level === "error" ? "error" : "warning", event.at);
      break;

    case "cancelled":
      pushFeed("Run cancelled.", "warning", event.at);
      break;

    case "run_finished":
      replaceLiveJobs(event.result.jobs);
      state.runElapsed = event.elapsed || "";
      setRunning(false);
      setRunOutcome(event.result.ok, event.result.summary);
      renderProgress();
      renderJobs();
      pushFeed(event.result.summary, event.result.ok ? "success" : "error", event.at);
      toast(event.result.summary, event.result.ok ? "ok" : "error");
      event.result.log_paths.forEach((path) =>
        pushFeed(`Failure details written to ${path}`, "warning", event.at),
      );
      loadHistory();
      break;

    case "run_failed":
      setRunning(false);
      setRunOutcome(false, event.error);
      renderJobs();
      pushFeed(event.error, "error", event.at);
      toast(event.error, "error");
      break;
  }
}

/** Repaint one card's footer in place, avoiding a full grid re-render. */
function updateJobCardFooter(liveJob) {
  const job = state.jobs.find((item) => item.selector === liveJob.selector);
  if (!job) return;
  const cards = $$('#job-cards [data-card="job"]');
  const ordered = [...state.jobs].sort(
    (first, second) => Number(second.enabled) - Number(first.enabled),
  );
  const position = ordered.findIndex((item) => item.selector === liveJob.selector);
  const card = cards[position];
  if (!card) return;
  card.querySelector('[data-role="footer"]').replaceWith(jobCardFooter(job));
}

function connectLiveFeed() {
  const indicator = $("#socket-indicator");
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/ws`);

  socket.addEventListener("open", () => {
    indicator.className = "pill pill-ok";
    indicator.textContent = "Live";
  });

  socket.addEventListener("message", (message) => {
    const event = JSON.parse(message.data);
    if (event.type === "snapshot") {
      (event.events || []).slice(-30).forEach((past) => {
        if (past.type === "log") pushFeed(past.message, past.level, past.at);
      });
    }
    applyEvent(event);
  });

  socket.addEventListener("close", () => {
    indicator.className = "pill pill-fail";
    indicator.textContent = "Reconnecting…";
    // The server restarts often during development; keep the page usable.
    setTimeout(connectLiveFeed, 2000);
  });
}

/* Tick the run clock locally so elapsed time advances between events. */
setInterval(async () => {
  if (!state.running) return;
  try {
    const status = await api("/api/status");
    state.runElapsed = status.elapsed || "";
    $("#run-elapsed").textContent = state.runElapsed;
  } catch {
    /* Transient failure; the next tick retries. */
  }
}, 1000);

/* -- modals ------------------------------------------------------------- */

let openDialog = null;

function openModal(element) {
  openDialog = element;
  element.hidden = false;
  const firstField = element.querySelector("input, select, textarea");
  if (firstField) firstField.focus();
}

function closeModal() {
  if (openDialog) openDialog.hidden = true;
  openDialog = null;
}

$$("[data-close]").forEach((element) => element.addEventListener("click", closeModal));

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeModal();
});

/** Persist a whole config, then refresh everything derived from it. */
async function persistConfig(config, successMessage, errorElement) {
  try {
    applyConfig(await api("/api/config", { method: "PUT", body: JSON.stringify(config) }));
    closeModal();
    toast(successMessage);
    return true;
  } catch (error) {
    if (errorElement) {
      errorElement.hidden = false;
      errorElement.textContent = error.message;
    }
    toast(`Could not save: ${error.message}`, "error");
    return false;
  }
}

/* -- job modal ---------------------------------------------------------- */

let editingJobIndex = null;

function syncJobModalType() {
  const type = $("#job-type").value;
  $$("#job-modal [data-only]").forEach((element) => {
    element.hidden = element.dataset.only !== type;
  });
}

$("#job-type").addEventListener("change", syncJobModalType);

function openJobModal(index) {
  editingJobIndex = index;
  const isNew = index === null;
  const job = isNew
    ? { name: "", type: "robocopy", source: "", destination: "", enabled: true }
    : state.config.sync_jobs[index];

  $("#job-modal-title").textContent = isNew ? "New job" : `Edit ${job.name}`;
  $("#job-delete").hidden = isNew;
  $("#job-error").hidden = true;

  const serverSelect = $("#job-server");
  serverSelect.textContent = "";
  state.servers.forEach((server) => {
    const option = document.createElement("option");
    option.value = server.name;
    option.textContent = server.name;
    serverSelect.append(option);
  });

  $("#job-name").value = job.name ?? "";
  $("#job-type").value = job.type ?? "robocopy";
  serverSelect.value = job.server ?? state.servers[0]?.name ?? "";
  $("#job-source").value = job.source ?? "";
  $("#job-destination").value = job.destination ?? "";
  $("#job-mirror").checked = job.robocopy_mirror !== false;
  $("#job-level").value = job.robocopy_level ?? "";
  $("#job-dirnames").value = (job.blacklisted_dirnames || []).join("\n");
  $("#job-filenames").value = (job.blacklisted_filenames || []).join("\n");
  $("#job-enabled").checked = job.enabled !== false;

  syncJobModalType();
  setPreviewVisible(false);
  openModal($("#job-modal"));
}

/** Build a job object from the modal fields, omitting empty optional keys. */
function readJobModal() {
  const type = $("#job-type").value;
  const job = {
    name: $("#job-name").value.trim(),
    type,
    source: $("#job-source").value.trim(),
    destination: $("#job-destination").value.trim(),
  };

  if (type === "rclone") {
    job.server = $("#job-server").value;
  } else {
    if (!$("#job-mirror").checked) job.robocopy_mirror = false;
    const level = $("#job-level").value.trim();
    if (level !== "") job.robocopy_level = Number(level);
  }

  const dirnames = linesToList($("#job-dirnames").value);
  if (dirnames.length) job.blacklisted_dirnames = dirnames;
  const filenames = linesToList($("#job-filenames").value);
  if (filenames.length) job.blacklisted_filenames = filenames;

  if (!$("#job-enabled").checked) job.enabled = false;
  return job;
}

$("#job-save").addEventListener("click", async () => {
  const config = structuredClone(state.config);
  const job = readJobModal();

  if (editingJobIndex === null) config.sync_jobs.push(job);
  else config.sync_jobs[editingJobIndex] = job;

  await persistConfig(
    config,
    editingJobIndex === null ? `Job "${job.name}" created.` : `Job "${job.name}" saved.`,
    $("#job-error"),
  );
});

$("#job-delete").addEventListener("click", async () => {
  const job = state.config.sync_jobs[editingJobIndex];
  if (!confirm(`Delete job "${job.name}"? This only removes the entry from the config.`)) {
    return;
  }
  const config = structuredClone(state.config);
  config.sync_jobs.splice(editingJobIndex, 1);
  await persistConfig(config, `Job "${job.name}" deleted.`, $("#job-error"));
});

$("#btn-add-job").addEventListener("click", () => openJobModal(null));

/* -- command preview ----------------------------------------------------- */

/* The backend renders the command from a job object, so the preview reflects
 * what is in the fields right now rather than what was last saved -- which is
 * the point of checking excludes before letting a mirror delete anything.
 */

/** Render one argument as it would be typed into a shell. */
function quoteArg(argument) {
  const text = String(argument);
  return /[\s"]/.test(text) ? `"${text.replaceAll('"', '\\"')}"` : text;
}

function setPreviewVisible(visible) {
  $("#job-preview").hidden = !visible;
  $("#job-preview-toggle").textContent = visible ? "Hide command" : "Preview command";
  $("#job-preview-toggle").prepend(icon("terminal"));
}

async function refreshPreview() {
  const output = $("#job-preview-command");
  try {
    const preview = await api("/api/jobs/preview", {
      method: "POST",
      body: JSON.stringify({
        job: readJobModal(),
        dry_run: $("#dry-run").checked,
      }),
    });
    output.textContent = preview.command.map(quoteArg).join(" ");
  } catch (error) {
    output.textContent = `Could not build the command: ${error.message}`;
  }
}

$("#job-preview-toggle").addEventListener("click", async () => {
  const showing = $("#job-preview").hidden;
  setPreviewVisible(showing);
  if (showing) await refreshPreview();
});

$("#job-preview-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("#job-preview-command").textContent);
    toast("Command copied.");
  } catch (error) {
    toast(`Could not copy: ${error.message}`, "error");
  }
});

/* -- server modal ------------------------------------------------------- */

let editingServerIndex = null;

function openServerModal(index) {
  editingServerIndex = index;
  const isNew = index === null;
  const server = isNew
    ? { name: "", host: "", user: "", ssh_key_path: "", port: 22 }
    : state.config.servers[index];

  $("#server-modal-title").textContent = isNew ? "New server" : `Edit ${server.name}`;
  $("#server-delete").hidden = isNew;
  $("#server-error").hidden = true;

  $("#server-name").value = server.name ?? "";
  $("#server-host").value = server.host ?? "";
  $("#server-user").value = server.user ?? "";
  $("#server-key").value = server.ssh_key_path ?? "";
  $("#server-port").value = server.port ?? 22;

  openModal($("#server-modal"));
}

$("#server-save").addEventListener("click", async () => {
  const config = structuredClone(state.config);
  const server = {
    name: $("#server-name").value.trim(),
    host: $("#server-host").value.trim(),
    user: $("#server-user").value.trim(),
    ssh_key_path: $("#server-key").value.trim(),
    port: Number($("#server-port").value) || 22,
  };

  if (editingServerIndex === null) {
    config.servers.push(server);
  } else {
    // Keep jobs pointing at this server when it is renamed.
    const previousName = config.servers[editingServerIndex].name;
    config.servers[editingServerIndex] = server;
    if (previousName !== server.name) {
      config.sync_jobs.forEach((job) => {
        if (job.type === "rclone" && job.server === previousName) job.server = server.name;
      });
    }
  }

  await persistConfig(
    config,
    editingServerIndex === null
      ? `Server "${server.name}" created.`
      : `Server "${server.name}" saved.`,
    $("#server-error"),
  );
});

$("#server-delete").addEventListener("click", async () => {
  const server = state.config.servers[editingServerIndex];
  const dependents = state.config.sync_jobs.filter(
    (job) => job.type === "rclone" && job.server === server.name,
  );
  if (dependents.length) {
    const error = $("#server-error");
    error.hidden = false;
    error.textContent =
      `${dependents.length} job(s) still reference "${server.name}". ` +
      "Reassign or delete them first.";
    return;
  }
  if (!confirm(`Delete server "${server.name}"?`)) return;

  const config = structuredClone(state.config);
  config.servers.splice(editingServerIndex, 1);
  await persistConfig(config, `Server "${server.name}" deleted.`, $("#server-error"));
});

$("#btn-add-server").addEventListener("click", () => openServerModal(null));

/* -- config loading ----------------------------------------------------- */

function applyConfig(payload) {
  state.config = payload.config;
  state.jobs = payload.jobs;
  state.servers = payload.servers;
  // Drop probe results for servers that no longer exist.
  const names = new Set(state.servers.map((server) => server.name));
  Object.keys(state.serverStatus).forEach((name) => {
    if (!names.has(name)) delete state.serverStatus[name];
  });

  renderServers();
  renderFiltered();
}

async function loadConfig() {
  try {
    applyConfig(await api("/api/config"));
  } catch (error) {
    toast(`Could not load config: ${error.message}`, "error");
    pushFeed(`Could not load config: ${error.message}`, "error");
  }
}

/* -- history ------------------------------------------------------------ */

function statRow(label, value, sub = "") {
  const card = document.createElement("div");
  card.className = "rounded-xl border border-edge bg-raised p-3.5";

  const labelEl = document.createElement("div");
  labelEl.className = "text-[11px] uppercase tracking-wider text-dim";
  labelEl.textContent = label;

  const valueEl = document.createElement("div");
  valueEl.className = "mt-1 text-[22px] font-semibold";
  valueEl.textContent = value;

  card.append(labelEl, valueEl);
  if (sub) {
    const subEl = document.createElement("div");
    subEl.className = "font-mono text-xs text-dim";
    subEl.textContent = sub;
    card.append(subEl);
  }
  return card;
}

async function loadHistory() {
  try {
    const [summary, history] = await Promise.all([
      api("/api/history/summary"),
      api("/api/history?limit=200"),
    ]);

    // Job cards show "last run" from this roll-up.
    state.jobHistory = {};
    summary.jobs.forEach((job) => {
      state.jobHistory[`${job.name}|${job.type}|${job.server}`] = job;
    });
    renderJobs();

    const totals = summary.totals;
    const grid = $("#history-totals");
    grid.textContent = "";
    grid.append(
      statRow("Total runs", totals.runs.toLocaleString()),
      statRow("Failed runs", totals.failures.toLocaleString(),
        totals.runs ? `${((totals.failures / totals.runs) * 100).toFixed(1)}% failure rate` : ""),
      statRow("Uploaded", formatBytes(totals.uploaded_bytes),
        `${totals.uploaded_files.toLocaleString()} files`),
      statRow("Deleted", formatBytes(totals.deleted_bytes),
        `${totals.deleted_files.toLocaleString()} files`),
      statRow("Errors logged", totals.error_count.toLocaleString()),
    );

    const jobsBody = $("#history-jobs tbody");
    jobsBody.textContent = "";
    summary.jobs.forEach((job) => {
      const row = jobsBody.insertRow();
      [job.name, job.type, job.server].forEach((value) => {
        row.insertCell().textContent = value;
      });
      [
        job.runs, job.failures, formatTimestamp(job.last_ended_at),
        `${Math.round(job.elapsed_seconds / 60)}m`,
        countAndSize(job.uploaded_files, job.uploaded_bytes),
        countAndSize(job.deleted_files, job.deleted_bytes),
        job.error_count,
      ].forEach((value, position) => {
        const cell = row.insertCell();
        cell.textContent = value;
        if (position !== 2) cell.className = "num";
      });
    });

    const runsBody = $("#history-runs tbody");
    runsBody.textContent = "";
    if (!history.entries.length) {
      const row = runsBody.insertRow();
      row.className = "empty";
      const cell = row.insertCell();
      cell.colSpan = 8;
      cell.textContent = "No runs recorded yet.";
      return;
    }

    history.entries.forEach((entry) => {
      const stats = entry.stats || {};
      const row = runsBody.insertRow();
      row.insertCell().textContent = formatTimestamp(entry.ended_at);
      row.insertCell().textContent = entry.dry_run ? `${entry.name} (dry)` : entry.name;
      row.insertCell().textContent = entry.server;

      const status = row.insertCell();
      status.className = STATUS_CLASSES[entry.status] || "";
      status.textContent = entry.status;

      const uploaded = row.insertCell();
      uploaded.className = "num";
      uploaded.textContent = countAndSize(stats.uploaded_files || 0, stats.uploaded_bytes || 0);

      const deleted = row.insertCell();
      deleted.className = "num";
      deleted.textContent = countAndSize(stats.deleted_files || 0, stats.deleted_bytes || 0);

      const elapsed = row.insertCell();
      elapsed.className = "num";
      elapsed.textContent = stats.elapsed || "";

      const error = row.insertCell();
      error.className = "wrap";
      error.textContent = stats.last_error || "";
      if (stats.last_error) error.dataset.tip = stats.last_error;
    });
  } catch (error) {
    pushFeed(`Could not load history: ${error.message}`, "error");
  }
}

$("#btn-refresh-history").addEventListener("click", loadHistory);

/* -- logs --------------------------------------------------------------- */

/** Fill the date picker with the days that actually have a log.
 *
 * Today is always offered, even before anything has failed, so an empty log
 * reads as a quiet day rather than a missing option.
 */
async function loadLogDates() {
  const select = $("#log-date");
  const wanted = select.value;

  let dates = [];
  let today = "";
  try {
    ({ dates, today } = await api("/api/logs/dates"));
  } catch (error) {
    pushFeed(`Could not list logs: ${error.message}`, "error");
  }
  if (today && !dates.includes(today)) dates = [today, ...dates];

  select.textContent = "";
  dates.forEach((date) => {
    const option = document.createElement("option");
    option.value = date;
    option.textContent = date === today ? `${date} (today)` : date;
    select.append(option);
  });

  // Keep reading the same day across a refresh, unless its log has gone.
  select.value = dates.includes(wanted) ? wanted : (dates[0] ?? "");
  select.disabled = dates.length < 2;
}

async function loadLogs() {
  const date = $("#log-date").value;
  const path = date ? `/api/logs?date=${encodeURIComponent(date)}` : "/api/logs";
  try {
    const log = await api(path);
    $("#log-path").textContent = log.path;
    $("#log-content").textContent = log.exists && log.lines.length
      ? log.lines.join("\n")
      : `No failures logged on ${log.date}.`;
  } catch (error) {
    $("#log-content").textContent = `Could not read log: ${error.message}`;
  }
}

/** Refresh the list of days, then the day on show. */
async function reloadLogs() {
  await loadLogDates();
  await loadLogs();
}

$("#log-date").addEventListener("change", loadLogs);
$("#btn-refresh-logs").addEventListener("click", reloadLogs);

/* -- start -------------------------------------------------------------- */

(async () => {
  expandButtonIcons();
  initTheme();

  // Placeholders first, then both requests at once: the cards only settle once
  // the history is in, so waiting for it costs nothing and avoids a card that
  // says "Never run" for a moment before correcting itself.
  renderServers();
  renderJobs();
  await Promise.all([loadConfig(), loadHistory()]);
  state.loaded = true;
  renderServers();
  renderFiltered();

  connectLiveFeed();
  checkServers();
})();
