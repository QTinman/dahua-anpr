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

function showDetail(ev) {
  currentDetailId = ev.id;
  $("#detail-plate").textContent = ev.plate || "?";
  const fields = {
    Time: fmtTime(ev.received_at),
    Camera: ev.camera_name,
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
      <div class="cam-meta">Events: ${esc(cam.event_codes)}</div>
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

/* ----------------------------------------------------------------- init */

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
