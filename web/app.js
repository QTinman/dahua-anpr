/* Dahua ANPR Monitor - frontend */
"use strict";

const $ = (sel) => document.querySelector(sel);
const MAX_LIVE_ROWS = 200;
const PAGE_SIZE = 50;

/* ------------------------------------------------------------- helpers */

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (_) {}
    // A 401 on a normal call (not the auth flow itself) means the session
    // lapsed; let the app surface the login screen.
    if (resp.status === 401 && !path.startsWith("/api/auth/")) {
      window.dispatchEvent(new Event("anpr-unauthorized"));
    }
    throw new Error(detail);
  }
  return resp.status === 204 ? null : resp.json();
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function esc(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

/* ---------------------------------------------------------------- tabs */

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("#tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "cameras") loadCameras();
    if (btn.dataset.tab === "reports") loadReportSettings();
    if (btn.dataset.tab === "search") populateCameraFilter();
    if (btn.dataset.tab === "playback") initPlayback();
    if (btn.dataset.tab === "whitelist") loadWhitelist();
    if (btn.dataset.tab === "reports") {
      loadAccessSettings(); loadAccessLog(); loadRetentionSettings();
    }
  });
});

/* ------------------------------------------------------------ websocket */

let ws = null;
const cameraStatuses = new Map(); // camera_id -> {name, status, detail}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => setWsStatus(true);
  ws.onclose = () => { setWsStatus(false); setTimeout(connectWs, 3000); };
  ws.onerror = () => ws.close();
  ws.onmessage = (msg) => {
    let data;
    try { data = JSON.parse(msg.data); } catch (_) { return; }
    if (data.type === "anpr_event") onLiveEvent(data.event);
    else if (data.type === "camera_status") onCameraStatus(data);
    else if (data.type === "event_image") onEventImage(data.event_id);
    else if (data.type === "access_log") onAccessLog(data.entry);
  };
}

function setWsStatus(online) {
  const el = $("#ws-status");
  el.textContent = online ? "live" : "offline";
  el.classList.toggle("online", online);
  el.classList.toggle("offline", !online);
}

/* ------------------------------------------------------- camera badges */

function onCameraStatus(data) {
  cameraStatuses.set(data.camera_id, {
    name: data.camera_name, status: data.status, detail: data.detail || "",
  });
  renderBadges();
  // Keep the Cameras panel current even when it is not the visible tab, so
  // switching to it always shows up-to-date cards (and self-heals if a
  // transient load ever left it empty).
  loadCameras();
}

function renderBadges() {
  const wrap = $("#camera-badges");
  wrap.innerHTML = "";
  for (const [id, info] of cameraStatuses) {
    const badge = document.createElement("span");
    badge.className = `badge ${info.status}`;
    badge.title = info.detail || info.status;
    badge.innerHTML = `<span class="dot"></span>${esc(info.name)}`;
    wrap.appendChild(badge);
  }
}

/* ------------------------------------------------------------ live feed */

function eventRowHtml(ev) {
  const img = ev.has_image
    ? `<span class="img-yes" title="View image">📷</span>`
    : `<span class="img-no">—</span>`;
  return `
    <td>${esc(fmtTime(ev.received_at))}</td>
    <td>${esc(ev.camera_name)}</td>
    <td><span class="plate-chip">${esc(ev.plate || "?")}</span></td>
    <td>${esc(ev.country)}</td>
    <td>${esc(ev.vehicle_type)}</td>
    <td>${esc(ev.vehicle_color)}</td>
    <td>${esc(ev.vehicle_brand)}</td>
    <td>${ev.speed != null ? esc(ev.speed) + " km/h" : ""}</td>
    <td>${esc(ev.direction)}</td>
    <td>${ev.lane != null ? esc(ev.lane) : ""}</td>
    <td class="img-cell">${img}</td>`;
}

function makeLiveRow(ev) {
  const row = document.createElement("tr");
  row.dataset.eventId = ev.id;
  row.dataset.event = JSON.stringify(ev);
  row.innerHTML = eventRowHtml(ev);
  // Read the event back from the row on click: the dataset is kept up to
  // date when the image arrives later, a captured closure would be stale.
  row.addEventListener("click", () =>
    showDetail(JSON.parse(row.dataset.event)));
  return row;
}

function onLiveEvent(ev) {
  if ($("#live-pause").checked) return;
  const body = $("#live-body");
  body.querySelector(".empty-row")?.remove();
  const row = makeLiveRow(ev);
  row.className = "flash";
  body.prepend(row);
  while (body.children.length > MAX_LIVE_ROWS) body.lastChild.remove();
  showDetail(ev);
}

async function loadRecentEvents() {
  // Populate the live table with stored events so a page refresh does not
  // start from an empty screen.
  let result;
  try {
    result = await api(`/api/events?limit=${MAX_LIVE_ROWS}`);
  } catch (_) {
    return;
  }
  const body = $("#live-body");
  if (!result.events.length) return;
  body.innerHTML = "";
  for (const ev of result.events) { // newest first
    ev.has_image = !!ev.has_image;
    body.appendChild(makeLiveRow(ev));
  }
  showDetail(result.events[0]);
}

function onEventImage(eventId) {
  // Update rows in both live and search tables.
  document.querySelectorAll(`tr[data-event-id="${eventId}"]`).forEach((row) => {
    const ev = JSON.parse(row.dataset.event || "{}");
    ev.has_image = true;
    row.dataset.event = JSON.stringify(ev);
    const cell = row.querySelector(".img-cell");
    if (cell) cell.innerHTML = `<span class="img-yes" title="View image">📷</span>`;
  });
  if (String(currentDetailId) === String(eventId)) loadDetailImage(eventId);
}

/* ----------------------------------------------------------- detail pane */

let currentDetailId = null;

let currentDetailPlate = "";

function showDetail(ev) {
  currentDetailId = ev.id;
  currentDetailPlate = ev.plate || "";
  $("#detail-plate").textContent = ev.plate || "?";
  const wlBtn = $("#detail-whitelist");
  wlBtn.disabled = !currentDetailPlate;
  $("#detail-wl-msg").textContent = "";
  const fields = {
    Time: fmtTime(ev.received_at),
    Camera: ev.camera_name,
    "Event": ev.event_code,
    Country: ev.country,
    "Plate color": ev.plate_color,
    Vehicle: ev.vehicle_type,
    Color: ev.vehicle_color,
    Brand: ev.vehicle_brand,
    Size: ev.vehicle_size,
    Speed: ev.speed != null ? ev.speed + " km/h" : "",
    Direction: ev.direction,
    Lane: ev.lane,
  };
  const dl = $("#detail-fields");
  dl.innerHTML = "";
  for (const [key, value] of Object.entries(fields)) {
    if (value === "" || value == null) continue;
    dl.insertAdjacentHTML("beforeend", `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`);
  }
  if (ev.has_image) loadDetailImage(ev.id);
  else {
    $("#detail-image").classList.add("empty");
    $("#detail-image").textContent = "no image";
  }
}

function loadDetailImage(eventId) {
  const box = $("#detail-image");
  box.classList.remove("empty");
  box.innerHTML = `<img src="/api/events/${eventId}/image?t=${Date.now()}" alt="Plate capture">`;
}

$("#live-clear").addEventListener("click", () => {
  $("#live-body").innerHTML =
    '<tr class="empty-row"><td colspan="11">Waiting for events…</td></tr>';
});

$("#detail-whitelist").addEventListener("click", async () => {
  if (!currentDetailPlate) return;
  const msg = $("#detail-wl-msg");
  msg.textContent = "";
  try {
    await api("/api/whitelist", {
      method: "POST",
      body: JSON.stringify({ plate: currentDetailPlate, label: "" }),
    });
    msg.textContent = `✓ ${currentDetailPlate} whitelisted`;
    msg.className = "muted msg-ok";
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "muted msg-err";
  }
});

// Double-click the latest-capture image to view it full size.
$("#detail-image").title = "Double-click for full size";
$("#detail-image").addEventListener("dblclick", () => {
  if (currentDetailId != null && $("#detail-image img")) {
    openImage(currentDetailId, $("#detail-plate").textContent);
  }
});

/* -------------------------------------------------------------- search */

let searchOffset = 0;
let searchTotal = 0;

function searchParams() {
  const params = new URLSearchParams();
  const plate = $("#search-plate").value.trim();
  const cameraId = $("#search-camera").value;
  const from = $("#search-from").value;
  const to = $("#search-to").value;
  if (plate) params.set("plate", plate);
  if (cameraId) params.set("camera_id", cameraId);
  if (from) params.set("date_from", from);
  if (to) params.set("date_to", to);
  return params;
}

async function runSearch() {
  const params = searchParams();
  params.set("limit", PAGE_SIZE);
  params.set("offset", searchOffset);
  const body = $("#search-body");
  body.innerHTML = '<tr class="empty-row"><td colspan="11">Searching…</td></tr>';
  try {
    const result = await api("/api/events?" + params);
    searchTotal = result.total;
    body.innerHTML = "";
    if (!result.events.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="11">No results.</td></tr>';
    }
    for (const ev of result.events) {
      ev.has_image = !!ev.has_image;
      const row = document.createElement("tr");
      row.dataset.eventId = ev.id;
      row.dataset.event = JSON.stringify(ev);
      row.innerHTML = eventRowHtml(ev);
      row.addEventListener("click", () => {
        const current = JSON.parse(row.dataset.event);
        if (current.has_image) openImage(current.id, current.plate);
      });
      body.appendChild(row);
    }
    $("#search-count").textContent = `${searchTotal} result${searchTotal === 1 ? "" : "s"}`;
    const page = Math.floor(searchOffset / PAGE_SIZE) + 1;
    const pages = Math.max(1, Math.ceil(searchTotal / PAGE_SIZE));
    $("#search-page").textContent = `Page ${page} / ${pages}`;
    $("#search-prev").disabled = searchOffset === 0;
    $("#search-next").disabled = searchOffset + PAGE_SIZE >= searchTotal;
  } catch (err) {
    body.innerHTML = `<tr class="empty-row"><td colspan="11">Error: ${esc(err.message)}</td></tr>`;
  }
}

$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  searchOffset = 0;
  runSearch();
});
$("#search-prev").addEventListener("click", () => {
  searchOffset = Math.max(0, searchOffset - PAGE_SIZE);
  runSearch();
});
$("#search-next").addEventListener("click", () => {
  searchOffset += PAGE_SIZE;
  runSearch();
});
$("#search-export").addEventListener("click", () => {
  window.location = "/api/events/export.csv?" + searchParams();
});

async function populateCameraFilter() {
  try {
    const cameras = await api("/api/cameras");
    const select = $("#search-camera");
    const current = select.value;
    select.innerHTML = '<option value="">All cameras</option>';
    for (const cam of cameras) {
      const opt = document.createElement("option");
      opt.value = cam.id;
      opt.textContent = cam.name;
      select.appendChild(opt);
    }
    select.value = current;
  } catch (_) { /* filter stays generic */ }
}

/* ------------------------------------------------------------- lightbox */

function openImage(eventId, plate) {
  $("#image-dialog-img").src = `/api/events/${eventId}/image?t=${Date.now()}`;
  $("#image-dialog-img").alt = plate || "Plate capture";
  $("#image-dialog").showModal();
}
$("#image-dialog-close").addEventListener("click", () => $("#image-dialog").close());

/* ---------------------------------------------------------------- about */
$("#about-btn").addEventListener("click", () => $("#about-dialog").showModal());
$("#about-close").addEventListener("click", () => $("#about-dialog").close());
$("#about-dialog").addEventListener("click", (e) => {
  // Click on the backdrop (outside the dialog content) closes it.
  if (e.target === $("#about-dialog")) $("#about-dialog").close();
});

document.addEventListener("click", (e) => {
  const icon = e.target.closest(".img-yes");
  if (!icon) return;
  const row = icon.closest("tr");
  if (row?.dataset.eventId) {
    e.stopPropagation();
    const ev = JSON.parse(row.dataset.event || "{}");
    openImage(row.dataset.eventId, ev.plate);
  }
}, true);

/* -------------------------------------------------------------- cameras */

let editingCameraId = null;

async function loadCameras() {
  const wrap = $("#camera-list");
  let cameras;
  try {
    cameras = await api("/api/cameras");
  } catch (err) {
    wrap.innerHTML = `<p class="msg-err">Failed to load cameras: ${esc(err.message)}</p>`;
    return;
  }
  wrap.innerHTML = "";
  if (!cameras.length) {
    wrap.innerHTML = '<p class="muted">No cameras configured yet. Click "Add camera" to get started.</p>';
    // Show which database file this server is using. If you configured a
    // camera before but see 0 here, the server was started from a different
    // directory and is reading a fresh, empty database — your data is still
    // in the original anpr.db. Pin ANPR_DB to a fixed absolute path.
    try {
      const d = await api("/api/diagnostics");
      wrap.insertAdjacentHTML("beforeend",
        `<p class="muted" style="margin-top:10px;font-size:12px">`
        + `Server database: <code>${esc(d.database_path)}</code><br>`
        + `This file currently holds ${d.cameras} camera(s) and ${d.events} event(s). `
        + `If you expected data here, the server is likely reading a different `
        + `anpr.db than before — set <code>ANPR_DB</code> to a fixed absolute path.`
        + `</p>`);
    } catch (_) { /* diagnostics optional */ }
    return;
  }
  for (const cam of cameras) {
    // Live status from WS beats the snapshot from the API response.
    const live = cameraStatuses.get(cam.id);
    const status = live ? live.status : cam.status;
    const detail = live ? live.detail : cam.status_detail;
    const card = document.createElement("div");
    card.className = "camera-card";
    card.innerHTML = `
      <div class="cam-head">
        <span class="badge ${esc(status)}"><span class="dot"></span>${esc(status)}</span>
        <h3>${esc(cam.name)}</h3>
      </div>
      <div class="cam-meta">${cam.use_https ? "https" : "http"}://${esc(cam.host)}:${cam.port}
        · ch ${cam.channel} · ${esc(cam.username)}</div>
      <div class="cam-meta">${cam.use_onvif
        ? "Source: ONVIF metadata (RTSP :" + cam.rtsp_port + ")"
        : "Events: " + esc(cam.event_codes)}</div>
      <div class="cam-detail">${esc(detail || "")}</div>
      <div class="cam-actions">
        <button class="btn small" data-action="edit">Edit</button>
        <button class="btn small" data-action="sync">Sync history</button>
        <button class="btn small" data-action="toggle">${cam.enabled ? "Disable" : "Enable"}</button>
        <button class="btn small danger" data-action="delete">Delete</button>
      </div>
      <div class="cam-sync muted"></div>`;
    card.querySelector('[data-action="edit"]').addEventListener("click", () => openCameraDialog(cam));
    card.querySelector('[data-action="sync"]').addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      const out = card.querySelector(".cam-sync");
      btn.disabled = true;
      out.className = "cam-sync muted";
      out.textContent = "Importing history from camera…";
      try {
        const r = await api(`/api/cameras/${cam.id}/sync`, { method: "POST" });
        if (r.ok) {
          out.className = "cam-sync msg-ok";
          out.textContent = `Imported ${r.imported} new record(s) `
            + `(${r.duplicates} already present, ${r.found} on camera).`;
        } else {
          out.className = "cam-sync msg-err";
          out.textContent = "Sync failed: " + r.error;
        }
      } catch (err) {
        out.className = "cam-sync msg-err";
        out.textContent = "Sync failed: " + err.message;
      } finally {
        btn.disabled = false;
      }
    });
    card.querySelector('[data-action="toggle"]').addEventListener("click", async () => {
      await api(`/api/cameras/${cam.id}`, {
        method: "PUT", body: JSON.stringify({ enabled: !cam.enabled }),
      });
      loadCameras();
    });
    card.querySelector('[data-action="delete"]').addEventListener("click", async () => {
      if (!confirm(`Delete camera "${cam.name}"? Stored events are kept.`)) return;
      await api(`/api/cameras/${cam.id}`, { method: "DELETE" });
      cameraStatuses.delete(cam.id);
      renderBadges();
      loadCameras();
    });
    wrap.appendChild(card);
  }
}

function openCameraDialog(cam = null) {
  editingCameraId = cam ? cam.id : null;
  $("#camera-dialog-title").textContent = cam ? `Edit camera — ${cam.name}` : "Add camera";
  $("#cam-name").value = cam?.name || "";
  $("#cam-host").value = cam?.host || "";
  $("#cam-port").value = cam?.port ?? 80;
  $("#cam-username").value = cam?.username || "admin";
  $("#cam-password").value = "";
  $("#cam-password").placeholder = cam ? "(unchanged)" : "";
  $("#cam-channel").value = cam?.channel ?? 1;
  $("#cam-codes").value = cam?.event_codes || "TrafficJunction";
  $("#cam-https").checked = cam?.use_https || false;
  $("#cam-snapshot").checked = cam ? cam.snapshot_on_event : true;
  $("#cam-onvif").checked = cam ? cam.use_onvif : false;
  $("#cam-rtsp-port").value = cam?.rtsp_port ?? 554;
  $("#cam-direction").value = cam?.direction_mode || "";
  $("#cam-enabled").checked = cam ? cam.enabled : true;
  $("#cam-test-result").textContent = "";
  $("#camera-dialog").showModal();
}

$("#camera-add").addEventListener("click", () => openCameraDialog());
$("#cam-cancel").addEventListener("click", () => $("#camera-dialog").close());

$("#cam-test").addEventListener("click", async () => {
  const out = $("#cam-test-result");
  out.textContent = "Testing…";
  out.className = "muted";
  try {
    const result = await api("/api/cameras/test", {
      method: "POST",
      body: JSON.stringify({
        host: $("#cam-host").value.trim(),
        port: parseInt($("#cam-port").value, 10) || 80,
        username: $("#cam-username").value.trim(),
        password: $("#cam-password").value,
        use_https: $("#cam-https").checked,
        camera_id: editingCameraId,
      }),
    });
    if (result.ok) {
      const dev = result.device || {};
      out.textContent = `✓ Connected${dev.type ? " — " + dev.type : ""}${dev.version ? " (fw " + dev.version + ")" : ""}`;
      out.className = "msg-ok";
    } else {
      out.textContent = "✗ " + result.error;
      out.className = "msg-err";
    }
  } catch (err) {
    out.textContent = "✗ " + err.message;
    out.className = "msg-err";
  }
});

$("#camera-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    name: $("#cam-name").value.trim(),
    host: $("#cam-host").value.trim(),
    port: parseInt($("#cam-port").value, 10) || 80,
    username: $("#cam-username").value.trim(),
    password: $("#cam-password").value,
    channel: parseInt($("#cam-channel").value, 10) || 1,
    event_codes: $("#cam-codes").value.trim() || "TrafficJunction",
    use_https: $("#cam-https").checked,
    snapshot_on_event: $("#cam-snapshot").checked,
    use_onvif: $("#cam-onvif").checked,
    rtsp_port: parseInt($("#cam-rtsp-port").value, 10) || 554,
    direction_mode: $("#cam-direction").value,
    enabled: $("#cam-enabled").checked,
  };
  try {
    if (editingCameraId == null) {
      await api("/api/cameras", { method: "POST", body: JSON.stringify(body) });
    } else {
      await api(`/api/cameras/${editingCameraId}`, {
        method: "PUT", body: JSON.stringify(body),
      });
    }
    $("#camera-dialog").close();
    loadCameras();
    populateCameraFilter();
  } catch (err) {
    $("#cam-test-result").textContent = "✗ " + err.message;
    $("#cam-test-result").className = "msg-err";
  }
});

/* -------------------------------------------------------------- reports */

async function loadReportSettings() {
  try {
    const settings = await api("/api/settings/report");
    $("#report-enabled").checked = settings.enabled;
    $("#report-time").value = settings.time;
    $("#report-dir").value = settings.directory;
  } catch (_) { /* keep defaults */ }
}

$("#report-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#report-msg");
  try {
    await api("/api/settings/report", {
      method: "PUT",
      body: JSON.stringify({
        enabled: $("#report-enabled").checked,
        time: $("#report-time").value || "23:59",
        directory: $("#report-dir").value.trim() || "reports",
      }),
    });
    msg.textContent = "Saved.";
    msg.className = "msg-ok";
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "msg-err";
  }
});

/* ------------------------------------------------------------- retention */

async function loadRetentionSettings() {
  try {
    const s = await api("/api/settings/retention");
    $("#ret-enabled").checked = s.enabled;
    $("#ret-days").value = s.days;
    $("#ret-delete-records").checked = s.delete_records;
  } catch (_) {}
}

$("#retention-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#ret-msg");
  try {
    await api("/api/settings/retention", {
      method: "PUT",
      body: JSON.stringify({
        enabled: $("#ret-enabled").checked,
        days: parseInt($("#ret-days").value, 10) || 30,
        delete_records: $("#ret-delete-records").checked,
      }),
    });
    msg.textContent = "Saved."; msg.className = "msg-ok";
  } catch (err) { msg.textContent = err.message; msg.className = "msg-err"; }
});

$("#ret-run").addEventListener("click", async () => {
  const msg = $("#ret-msg");
  msg.textContent = "Cleaning…"; msg.className = "muted";
  try {
    // Save current settings first so the run uses them.
    await api("/api/settings/retention", {
      method: "PUT",
      body: JSON.stringify({
        enabled: $("#ret-enabled").checked,
        days: parseInt($("#ret-days").value, 10) || 30,
        delete_records: $("#ret-delete-records").checked,
      }),
    });
    const r = await api("/api/retention/run", { method: "POST" });
    msg.textContent = `Removed ${r.purged} old snapshot(s).`;
    msg.className = "msg-ok";
  } catch (err) { msg.textContent = err.message; msg.className = "msg-err"; }
});

$("#report-run").addEventListener("click", async () => {
  const msg = $("#report-msg");
  msg.textContent = "Generating…";
  msg.className = "muted";
  try {
    const result = await api("/api/reports/run", { method: "POST" });
    msg.textContent = "Report written: " + result.path;
    msg.className = "msg-ok";
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "msg-err";
  }
});

/* -------------------------------------------------------------- playback */

const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
  "August", "September", "October", "November", "December"];
let pbMonth = null;          // Date pointing at the shown month (day 1)
let pbCounts = {};           // "YYYY-MM-DD" -> count for the shown month
let pbDay = null;            // selected "YYYY-MM-DD"
let pbEvents = [];           // events of the selected day (ascending)
let pbIndex = 0;
let pbTimer = null;
let pbInited = false;

function ymd(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-`
    + String(d.getDate()).padStart(2, "0");
}

function initPlayback() {
  if (pbInited) return;
  pbInited = true;
  const now = new Date();
  pbMonth = new Date(now.getFullYear(), now.getMonth(), 1);
  $("#cal-prev").addEventListener("click", () => shiftMonth(-1));
  $("#cal-next").addEventListener("click", () => shiftMonth(1));
  $("#pb-prev").addEventListener("click", () => { pbStop(); pbGo(pbIndex - 1); });
  $("#pb-next").addEventListener("click", () => { pbStop(); pbGo(pbIndex + 1); });
  $("#pb-play").addEventListener("click", pbTogglePlay);
  $("#pb-image").title = "Double-click for full size";
  $("#pb-image").addEventListener("dblclick", () => {
    const ev = pbEvents[pbIndex];
    if (ev && ev.has_image) openImage(ev.id, ev.plate);
  });
  renderCalendar();
  // Auto-select today if it has data, else the latest day with data.
  loadCalendar().then(() => {
    const today = ymd(new Date());
    if (pbCounts[today]) selectDay(today);
    else {
      const days = Object.keys(pbCounts).sort();
      if (days.length) selectDay(days[days.length - 1]);
    }
  });
}

function shiftMonth(delta) {
  pbMonth = new Date(pbMonth.getFullYear(), pbMonth.getMonth() + delta, 1);
  renderCalendar();
  loadCalendar();
}

async function loadCalendar() {
  const month = `${pbMonth.getFullYear()}-${String(pbMonth.getMonth() + 1).padStart(2, "0")}`;
  try {
    const r = await api(`/api/events/calendar?month=${month}`);
    pbCounts = r.counts || {};
  } catch (_) { pbCounts = {}; }
  renderCalendar();
}

function renderCalendar() {
  $("#cal-title").textContent = `${MONTHS[pbMonth.getMonth()]} ${pbMonth.getFullYear()}`;
  const grid = $("#calendar-grid");
  grid.innerHTML = "";
  const year = pbMonth.getFullYear(), month = pbMonth.getMonth();
  const first = new Date(year, month, 1);
  // Monday-based offset.
  let lead = (first.getDay() + 6) % 7;
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const todayStr = ymd(new Date());
  // Leading days from previous month.
  for (let i = 0; i < lead; i++) {
    const cell = document.createElement("div");
    cell.className = "cal-day other";
    grid.appendChild(cell);
  }
  for (let d = 1; d <= daysInMonth; d++) {
    const dateStr = `${year}-${String(month + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    const count = pbCounts[dateStr] || 0;
    const cell = document.createElement("div");
    cell.className = "cal-day" + (count ? " has-data" : "")
      + (dateStr === pbDay ? " selected" : "") + (dateStr === todayStr ? " today" : "");
    cell.innerHTML = `<span>${d}</span>` + (count
      ? `<span class="cal-count">${count}</span>` : "");
    if (count) cell.addEventListener("click", () => selectDay(dateStr));
    grid.appendChild(cell);
  }
}

async function selectDay(dateStr) {
  pbStop();
  pbDay = dateStr;
  renderCalendar();
  const strip = $("#pb-filmstrip");
  strip.innerHTML = '<span class="muted" style="padding:10px">Loading…</span>';
  try {
    const r = await api(`/api/events/day?date=${dateStr}`);
    pbEvents = r.events || [];
  } catch (err) {
    strip.innerHTML = `<span class="msg-err" style="padding:10px">${esc(err.message)}</span>`;
    return;
  }
  renderFilmstrip();
  const has = pbEvents.length > 0;
  $("#pb-prev").disabled = !has;
  $("#pb-next").disabled = !has;
  $("#pb-play").disabled = !has;
  if (has) pbGo(0);
  else {
    $("#pb-image").className = "plate-image empty";
    $("#pb-image").textContent = "no captures this day";
    $("#pb-plate").textContent = "—";
    $("#pb-fields").innerHTML = "";
    $("#pb-pos").textContent = "0 / 0";
  }
}

function renderFilmstrip() {
  const strip = $("#pb-filmstrip");
  strip.innerHTML = "";
  if (!pbEvents.length) {
    strip.innerHTML = '<span class="muted" style="padding:10px">No captures this day.</span>';
    return;
  }
  pbEvents.forEach((ev, i) => {
    const item = document.createElement("div");
    item.className = "film-item" + (i === pbIndex ? " active" : "");
    item.dataset.i = i;
    const thumb = ev.has_image
      ? `<img loading="lazy" src="/api/events/${ev.id}/image" alt="">`
      : `<div class="film-noimg">no image</div>`;
    const t = fmtTime(ev.received_at).split(", ")[1] || "";
    item.innerHTML = thumb
      + `<div class="film-plate">${esc(ev.plate || "?")}</div>`
      + `<div class="film-cap">${esc(t)}</div>`;
    item.addEventListener("click", () => { pbStop(); pbGo(i); });
    strip.appendChild(item);
  });
}

function pbGo(index) {
  if (!pbEvents.length) return;
  pbIndex = Math.max(0, Math.min(index, pbEvents.length - 1));
  const ev = pbEvents[pbIndex];
  $("#pb-plate").textContent = ev.plate || "?";
  const box = $("#pb-image");
  if (ev.has_image) {
    box.className = "plate-image";
    box.innerHTML = `<img src="/api/events/${ev.id}/image" alt="capture">`;
  } else {
    box.className = "plate-image empty";
    box.textContent = "no image";
  }
  const fields = {
    Time: fmtTime(ev.received_at), Camera: ev.camera_name, Event: ev.event_code,
    Country: ev.country, "Plate color": ev.plate_color, Vehicle: ev.vehicle_type,
    Color: ev.vehicle_color, Brand: ev.vehicle_brand, Size: ev.vehicle_size,
    Speed: ev.speed != null ? ev.speed + " km/h" : "", Direction: ev.direction,
    Lane: ev.lane,
  };
  const dl = $("#pb-fields");
  dl.innerHTML = "";
  for (const [k, v] of Object.entries(fields)) {
    if (v === "" || v == null) continue;
    dl.insertAdjacentHTML("beforeend", `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`);
  }
  $("#pb-pos").textContent = `${pbIndex + 1} / ${pbEvents.length}`;
  $("#pb-prev").disabled = pbIndex === 0 && !pbTimer;
  $("#pb-next").disabled = pbIndex === pbEvents.length - 1 && !pbTimer;
  // highlight + scroll filmstrip
  document.querySelectorAll(".film-item").forEach((el) =>
    el.classList.toggle("active", Number(el.dataset.i) === pbIndex));
  const active = $(`.film-item[data-i="${pbIndex}"]`);
  if (active) active.scrollIntoView({ inline: "center", block: "nearest" });
}

function pbTogglePlay() {
  if (pbTimer) { pbStop(); return; }
  if (!pbEvents.length) return;
  if (pbIndex >= pbEvents.length - 1) pbIndex = -1; // restart from beginning
  $("#pb-play").textContent = "❚❚ Pause";
  const step = () => {
    if (pbIndex >= pbEvents.length - 1) { pbStop(); return; }
    pbGo(pbIndex + 1);
  };
  const speed = parseInt($("#pb-speed").value, 10) || 1200;
  step();
  pbTimer = setInterval(step, speed);
}

function pbStop() {
  if (pbTimer) { clearInterval(pbTimer); pbTimer = null; }
  $("#pb-play").textContent = "▶ Play";
  if (pbEvents.length) {
    $("#pb-prev").disabled = pbIndex === 0;
    $("#pb-next").disabled = pbIndex === pbEvents.length - 1;
  }
}

/* ------------------------------------------------------------- whitelist */

async function loadWhitelist() {
  const body = $("#wl-body");
  let entries;
  try { entries = await api("/api/whitelist"); }
  catch (err) {
    body.innerHTML = `<tr class="empty-row"><td colspan="4">Error: ${esc(err.message)}</td></tr>`;
    return;
  }
  body.innerHTML = "";
  if (!entries.length) {
    body.innerHTML = '<tr class="empty-row"><td colspan="4">No plates yet.</td></tr>';
    return;
  }
  for (const e of entries) {
    const row = document.createElement("tr");
    row.innerHTML = `<td><span class="plate-chip">${esc(e.plate)}</span></td>`
      + `<td>${esc(e.label || "")}</td>`
      + `<td class="muted">${esc(fmtTime(e.created_at))}</td>`
      + `<td><button class="btn small danger" data-del="${e.id}">Remove</button></td>`;
    row.querySelector("[data-del]").addEventListener("click", async () => {
      await api(`/api/whitelist/${e.id}`, { method: "DELETE" });
      loadWhitelist();
    });
    body.appendChild(row);
  }
}

$("#wl-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#wl-msg");
  try {
    await api("/api/whitelist", {
      method: "POST",
      body: JSON.stringify({
        plate: $("#wl-plate").value.trim(),
        label: $("#wl-label").value.trim(),
      }),
    });
    $("#wl-plate").value = "";
    $("#wl-label").value = "";
    msg.textContent = "";
    loadWhitelist();
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "msg-err";
  }
});

/* -------------------------------------------------------- access control */

function accessFormToBody() {
  return {
    enabled: $("#acc-enabled").checked,
    gate_enabled: $("#acc-gate-enabled").checked,
    gate_open_path: $("#acc-gate-open").value.trim(),
    gate_close_path: $("#acc-gate-close").value.trim(),
    gate_pulse_seconds: parseFloat($("#acc-gate-pulse").value) || 0,
    email_enabled: $("#acc-email-enabled").checked,
    smtp_host: $("#acc-smtp-host").value.trim(),
    smtp_port: parseInt($("#acc-smtp-port").value, 10) || 587,
    smtp_user: $("#acc-smtp-user").value.trim(),
    smtp_password: $("#acc-smtp-pass").value,
    smtp_tls: $("#acc-smtp-tls").checked,
    email_from: $("#acc-email-from").value.trim(),
    email_to: $("#acc-email-to").value.trim(),
    email_attach_image: $("#acc-email-image").checked,
    debounce_seconds: parseInt($("#acc-debounce").value, 10) || 0,
  };
}

async function loadAccessSettings() {
  // Populate the gate-camera dropdown from configured cameras.
  try {
    const cameras = await api("/api/cameras");
    const sel = $("#acc-gate-camera");
    sel.innerHTML = "";
    for (const cam of cameras) {
      const opt = document.createElement("option");
      opt.value = cam.id; opt.textContent = cam.name;
      sel.appendChild(opt);
    }
  } catch (_) {}
  try {
    const s = await api("/api/settings/access");
    $("#acc-enabled").checked = s.enabled;
    $("#acc-gate-enabled").checked = s.gate_enabled;
    $("#acc-gate-open").value = s.gate_open_path;
    $("#acc-gate-close").value = s.gate_close_path;
    $("#acc-gate-pulse").value = s.gate_pulse_seconds;
    $("#acc-email-enabled").checked = s.email_enabled;
    $("#acc-smtp-host").value = s.smtp_host;
    $("#acc-smtp-port").value = s.smtp_port;
    $("#acc-smtp-user").value = s.smtp_user;
    $("#acc-smtp-pass").value = "";
    $("#acc-smtp-pass").placeholder = s.smtp_password_set ? "(unchanged)" : "";
    $("#acc-smtp-tls").checked = s.smtp_tls;
    $("#acc-email-from").value = s.email_from;
    $("#acc-email-to").value = s.email_to;
    $("#acc-email-image").checked = s.email_attach_image;
    $("#acc-debounce").value = s.debounce_seconds;
    if (s.gate_camera_id) $("#acc-gate-camera").value = s.gate_camera_id;
  } catch (_) {}
}

$("#access-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#acc-msg");
  try {
    await api("/api/settings/access", {
      method: "PUT", body: JSON.stringify(accessFormToBody()),
    });
    msg.textContent = "Saved."; msg.className = "msg-ok";
  } catch (err) { msg.textContent = err.message; msg.className = "msg-err"; }
});

$("#acc-gate-test").addEventListener("click", async () => {
  const msg = $("#acc-gate-msg");
  const camId = $("#acc-gate-camera").value;
  if (!camId) { msg.textContent = "Add a camera first."; msg.className = "msg-err"; return; }
  msg.textContent = "Firing…"; msg.className = "muted";
  // Save first so the test uses the current paths.
  try {
    await api("/api/settings/access", { method: "PUT", body: JSON.stringify(accessFormToBody()) });
    const r = await api(`/api/access/test-gate?camera_id=${camId}`, { method: "POST" });
    if (r.ok) { msg.textContent = "✓ Gate output fired."; msg.className = "msg-ok"; }
    else { msg.textContent = "✗ " + r.error; msg.className = "msg-err"; }
  } catch (err) { msg.textContent = "✗ " + err.message; msg.className = "msg-err"; }
});

const ACC_ACTIONS = { gate_open: "Gate open", email_alert: "Email alert" };

function accLogRowHtml(e) {
  const action = ACC_ACTIONS[e.action] || e.action;
  const res = e.result === "ok"
    ? '<span class="msg-ok">ok</span>'
    : `<span class="msg-err">error</span>`;
  return `<td>${esc(fmtTime(e.time))}</td>`
    + `<td><span class="plate-chip">${esc(e.plate || "?")}</span></td>`
    + `<td>${esc(e.camera_name)}</td><td>${esc(action)}</td>`
    + `<td>${res}</td><td class="muted">${esc(e.detail || "")}</td>`;
}

async function loadAccessLog() {
  const body = $("#acc-log-body");
  let entries;
  try { entries = await api("/api/access/log?limit=200"); }
  catch (_) { return; }
  body.innerHTML = "";
  if (!entries.length) {
    body.innerHTML = '<tr class="empty-row"><td colspan="6">No access events yet.</td></tr>';
    return;
  }
  for (const e of entries) {
    const row = document.createElement("tr");
    row.innerHTML = accLogRowHtml(e);
    body.appendChild(row);
  }
}

function onAccessLog(entry) {
  const body = $("#acc-log-body");
  if (!body) return;
  body.querySelector(".empty-row")?.remove();
  const row = document.createElement("tr");
  row.className = "flash";
  row.innerHTML = accLogRowHtml(entry);
  body.prepend(row);
  while (body.children.length > 300) body.lastChild.remove();
}

$("#acc-log-refresh").addEventListener("click", loadAccessLog);

$("#acc-email-test").addEventListener("click", async () => {
  const msg = $("#acc-email-msg");
  msg.textContent = "Sending…"; msg.className = "muted";
  try {
    const r = await api("/api/access/test-email", {
      method: "POST", body: JSON.stringify(accessFormToBody()),
    });
    if (r.ok) { msg.textContent = "✓ Test email sent."; msg.className = "msg-ok"; }
    else { msg.textContent = "✗ " + r.error; msg.className = "msg-err"; }
  } catch (err) { msg.textContent = "✗ " + err.message; msg.className = "msg-err"; }
});

/* ------------------------------------------------------------------ auth */

let appStarted = false;

function startApp() {
  if (appStarted) return;
  appStarted = true;
  connectWs();
  loadRecentEvents();
  loadCameras().then(() => {
    // Seed status badges from the API snapshot before WS updates arrive.
    api("/api/cameras").then((cameras) => {
      for (const cam of cameras) {
        if (!cameraStatuses.has(cam.id)) {
          cameraStatuses.set(cam.id, {
            name: cam.name, status: cam.status, detail: cam.status_detail,
          });
        }
      }
      renderBadges();
    }).catch(() => {});
  });
  populateCameraFilter();
}

function showAuth(which) {
  $("#auth-overlay").classList.remove("hidden");
  $("#setup-form").classList.toggle("hidden", which !== "setup");
  $("#login-form").classList.toggle("hidden", which !== "login");
  const focus = which === "setup" ? "#setup-user" : "#login-user";
  $(focus)?.focus();
}

function hideAuth() {
  $("#auth-overlay").classList.add("hidden");
}

// Reflect the signed-in account in the header and Settings.
function applyAccount(status) {
  const account = $("#account");
  const panel = $("#account-panel");
  if (status.auth_required && status.username) {
    $("#account-user").textContent = status.username;
    account.classList.remove("hidden");
    if (panel) panel.hidden = false;
  } else {
    account.classList.add("hidden");
    if (panel) panel.hidden = true;
  }
}

async function initAuth() {
  let status;
  try {
    status = await api("/api/auth/status");
  } catch (_) {
    // If the status check fails, fall back to starting the app so a network
    // blip never locks the user out of an unauthenticated install.
    startApp();
    return;
  }
  if (status.needs_setup) {
    showAuth("setup");
  } else if (status.auth_required && !status.authenticated) {
    showAuth("login");
  } else {
    applyAccount(status);
    hideAuth();
    startApp();
  }
}

// A 401 from any API call means the session expired mid-use: show the login.
window.addEventListener("anpr-unauthorized", () => showAuth("login"));

$("#setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#setup-msg");
  const user = $("#setup-user").value.trim();
  const pass = $("#setup-pass").value;
  const pass2 = $("#setup-pass2").value;
  msg.textContent = "";
  if (pass !== pass2) { msg.textContent = "Passwords do not match."; return; }
  try {
    await api("/api/auth/setup", {
      method: "POST",
      body: JSON.stringify({ username: user, password: pass }),
    });
    hideAuth();
    await initAuth();
  } catch (err) { msg.textContent = err.message; }
});

$("#setup-skip").addEventListener("click", async () => {
  const msg = $("#setup-msg");
  try {
    await api("/api/auth/setup", {
      method: "POST", body: JSON.stringify({ skip: true }),
    });
    hideAuth();
    startApp();
  } catch (err) { msg.textContent = err.message; }
});

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#login-msg");
  msg.textContent = "";
  try {
    await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: $("#login-user").value.trim(),
        password: $("#login-pass").value,
      }),
    });
    $("#login-pass").value = "";
    hideAuth();
    await initAuth();
    // If the app was already running (session expired then re-login), just
    // refresh; otherwise startApp() inside initAuth boots it.
    location.reload();
  } catch (err) { msg.textContent = err.message; }
});

$("#logout-btn").addEventListener("click", async () => {
  try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {}
  location.reload();
});

$("#password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#pw-msg");
  const oldp = $("#pw-old").value;
  const newp = $("#pw-new").value;
  const newp2 = $("#pw-new2").value;
  msg.className = "muted";
  if (newp !== newp2) {
    msg.textContent = "New passwords do not match."; msg.className = "msg-err"; return;
  }
  msg.textContent = "Saving…";
  try {
    await api("/api/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ old_password: oldp, new_password: newp }),
    });
    msg.textContent = "✓ Password changed."; msg.className = "msg-ok";
    $("#password-form").reset();
  } catch (err) { msg.textContent = "✗ " + err.message; msg.className = "msg-err"; }
});

/* ----------------------------------------------------------------- init */

initAuth();
