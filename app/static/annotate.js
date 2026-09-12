import {collectDirectoryEntries, classifyFiles, hiddenPath} from '/import-files.mjs?v=dicom-import-1';
import {PLANE_AXES, zoomTransform, wheelPixels, nextFocus, isTextEditing, UndoQueue} from '/viewer-controls.mjs?v=multiplane-1';
import {workflowState, STATUS_LABELS} from '/annotation-workflow.mjs?v=nodules-1';
import {jobNoduleLabel, selectedNodule, proposalIsCurrent, previewLabel} from '/nodule-controls.mjs?v=nodules-1';
import {DIAMETER_PRESETS, createEditSettings, normalizeDiameter, strokeSpec, sphereSectionRadius} from '/mask-editing.mjs?v=mask-tools-1';

const $ = (id) => document.getElementById(id);
const embedded = new URLSearchParams(location.search).get('embedded') === '1';
if (embedded) document.body.classList.add('embedded-viewer');
function clinicalCopy() {
  if (!embedded) return;
  const replace = value => value.replace(/\b[Nn]odules?\b/g, word => word[0] === 'N' ? word.replace('Nodule', 'Lesion') : word.replace('nodule', 'lesion'))
    .replaceAll('Self-selected test identity, not a clinical signature.', 'Authenticated demo account; not a clinical signature.')
    .replaceAll('Local test attribution only.', 'Authenticated revision attribution.')
    .replaceAll('CT + all lesions', 'Image + all lesions')
    .replaceAll('CT image', 'image').replaceAll('CT selected', 'image selected');
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (!node.parentElement.closest('script,style,pre') && node.nodeValue.trim()) node.nodeValue = replace(node.nodeValue);
  }
  for (const element of document.querySelectorAll('[title],[aria-label]')) for (const attribute of ['title','aria-label']) {
    if (element.hasAttribute(attribute)) element.setAttribute(attribute, replace(element.getAttribute(attribute)));
  }
}
const state = {page: "cases", cases: [], id: null, meta: null, volume: null, mask: null,
  proposal: null, preview: false, box: null, tool: "crosshair", cross: [0, 0, 0],
  model: null, job: null, busy: false, loading: false, overlay: true, undo: [], importEntries: [],
  window: {width: 1500, level: -600}, epoch: 0, activePlane: "axial", focusedPlane: null,
  editSettings: createEditSettings(), brushHover: null, identity: null,
  selectedNodule: null, jobs: [], runningJobId: null, noduleBoxes: {}, noduleColors: {},
  hiddenNodules: new Set(), displayVersion: 0};
let identityId = 'reviewer';
try {identityId = localStorage.getItem('lung-test-identity') || identityId;} catch {}
const views = [...document.querySelectorAll(".viewport")].map((node) => ({
  node, plane: node.dataset.plane, canvas: node.querySelector("canvas"),
  slider: node.querySelector("input"), zoom: 1, pan: [0, 0], transform: null,
  bitmap: document.createElement("canvas"), drag: null,
}));
let toastTimer, pollTimer, renderPending = false;
let pendingPoint = null, pointTimer, pointSave = null;
const undoQueue = new UndoQueue(() => !state.busy && !state.loading && !pendingPoint && !views.some((v) => v.drag), undo);

async function api(path, options = {}) {
  const response = await fetch(path, {cache: "no-store", ...options,
    headers: {"X-Annotation-Actor": identityId, ...options.headers}});
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try { const data = await response.json(); detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail); } catch {}
    const error = new Error(detail); error.status = response.status; throw error;
  }
  return response;
}
async function json(path, options) { return (await api(path, options)).json(); }
function post(path, payload) { return json(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)}); }
function base(id = state.id) { return `/api/lung/studies/${id}`; }
function notify(message, error = false) {
  clearTimeout(toastTimer); $("toast").textContent = message; $("toast").classList.toggle("error", error);
  $("toast").hidden = false; toastTimer = setTimeout(() => $("toast").hidden = true, error ? 11000 : 4500);
}
function handle(error) { notify(error.message || String(error), true); }
function task(fn) { return (...args) => { Promise.resolve().then(() => fn(...args)).catch(handle); }; }

function showPage(page) {
  state.page = page;
  document.querySelectorAll(".page").forEach((el) => el.hidden = el.id !== `page-${page}`);
  document.querySelectorAll(".tabs [data-page]").forEach((el) => el.classList.toggle("selected", el.dataset.page === page));
  if (page === "annotate") requestRender();
}
document.querySelectorAll("[data-page]").forEach((el) => el.addEventListener("click", () => {
  showPage(el.dataset.page); if (el.dataset.page === "cases") task(loadCases)();
}));

async function loadCases() {
  state.cases = (await json("/api/worklist")).items;
  $("case-count").textContent = state.cases.length;
  renderCases();
}
function renderCases() {
  const query = $("case-search").value.toLowerCase();
  const filter = $("case-status").value;
  const rows = state.cases.filter((row) => [row.patient_id, row.description, row.accession_number].join(" ").toLowerCase().includes(query)
    && (!filter || row.modality === "CT" && row.annotation_status === filter));
  $("status-summary").replaceChildren();
  for (const [status, label] of Object.entries(STATUS_LABELS)) {
    const badge = document.createElement("span"); badge.className = `status-badge annotation-${status}`;
    badge.textContent = `${label} ${state.cases.filter((row) => row.modality === 'CT' && row.annotation_status === status).length}`;
    $("status-summary").append(badge);
  }
  $("cases-body").replaceChildren();
  $("empty-cases").hidden = state.cases.length > 0;
  $("no-matching-cases").hidden = !state.cases.length || !!rows.length;
  for (const row of rows) {
    const tr = document.createElement("tr");
    const status = row.modality === "CT" ? row.annotation_status || "new" : row.status;
    for (const value of [row.patient_id || row.study_id.slice(0, 8), row.description || row.body_part || "Imaging study", row.modality || "CT", STATUS_LABELS[status] || status.replaceAll("_", " ")]) {
      const td = document.createElement("td"); td.textContent = value; tr.append(td);
    }
    tr.lastChild.className = `annotation-${status}`;
    if (row.annotation_event?.actor) tr.lastChild.title = `${row.annotation_event.actor.name} / ${row.annotation_event.actor.role.replaceAll('_', ' ')}`;
    const td = document.createElement("td"); const button = document.createElement("button");
    button.className = "open-case"; button.textContent = row.modality === "CT" ? "Open case" : "Open PACS";
    button.addEventListener("click", task(() => row.modality === "CT" ? openCase(row.study_id) : location.assign(`/index.html?study_id=${row.study_id}`)));
    td.append(button);
    if (row.modality === "CT" && ["completed", "approved"].includes(status)) {
      const exportButton = document.createElement("button"); exportButton.className = "open-case";
      exportButton.textContent = "Export"; exportButton.addEventListener("click", task(() => openExport(row.study_id)));
      td.append(exportButton);
    }
    tr.append(td); $("cases-body").append(tr);
  }
}
$("case-search").addEventListener("input", renderCases);
$("case-status").addEventListener("change", renderCases);
$("refresh-cases").addEventListener("click", task(loadCases));

async function openCase(id) {
  if (state.busy || state.loading) return notify("Wait for the current edit or load to finish.");
  await flushPendingPoint();
  undoQueue.clear();
  const epoch = ++state.epoch; state.loading = true; clearTimeout(pollTimer);
  state.id = id; state.meta = null; state.volume = null; state.mask = null; state.proposal = null;
  state.preview = false; state.box = null; state.job = null; state.undo = []; state.brushHover = null;
  state.jobs = []; state.runningJobId = null; state.selectedNodule = null;
  state.hiddenNodules = new Set(); state.noduleBoxes = {}; renderNodules();
  $("viewer-empty").hidden = false; $("viewer-empty").querySelector("h2").textContent = "Loading CT volume...";
  showPage("annotate"); updateControls();
  try {
    let meta = await json(base());
    const response = await api(`${base()}/volume`);
    const volume = meta.transfer_dtype === 'float32-le' ? new Float32Array(await response.arrayBuffer()) : new Int16Array(await response.arrayBuffer());
    if (epoch !== state.epoch) return;
    if (volume.length !== meta.size_xyz.reduce((a, b) => a * b, 1)) throw new Error("CT transfer size mismatch");
    if (meta.annotation_status === "new") {
      try {await post(`${base()}/open`, {version: meta.version});}
      catch (error) {if (error.status !== 409) throw error;}
      meta = await json(base());
    }
    state.meta = meta; state.volume = volume; state.jobs = meta.jobs;
    syncCaseStatus();
    state.cross = meta.size_xyz.map((n) => Math.floor(n / 2));
    await loadMask();
    const row = state.cases.find((item) => item.study_id === id);
    const name = row?.patient_id || id.slice(0, 8);
    $("study-title").textContent = name; $("case-tab").textContent = name;
    $("study-subtitle").textContent = `${meta.size_xyz[2]} slices / ${meta.modality || 'CT'}`;
    if (meta.modality === 'MR') {
      state.window = {width: Math.max(1, meta.display_range[1] - meta.display_range[0]), level: (meta.display_range[0] + meta.display_range[1]) / 2};
    } else if (!row?.description?.includes('lung')) state.window = {width: 400, level: 40};
    $('window-width').value = state.window.width; $('window-level').value = state.window.level;
    $('window-preset').value = meta.modality === 'MR' ? 'custom' : row?.description?.includes('lung') ? 'lung' : 'soft';
    $("geometry-info").textContent = `${meta.size_xyz.join(" x ")} voxels. Spacing: ${meta.spacing_xyz.map((n) => n.toFixed(2)).join(" / ")} mm. Native acquisition grid; oblique acquisitions are not resampled into anatomical MPR.`;
    views.forEach((view) => {view.zoom = 1; view.pan = [0, 0];});
    reconcileNodules();
    const center = activeNodule()?.centroid_xyz;
    if (center) state.cross = center.map(Math.round);
    const head = meta.history.find((rev) => rev.id === meta.revision_id);
    if (head) state.undo = [head.parent_id];
    $("viewer-empty").hidden = true;
    history.replaceState({}, "", `/viewer?study_id=${id}${embedded ? '&embedded=1' : ''}`);
    state.runningJobId = state.jobs.find((job) => ['queued', 'running'].includes(job.status))?.id || null;
    if (state.runningJobId) schedulePoll();
  } catch (error) {
    $("viewer-empty").querySelector("h2").textContent = "Unable to open this CT";
    state.volume = null; throw error;
  } finally { state.loading = false; updateControls(); renderHistory(); requestRender(); }
  if (proposalIsCurrent(state.job, state.meta, state.selectedNodule) && !state.meta?.confirmation) await showProposal();
}

async function loadMask() {
  const mask = state.meta.revision_id
    ? new Uint8Array(await (await api(`${base()}/mask?revision_id=${state.meta.revision_id}`)).arrayBuffer())
    : new Uint8Array(state.volume.length);
  if (state.volume && mask.length !== state.volume.length) throw new Error("Mask transfer size mismatch");
  state.mask = mask;
}
async function reloadDraft() {
  state.meta = await json(base());
  state.jobs = state.meta.jobs;
  const running = state.jobs.find((job) => ['queued', 'running'].includes(job.status));
  if (running && state.runningJobId !== running.id) {state.runningJobId = running.id; schedulePoll();}
  await loadMask(); reconcileNodules(); syncCaseStatus(); renderHistory(); updateControls(); requestRender();
}
function syncCaseStatus() {
  const row = state.cases.find((item) => item.study_id === state.id);
  if (row) {row.annotation_status = state.meta.annotation_status; row.annotation_event = state.meta.review_history[0];}
  renderCases();
}
function validPrompt() {return !!state.box && state.box.box_xyxy[2] > state.box.box_xyxy[0] && state.box.box_xyxy[3] > state.box.box_xyxy[1];}
function workflow() {return workflowState({meta: state.meta, job: state.job, jobs: state.jobs, preview: state.preview,
  hasBox: validPrompt(), modelAvailable: state.model?.available, noduleLabel: state.selectedNodule});}

function activeNodule() {return selectedNodule(state.meta, state.selectedNodule);}
function selectJob() {
  const previousId = state.job?.id;
  state.job = state.jobs.find((job) => jobNoduleLabel(job) === state.selectedNodule) || null;
  if (previousId !== state.job?.id) {state.proposal = null; state.preview = false;}
}
function nodulePrompt() {
  if (state.noduleBoxes[state.selectedNodule]) return state.noduleBoxes[state.selectedNodule];
  const prompt = state.job?.prompt;
  return Array.isArray(prompt?.box_xyxy) ? {plane: prompt.plane || 'axial', frame_index: prompt.frame_index, box_xyxy: [...prompt.box_xyxy]} : null;
}
function reconcileNodules() {
  const previous = state.selectedNodule;
  if (!activeNodule()) state.selectedNodule = state.meta?.nodules[0]?.label ?? null;
  selectJob();
  if (previous !== state.selectedNodule || !state.box) state.box = nodulePrompt();
  state.noduleColors = Object.fromEntries((state.meta?.nodules || []).map((nodule) => [nodule.label, nodule.color]));
  state.displayVersion++; renderNodules();
}
function selectNodule(label, jump = true) {
  if (state.busy || state.loading || pendingPoint || pointSave || views.some((view) => view.drag)) return;
  if (!selectedNodule(state.meta, label)) return;
  if (state.selectedNodule !== null) state.noduleBoxes[state.selectedNodule] = state.box;
  state.selectedNodule = label; state.preview = false; state.proposal = null; state.brushHover = null;
  state.hiddenNodules.delete(label); selectJob(); state.box = nodulePrompt();
  if (jump && activeNodule().centroid_xyz) state.cross = activeNodule().centroid_xyz.map(Math.round);
  state.displayVersion++; renderNodules(); updateControls(); requestRender();
}
function renderNodules() {
  const nodules = state.meta?.nodules || [];
  $("nodule-list").replaceChildren(); $("nodule-select").replaceChildren();
  $("nodule-count").textContent = nodules.length;
  for (const nodule of nodules) {
    const option = document.createElement('option'); option.value = nodule.label; option.textContent = nodule.id;
    $("nodule-select").append(option);
    const row = document.createElement('div'); row.className = 'nodule-row';
    row.classList.toggle('selected', nodule.label === state.selectedNodule);
    row.style.setProperty('--nodule-color', `rgb(${nodule.color.join(',')})`);
    const button = document.createElement('button'); button.dataset.selectNodule = nodule.label;
    button.setAttribute('aria-pressed', nodule.label === state.selectedNodule);
    button.title = `Select and locate ${nodule.id}`;
    const name = document.createElement('strong'); name.textContent = `${nodule.id} / ${nodule.name}`;
    const detail = document.createElement('span'); detail.textContent = nodule.voxel_count ? `${nodule.volume_ml.toFixed(3)} mL` : 'Empty / annotation needed';
    button.append(name, detail); button.addEventListener('click', () => selectNodule(nodule.label));
    const visibility = document.createElement('button'); visibility.className = 'nodule-visibility';
    visibility.dataset.noduleVisibility = nodule.label;
    visibility.textContent = state.hiddenNodules.has(nodule.label) ? 'Show' : 'Hide';
    visibility.setAttribute('aria-label', `${visibility.textContent} ${nodule.id}`);
    visibility.addEventListener('click', () => {
      if (state.hiddenNodules.has(nodule.label)) state.hiddenNodules.delete(nodule.label); else state.hiddenNodules.add(nodule.label);
      state.displayVersion++; renderNodules(); updateControls(); requestRender();
    });
    row.append(button, visibility); $("nodule-list").append(row);
  }
  if (!nodules.length) {const option = document.createElement('option'); option.value = ''; option.textContent = 'None'; $("nodule-select").append(option);}
  const add = document.createElement('option'); add.value = '__new'; add.textContent = 'Add nodule...'; $("nodule-select").append(add);
  $("nodule-select").value = state.selectedNodule ?? '';
  $("nodules-summary").textContent = state.meta ? `${nodules.length} nodule(s) / ${nodules.reduce((sum, nodule) => sum + nodule.volume_ml, 0).toFixed(3)} mL total. Select a row to locate; only the selected nodule is edited.` : 'Select a CT case.';
}
async function addNodule() {
  if ($("add-nodule").disabled) return;
  const result = await mutate(`${base()}/nodules`, {});
  if (result) {selectNodule(result.nodule_label, false); setTool(state.model?.available ? 'box' : 'brush'); notify(`N${result.nodule_label} added. Draw a box or paint its mask; other nodules are preserved.`);}
}
$("add-nodule").addEventListener('click', task(addNodule));
$("nodule-select").addEventListener('change', task(async () => {
  const value = $("nodule-select").value; $("nodule-select").value = state.selectedNodule ?? '';
  if (value === '__new') await addNodule(); else selectNodule(Number(value));
}));

function axes(plane) { return PLANE_AXES[plane]; }
function selectView(plane) {
  state.activePlane = plane;
  views.forEach((v) => v.node.classList.toggle("selected-view", v.plane === plane));
  for (const [id, label] of [["zoom-in", "Zoom in"], ["zoom-out", "Zoom out"]]) $(id).title = `${label}: ${plane} view`;
}
function focusView(plane) {
  state.brushHover = null;
  state.focusedPlane = plane;
  $("viewports").classList.toggle("single", !!plane);
  views.forEach((v) => v.node.classList.toggle("focused-view", v.plane === plane));
  $("toggle-mpr").classList.toggle("active", !plane);
  $("toggle-mpr").textContent = plane ? "All planes" : "Focus view";
  if (plane) selectView(plane);
  requestRender();
}
function zoomView(view, factor, event) {
  if (!state.volume || view.drag) return;
  state.brushHover = null;
  const rect = view.canvas.getBoundingClientRect();
  const anchor = event ? [event.clientX - rect.left - rect.width / 2, event.clientY - rect.top - rect.height / 2] : [0, 0];
  Object.assign(view, zoomTransform(view.zoom, view.pan, factor, anchor));
  selectView(view.plane); requestRender();
}
function displaySigns(plane) {
  const [a, b] = axes(plane), d = state.meta.direction;
  // Radiological display for standard axis-aligned scans; preserve native voxel indexing.
  return [d[a * 3 + a] < 0 ? -1 : 1, (d[b * 3 + b] < 0 ? -1 : 1) * (plane === "axial" ? 1 : -1)];
}
function offset(x, y, z) { return (z * state.meta.size_xyz[1] + y) * state.meta.size_xyz[0] + x; }
function point3(plane, u, v) { const result = [...state.cross]; const [a, b] = axes(plane); result[a] = u; result[b] = v; return result; }
function requestRender() {
  if (renderPending) return;
  renderPending = true;
  requestAnimationFrame(() => { renderPending = false; if (state.page === "annotate") views.forEach(renderView); });
}
function orientation(axis, sign) {
  const d = state.meta.direction;
  const vector = [d[axis], d[3 + axis], d[6 + axis]].map((v) => v * sign);
  return vector.map((v, i) => ({v, i})).filter(({v}) => Math.abs(v) > .2).sort((a, b) => Math.abs(b.v) - Math.abs(a.v))
    .map(({v, i}) => (v >= 0 ? ["L", "P", "S"] : ["R", "A", "I"])[i]).join("");
}
function renderView(view) {
  const rect = view.canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const dpr = Math.min(devicePixelRatio || 1, 2);
  view.canvas.width = Math.round(rect.width * dpr); view.canvas.height = Math.round(rect.height * dpr);
  const ctx = view.canvas.getContext("2d"); ctx.scale(dpr, dpr);
  ctx.fillStyle = "#050809"; ctx.fillRect(0, 0, rect.width, rect.height);
  if (!state.volume || !state.meta) return;
  const [a, b, normal] = axes(view.plane);
  const width = state.meta.size_xyz[a], height = state.meta.size_xyz[b];
  const [signX, signY] = displaySigns(view.plane);
  const displayX = (u) => signX < 0 ? width - 1 - u : u;
  const displayY = (v) => signY < 0 ? height - 1 - v : v;
  const slice = state.cross[normal];
  view.slider.max = state.meta.size_xyz[normal] - 1; view.slider.value = slice;
  const source = view.bitmap;
  const overlay = state.preview ? state.proposal : state.mask;
  const targetLabel = state.preview ? jobNoduleLabel(state.job) : null;
  const colors = {...state.noduleColors, 256: [255, 102, 94], 257: [245, 186, 85]};
  const opacity = state.overlay ? Number($("overlay-opacity").value) : 0;
  const low = state.window.level - state.window.width / 2;
  const rasterKey = [state.volume, state.mask, overlay, opacity, low, state.window.width, slice,
    state.preview, targetLabel, state.selectedNodule, state.displayVersion, signX, signY];
  // Zoom, pan and crosshair changes reuse the raster instead of windowing every voxel again.
  if (!view.rasterKey || rasterKey.some((value, i) => value !== view.rasterKey[i])) {
    source.width = width; source.height = height;
    const bctx = source.getContext("2d"); const pixels = bctx.createImageData(width, height);
    for (let v = 0; v < height; v++) for (let u = 0; u < width; u++) {
      let x, y, z; const nativeU = displayX(u), nativeV = displayY(v);
      if (normal === 2) {x = nativeU; y = nativeV; z = slice;} else if (normal === 1) {x = nativeU; y = slice; z = nativeV;} else {x = slice; y = nativeU; z = nativeV;}
      const index = offset(x, y, z), p = (v * width + u) * 4;
      const value = Math.max(0, Math.min(255, (state.volume[index] - low) * 255 / state.window.width));
      const label = previewLabel(state.mask?.[index] || 0, state.preview && overlay?.[index], targetLabel);
      const color = colors[label] || [104, 237, 182];
      const alpha = !label || state.hiddenNodules.has(label) ? 0 : opacity * (label === state.selectedNodule || label > 255 ? 1 : .65);
      for (let c = 0; c < 3; c++) pixels.data[p + c] = value * (1 - alpha) + color[c] * alpha;
      pixels.data[p + 3] = 255;
    }
    bctx.putImageData(pixels, 0, 0);
    view.rasterKey = rasterKey;
  }
  const sx = state.meta.spacing_xyz[a], sy = state.meta.spacing_xyz[b];
  const scale = Math.max(.01, Math.min((rect.width - 36) / (width * sx), (rect.height - 90) / (height * sy))) * view.zoom;
  const dw = width * sx * scale, dh = height * sy * scale;
  const left = (rect.width - dw) / 2 + view.pan[0], top = (rect.height - dh) / 2 + view.pan[1];
  view.transform = {left, top, px: dw / width, py: dh / height, width, height, signX, signY};
  ctx.imageSmoothingEnabled = true; ctx.drawImage(source, left, top, dw, dh);
  const cx = left + (displayX(state.cross[a]) + .5) * dw / width, cy = top + (displayY(state.cross[b]) + .5) * dh / height;
  ctx.save(); ctx.beginPath(); ctx.rect(left, top, dw, dh); ctx.clip();
  ctx.strokeStyle = "#77bbd57a"; ctx.lineWidth = .7; ctx.beginPath();
  ctx.moveTo(cx, top); ctx.lineTo(cx, cy - 9); ctx.moveTo(cx, cy + 9); ctx.lineTo(cx, top + dh);
  ctx.moveTo(left, cy); ctx.lineTo(cx - 9, cy); ctx.moveTo(cx + 9, cy); ctx.lineTo(left + dw, cy); ctx.stroke();
  if (state.box && view.plane === (state.box.plane || "axial") && state.box.frame_index === slice) {
    const [x0, y0, x1, y1] = state.box.box_xyxy;
    ctx.strokeStyle = "#f0be63"; ctx.lineWidth = 1.3; ctx.setLineDash([5, 4]);
    ctx.strokeRect(left + (displayX(x0) + .5) * dw / width, top + (displayY(y0) + .5) * dh / height, (x1 - x0) * dw / width * signX, (y1 - y0) * dh / height * signY);
    ctx.setLineDash([]);
  }
  const strokePreview = view.drag?.points ? view.drag : pendingPoint?.view === view ? pendingPoint.drag : null;
  if (strokePreview) {
    ctx.strokeStyle = strokePreview.tool === "erase" ? "#f48776aa" : `rgba(${(state.noduleColors[strokePreview.noduleLabel] || [104, 237, 182]).join(',')},.65)`;
    ctx.lineWidth = strokePreview.spec.radius_mm * 2 * scale; ctx.lineCap = "round"; ctx.lineJoin = "round";
    ctx.beginPath(); strokePreview.points.forEach(([u, v], i) => {const fn = i ? "lineTo" : "moveTo"; ctx[fn](left + (displayX(u) + .5) * dw / width, top + (displayY(v) + .5) * dh / height);});
    if (strokePreview.points.length === 1) {const [u, v] = strokePreview.points[0]; ctx.lineTo(left + (displayX(u) + .5) * dw / width + .01, top + (displayY(v) + .5) * dh / height);}
    ctx.stroke();
  }
  if (state.brushHover && isEditTool() && !state.preview && !state.meta.confirmation && !state.busy) {
    const spec = views.find((v) => v.drag?.points)?.drag.spec || strokeSpec(state.tool, state.editSettings);
    const center = state.brushHover.center;
    const distance = (slice - center[normal]) * state.meta.spacing_xyz[normal];
    const radius = spec.spherical ? sphereSectionRadius(spec.radius_mm, distance) :
      state.brushHover.plane === view.plane && distance === 0 ? spec.radius_mm : null;
    if (radius !== null && radius > 0) {
      const x = left + (displayX(center[a]) + .5) * dw / width;
      const y = top + (displayY(center[b]) + .5) * dh / height;
      ctx.strokeStyle = spec.erase ? "#ff9d8c" : `rgb(${(activeNodule()?.color || [104, 237, 182]).join(',')})`; ctx.lineWidth = 1.2;
      ctx.setLineDash(spec.spherical ? [4, 3] : []);
      ctx.beginPath(); ctx.arc(x, y, radius * scale, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
      if (state.brushHover.plane === view.plane) {
        ctx.fillStyle = ctx.strokeStyle; ctx.font = "10px monospace";
        ctx.fillText(`${spec.spherical ? "Sphere" : "Circle"} ${spec.radius_mm * 2} mm`, x + radius * scale + 6, y - 6);
      }
    }
  }
  ctx.restore();
  const scaleMm = dw / scale > 100 ? 50 : 10;
  ctx.strokeStyle = "#819294"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(rect.width - 22 - scaleMm * scale, rect.height - 55); ctx.lineTo(rect.width - 22, rect.height - 55); ctx.stroke();
  ctx.font = "9px monospace"; ctx.fillStyle = "#90a0a1"; ctx.textAlign = "right"; ctx.fillText(`${scaleMm} mm`, rect.width - 22, rect.height - 61);
  view.node.querySelector(".plane-position").textContent = `${slice + 1} / ${state.meta.size_xyz[normal]}`;
  view.node.querySelector(".view-info").textContent = `${width} x ${height}   ${sx.toFixed(2)} x ${sy.toFixed(2)} mm   ${Math.round(view.zoom * 100)}%`;
  for (const [pos, axis, sign] of [["left", a, -signX], ["right", a, signX], ["top", b, -signY], ["bottom", b, signY]]) view.node.querySelector(`.orient.${pos}`).textContent = orientation(axis, sign);
}
function pixel(view, event, clamp = false) {
  if (!view.transform) return null;
  const rect = view.canvas.getBoundingClientRect(), t = view.transform;
  let u = (event.clientX - rect.left - t.left) / t.px - .5, v = (event.clientY - rect.top - t.top) / t.py - .5;
  if (!clamp && (u < -.5 || v < -.5 || u >= t.width - .5 || v >= t.height - .5)) return null;
  u = Math.max(0, Math.min(t.width - 1, u)); v = Math.max(0, Math.min(t.height - 1, v));
  return [t.signX < 0 ? t.width - 1 - u : u, t.signY < 0 ? t.height - 1 - v : v];
}
function setTool(tool) {
  if (!activeNodule() && ["box", "brush", "erase"].includes(tool)) return notify("Add a nodule before annotating.");
  if (state.meta?.confirmation && ["box", "brush", "erase"].includes(tool)) return notify("Reopen the completed or approved case before editing.");
  if (["box", "brush", "erase"].includes(tool) && state.hiddenNodules.delete(state.selectedNodule)) {state.displayVersion++; renderNodules();}
  state.tool = tool; state.brushHover = null;
  document.querySelectorAll("[data-tool]").forEach((el) => {el.classList.toggle("active", el.dataset.tool === tool); el.setAttribute("aria-pressed", el.dataset.tool === tool);});
  views.forEach((view) => view.canvas.style.cursor = tool === "pan" ? "grab" : tool === "zoom" ? "zoom-in" : "crosshair");
  $("interaction-hint").textContent = {box: "Box one lesion in any plane. Set the slice range in the inspector.", brush: "Paint: add voxels | Pinch: zoom | Cmd/Ctrl+Z: undo", erase: "Paint: remove voxels | Pinch: zoom | Cmd/Ctrl+Z: undo", pan: "Drag: pan | Pinch: zoom | Double-click: focus/restore", zoom: "Drag up/down or scroll: zoom | +/-: zoom selected view | Fit: reset", crosshair: "Double-click: focus/restore | Pinch: zoom | Scroll: slice | Cmd/Ctrl+Z: undo"}[tool];
  updateControls(); requestRender();
}
function isEditTool() {return ["brush", "erase"].includes(state.tool);}
function updateEditControls(ready) {
  $("edit-options").hidden = !isEditTool();
  if (!isEditTool()) return;
  const selected = state.editSettings[state.tool];
  $("edit-options").classList.toggle("erasing", state.tool === "erase");
  $("edit-tool-name").textContent = state.tool === "erase" ? "Erase" : "Brush";
  $("edit-diameter").value = selected.diameter; $("edit-size-slider").value = selected.diameter;
  $("edit-scope").textContent = state.preview ? "Accept the preview before editing" : selected.shape === "sphere" ? "3D: affects adjacent slices" : "2D: current slice only";
  for (const button of document.querySelectorAll("[data-edit-shape]")) {
    const active = button.dataset.editShape === selected.shape;
    button.classList.toggle("active", active); button.setAttribute("aria-pressed", active);
  }
  for (const button of document.querySelectorAll("[data-edit-diameter]")) {
    const active = Number(button.dataset.editDiameter) === selected.diameter;
    button.classList.toggle("active", active); button.setAttribute("aria-pressed", active);
  }
  for (const el of $("edit-options").querySelectorAll("button,input")) el.disabled = !ready || !!state.meta?.confirmation || !activeNodule();
}
function changeEditSetting(key, value) {
  if (!isEditTool() || !state.volume || state.busy || state.loading || pendingPoint || pointSave || views.some((v) => v.drag) || state.meta?.confirmation) return;
  state.editSettings[state.tool][key] = value;
  updateControls(); requestRender();
}
for (const diameter of DIAMETER_PRESETS) {
  const button = document.createElement("button"); button.textContent = diameter;
  button.dataset.editDiameter = diameter; button.setAttribute("aria-label", `${diameter} mm diameter`);
  button.addEventListener("click", () => changeEditSetting("diameter", diameter)); $("size-presets").append(button);
}
for (const button of document.querySelectorAll("[data-edit-shape]")) button.addEventListener("click", () => changeEditSetting("shape", button.dataset.editShape));
$("edit-size-slider").addEventListener("input", () => changeEditSetting("diameter", normalizeDiameter($("edit-size-slider").value)));
$("edit-diameter").addEventListener("change", () => changeEditSetting("diameter", normalizeDiameter($("edit-diameter").value)));
document.querySelectorAll("[data-tool]").forEach((el) => el.addEventListener("click", () => setTool(el.dataset.tool)));
for (const view of views) {
  view.node.addEventListener("dblclick", (event) => {
    if (event.target.closest("input") || state.loading || state.busy) return;
    event.preventDefault(); cancelPendingPoint();
    focusView(nextFocus(state.focusedPlane, view.plane));
  });
  view.node.addEventListener("pointerdown", () => selectView(view.plane));
  view.slider.addEventListener("input", () => { if (!state.meta || state.busy || view.drag || pendingPoint) return; state.brushHover = null; selectView(view.plane); state.cross[axes(view.plane)[2]] = Number(view.slider.value); requestRender(); });
  view.canvas.addEventListener("wheel", (event) => {
    event.preventDefault(); if (!state.meta || state.busy || view.drag || pendingPoint || view.gestureScale) return;
    state.brushHover = null;
    selectView(view.plane);
    const delta = wheelPixels(event, view.canvas.clientHeight);
    if (event.ctrlKey || event.metaKey || event.shiftKey || state.tool === "zoom") zoomView(view, Math.exp(-Math.max(-100, Math.min(100, delta)) * .01), event);
    else {
      if (!view.wheelTime || event.timeStamp - view.wheelTime > 160 || Math.sign(delta) !== Math.sign(view.wheelRemainder)) view.wheelRemainder = 0;
      view.wheelTime = event.timeStamp; view.wheelRemainder += delta;
      const steps = Math.trunc(view.wheelRemainder / 24);
      view.wheelRemainder -= steps * 24;
      const axis = axes(view.plane)[2]; state.cross[axis] = Math.max(0, Math.min(state.meta.size_xyz[axis] - 1, state.cross[axis] + steps));
    }
    requestRender();
  }, {passive: false});
  view.canvas.addEventListener("gesturestart", (event) => {event.preventDefault(); view.gestureScale = 1;}, {passive: false});
  view.canvas.addEventListener("gesturechange", (event) => {
    event.preventDefault();
    if (view.gestureScale && event.scale > 0) zoomView(view, event.scale / view.gestureScale, event);
    view.gestureScale = event.scale;
  }, {passive: false});
  view.canvas.addEventListener("gestureend", (event) => {event.preventDefault(); view.gestureScale = null;}, {passive: false});
  view.canvas.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const previousClick = view.lastClick;
    view.lastClick = {time: event.timeStamp, x: event.clientX, y: event.clientY};
    // PointerEvent.detail is zero in some browsers; identify the second click before a dot can be saved.
    if (event.detail >= 2 || (previousClick && event.timeStamp - previousClick.time < 600 &&
      Math.hypot(event.clientX - previousClick.x, event.clientY - previousClick.y) < 5)) {cancelPendingPoint(); return;}
    if (!state.volume || state.busy || state.loading || event.button !== 0) return;
    if (state.meta?.confirmation && ["box", "brush", "erase"].includes(state.tool)) return notify("Reopen the case before editing.");
    if (!activeNodule() && ["box", "brush", "erase"].includes(state.tool)) return;
    if (pendingPoint) task(flushPendingPoint)();
    const point = pixel(view, event, ["pan", "zoom"].includes(state.tool)); if (!point) return;
    if (["brush", "erase"].includes(state.tool) && state.preview) return notify("Choose Use result & edit before painting, or Undo to return to your saved mask.");
    view.canvas.setPointerCapture(event.pointerId);
    view.drag = {start: point, screen: [event.clientX, event.clientY], pan: [...view.pan], zoom: view.zoom, tool: state.tool,
      noduleLabel: state.selectedNodule, previousBox: state.box, slice: state.cross[axes(view.plane)[2]]};
    if (isEditTool()) {view.drag.points = [point]; view.drag.spec = strokeSpec(state.tool, state.editSettings); state.brushHover = {plane: view.plane, center: point3(view.plane, ...point)};}
    if (state.tool === "crosshair") state.cross = point3(view.plane, ...point.map(Math.round));
    if (state.tool === "box") state.box = {plane: view.plane, frame_index: view.drag.slice, box_xyxy: [...point, ...point]};
    requestRender(); updateControls();
  });
  view.canvas.addEventListener("pointermove", (event) => {
    if (state.volume && isEditTool() && !state.preview && !state.meta?.confirmation && !state.busy) {
      const hover = pixel(view, event);
      state.brushHover = hover ? {plane: view.plane, center: point3(view.plane, ...hover)} : null;
      requestRender();
    }
    if (!view.drag) return;
    const point = pixel(view, event, true), drag = view.drag;
    if (drag.tool === "pan") view.pan = drag.pan.map((v, i) => v + [event.clientX, event.clientY][i] - drag.screen[i]);
    if (drag.tool === "zoom") Object.assign(view, zoomTransform(drag.zoom, drag.pan, Math.exp((drag.screen[1] - event.clientY) * .01)));
    if (drag.tool === "crosshair") state.cross = point3(view.plane, ...point.map(Math.round));
    if (drag.tool === "box") state.box.box_xyxy = [Math.min(drag.start[0], point[0]), Math.min(drag.start[1], point[1]), Math.max(drag.start[0], point[0]), Math.max(drag.start[1], point[1])];
    if (drag.points && drag.points.length < 500) {
      const last = drag.points.at(-1);
      if (Math.hypot(point[0] - last[0], point[1] - last[1]) >= .4) drag.points.push(point);
    }
    requestRender();
  });
  view.canvas.addEventListener("pointerup", task(async (event) => {
    const drag = view.drag; if (!drag) return;
    if (view.canvas.hasPointerCapture(event.pointerId)) view.canvas.releasePointerCapture(event.pointerId);
    view.drag = null;
    if (Math.hypot(event.clientX - drag.screen[0], event.clientY - drag.screen[1]) >= 3) view.lastClick = null;
    if (drag.tool === "box" && Math.hypot(event.clientX - drag.screen[0], event.clientY - drag.screen[1]) < 3) state.box = drag.previousBox;
    if (drag.points) {
      const payload = {plane: view.plane, slice_index: drag.slice, points: drag.points,
        nodule_label: drag.noduleLabel, ...drag.spec};
      if (drag.points.length === 1) {
        pendingPoint = {view, drag, payload, url: `${base()}/strokes`};
        // Defer point-only edits so a double-click focuses the view without painting a dot.
        pointTimer = setTimeout(task(flushPendingPoint), 600);
      } else {await pointSave; await mutate(`${base()}/strokes`, payload);}
    }
    if (drag.tool === 'crosshair') {
      const label = state.mask[offset(...state.cross)];
      if (label && label !== state.selectedNodule && !state.hiddenNodules.has(label)) selectNodule(label, false);
    }
    updateControls(); requestRender();
    task(() => undoQueue.drain())();
  }));
  view.canvas.addEventListener("pointercancel", () => {view.drag = null; state.brushHover = null; updateControls(); requestRender();});
  view.canvas.addEventListener("pointerleave", () => {state.brushHover = null; requestRender();});
  new ResizeObserver(requestRender).observe(view.canvas);
}

function cancelPendingPoint() {clearTimeout(pointTimer); pendingPoint = null; updateControls(); requestRender();}
async function flushPendingPoint() {
  if (!pendingPoint) return pointSave;
  const edit = pendingPoint; cancelPendingPoint();
  const previous = pointSave;
  const save = (async () => {await previous; await mutate(edit.url, edit.payload);})();
  pointSave = save;
  try {await save;} finally {if (pointSave === save) pointSave = null; updateControls(); task(() => undoQueue.drain())();}
}

async function mutate(url, payload, recordUndo = true) {
  if (state.busy || !state.meta) return false;
  state.busy = true; updateControls(); const previous = state.meta.revision_id;
  try {
    const result = await post(url, {version: state.meta.version, ...payload});
    if (recordUndo) state.undo.push(previous);
    state.preview = false; await reloadDraft();
    if (result.protected_other_nodules) notify(`Other nodules were protected. The stroke only changed N${payload.nodule_label}.`);
    return result;
  } catch (error) {
    if (error.status === 409) {state.undo = []; await reloadDraft();}
    throw error;
  } finally {state.busy = false; updateControls(); requestRender(); task(() => undoQueue.drain())();}
}
async function undo() {
  if (state.busy) return;
  if (state.meta?.confirmation) {
    if (!workflow().canUndoReview) return notify("Only a reviewer can undo approval.");
    await changeReview(workflow().approved ? "undo-approval" : "reopen"); return;
  }
  if (state.preview) {state.preview = false; updateControls(); requestRender(); notify("Preview closed. The saved mask is unchanged."); return;}
  if (!state.undo.length) return;
  const target = state.undo.at(-1);
  const label = state.meta.history.find((revision) => revision.id === state.meta.revision_id)?.details.nodule_label;
  await mutate(target ? `${base()}/restore/${target}` : `${base()}/restore-initial`, {}, false);
  if (label && selectedNodule(state.meta, label)) selectNodule(label, true);
  state.undo.pop(); updateControls();
}
function requestUndo() {
  if (pendingPoint) {cancelPendingPoint(); updateControls(); return undoQueue.drain();}
  return undoQueue.request();
}
$("undo").addEventListener("click", task(requestUndo));
$("workflow-undo").addEventListener("click", task(requestUndo));

function updateControls() {
  const ready = !!state.volume && !state.busy && !state.loading && !pendingPoint && !pointSave && !views.some((v) => v.drag);
  const flow = workflow(), {running, confirmed} = flow;
  const readOnly = state.meta?.permissions?.annotate === false && !confirmed;
  const nodule = activeNodule();
  $("add-nodule").disabled = !ready || confirmed || readOnly || !state.meta?.can_add_nodule;
  $("nodule-select").disabled = !ready;
  $("nodule-select").querySelector('option[value="__new"]')?.toggleAttribute('disabled', $("add-nodule").disabled);
  for (const button of document.querySelectorAll('[data-select-nodule], [data-nodule-visibility]')) button.disabled = !ready;
  $("test-identity").disabled = state.busy || state.loading || !!pendingPoint || !!pointSave || views.some((v) => v.drag);
  updateEditControls(ready);
  for (const button of document.querySelectorAll("[data-delete-mask]")) button.disabled = !ready || confirmed || readOnly || state.preview || !nodule;
  $("delete-nodule").textContent = nodule ? `Delete ${nodule.id}` : 'Delete nodule mask';
  $("delete-mask-note").textContent = confirmed ? "Reopen the case before deleting a nodule." : state.preview ? "Return to the saved mask before deleting it." : "Deletes only the selected nodule. Others are preserved. Undo restores it.";
  const validBox = validPrompt();
  $("run-model").disabled = !ready || !state.model?.available || !(state.model?.modalities || ['CT']).includes(state.meta?.modality) || running || !validBox || confirmed || readOnly || !nodule;
  $('model-slice-radius').disabled = running || confirmed || readOnly;
  if (state.model?.id === 'litemedsam-onnx') {
    const radius = Number($('model-slice-radius').value);
    const normal = axes(state.box?.plane || state.activePlane)[2];
    const frame = state.box?.frame_index ?? state.cross[normal];
    const last = (state.meta?.size_xyz[normal] || 1) - 1;
    $('model-range-note').textContent = state.meta ? `Slices ${Math.max(0, frame-radius)+1}-${Math.min(last, frame+radius)+1} in the ${state.box?.plane || state.activePlane} plane. The same box is used on each slice; inspect all results.` : '';
  }
  $("run-model").hidden = confirmed || state.preview;
  const runningJob = state.jobs.find((job) => ['queued', 'running'].includes(job.status));
  $("run-model").textContent = running ? `Segmenting N${jobNoduleLabel(runningJob)}...` : "Run segmentation";
  $("run-model").classList.toggle("primary", flow.step === 1);
  $("draw-lesion").hidden = confirmed || flow.step !== 1;
  $("draw-lesion").disabled = !ready || !nodule;
  $("draw-lesion").textContent = validBox ? "Redraw box" : "Draw lesion box";
  $("confirm-case").hidden = confirmed;
  $("confirm-case").disabled = !ready || !flow.canConfirm;
  $("confirm-case").classList.toggle("primary", flow.canConfirm);
  $("approve-case").hidden = !flow.canApprove; $("approve-case").disabled = !ready;
  $("reopen-case").hidden = !flow.canReopen; $("reopen-case").disabled = !ready;
  $("workflow-hint").textContent = state.loading ? "Loading CT..." : !state.meta ? "Select a CT case to begin." : state.busy ? "Saving or loading mask. Please wait..." : flow.hint;
  $("workflow-bar").classList.toggle("confirmed", confirmed);
  document.querySelectorAll("[data-step]").forEach((el) => {el.classList.toggle("complete", Number(el.dataset.step) < flow.step); if (Number(el.dataset.step) === flow.step) el.setAttribute("aria-current", "step"); else el.removeAttribute("aria-current");});
  $("undo").disabled = !state.volume || state.loading || (!state.undo.length && !state.busy && !pendingPoint && !views.some((v) => v.drag) && !state.preview && !confirmed);
  if (confirmed && !flow.canUndoReview) $("undo").disabled = true;
  $("workflow-undo").disabled = $("undo").disabled;
  $("workflow-undo").textContent = flow.approved ? "Undo approval" : flow.completed ? "Undo completion" : state.preview ? "Close preview" : "Undo";
  for (const el of document.querySelectorAll('[data-tool="brush"], [data-tool="erase"], [data-tool="box"]')) el.disabled = confirmed || readOnly || !nodule;
  for (const id of ["zoom-in", "zoom-out"]) $(id).disabled = !ready;
  $("export-mask").disabled = !ready || !confirmed;
  $("export-mask").title = confirmed ? "Download the completed or approved revision" : "Mark the task Completed to export the case";
  $("download-dicom").disabled = !ready || !state.meta?.original_dicom_available;
  $("dicom-source-note").textContent = !state.meta
    ? state.loading ? "Loading source metadata..." : "Select a case to check source DICOM availability."
    : state.meta.original_dicom_available
    ? "Original DICOM instances and metadata. Edited mask excluded."
    : "Source DICOM unavailable. Reimport DICOM files to enable download; NIfTI-only imports have no DICOM source.";
  $("save-state").textContent = state.loading ? "Loading CT..." : state.busy ? "Processing..." : state.meta ? `${STATUS_LABELS[state.meta.annotation_status]} / v${state.meta.version}` : "No case open";
  $("confirmation-note").hidden = !confirmed;
  $("confirmation-note").textContent = confirmed ? `${STATUS_LABELS[state.meta.annotation_status]}. Last action: ${state.meta.confirmation.action.replaceAll('_', ' ')} by ${state.meta.confirmation.actor?.name || 'legacy unattributed user'} / ${new Date(state.meta.confirmation.created_at).toLocaleString()}. Mask ${state.meta.revision_id.slice(0, 8)}. Self-selected test identity, not a clinical signature.` : "";
  document.querySelector(".target-card").classList.toggle("preview", state.preview);
  $("revision-tag").textContent = `v${state.meta?.version || 0}`;
  const volume = state.preview ? state.job?.result?.volume_ml : nodule?.volume_ml;
  $("selected-nodule-name").textContent = nodule ? `${nodule.id} / ${nodule.name}` : 'No nodule selected';
  document.querySelector('.target-card .mask-dot').style.background = state.preview ? '#f0be63' : `rgb(${(nodule?.color || [104,237,182]).join(',')})`;
  $("mask-volume").textContent = volume === undefined ? "No saved mask" : `${volume.toFixed(3)} mL / ${state.preview ? "AI preview, unsaved" : "selected nodule"}`;
  $("prompt-info").textContent = validBox ? `${nodule?.id || ''} / ${state.box.plane || 'axial'} slice ${state.box.frame_index + 1} / box ${state.box.box_xyxy.map((n) => Math.round(n)).join(", ")} px` : "No bounding box defined";
  $("job-panel").hidden = !state.job;
  $("accept-proposal").hidden = !state.preview || state.job?.status !== "succeeded" || confirmed;
  if (state.job) {
    const status = state.job.status;
    $("job-status").textContent = {queued: "Queued", running: "Segmenting (CPU)", succeeded: "Segmentation complete", cancelled: "Job cancelled", failed: "Inference failed"}[status] || status;
    const thisRunning = ['queued', 'running'].includes(status);
    $("job-progress").hidden = !thisRunning; $("cancel-job").hidden = !thisRunning;
    $("preview-proposal").hidden = status !== "succeeded" || confirmed;
    $("preview-proposal").textContent = state.preview ? "Show saved mask" : "Preview model mask";
    $("preview-proposal").disabled = !ready;
    $("accept-proposal").disabled = !ready || !flow.freshProposal || confirmed;
    $("accept-proposal").textContent = !flow.freshProposal ? "Result is outdated" : "Use result & edit";
    const result = state.job.result;
    $("job-detail").textContent = thisRunning ? `CPU inference for N${jobNoduleLabel(state.job)}. ${state.job.progress ? `${state.job.progress.completed} / ${state.job.progress.total} slices ${state.job.progress.phase === 'encoding' ? 'encoded' : 'segmented'}.` : 'Loading model.'} You can work on other nodules.` :
      status === "succeeded" ? `${result.elapsed_seconds.toFixed(0)} seconds / ${result.volume_ml.toFixed(3)} mL. ${result.touches_crop_boundary ? "WARNING: mask reaches a processed boundary; inspect and extend the range if needed. " : ""}${result.empty_prediction ? "The model returned an empty mask. " : ""}Preview replaces only N${jobNoduleLabel(state.job)}; other colors are saved nodules. Red overlap must be resolved.` : state.job.error || "No draft was changed.";
    $("model-provenance").textContent = result ? JSON.stringify(result, null, 2) : "";
  }
  if (undoQueue.pending && !undoQueue.running && undoQueue.canRun()) task(() => undoQueue.drain())();
  clinicalCopy();
}
function renderHistory() {
  const rows = state.meta?.history || []; $("history-count").textContent = rows.length; $("history-list").replaceChildren();
  for (const row of rows) {
    const item = document.createElement("div"); item.className = "history-item";
    const text = document.createElement("span"); text.textContent = `${row.kind.replaceAll("-", " ")}${row.details.nodule_label ? ' N' + row.details.nodule_label : ''} / ${row.details.volume_ml.toFixed(3)} mL total`;
    const detail = document.createElement("small"); detail.textContent = `${row.id.slice(0, 8)} / ${row.details.actor?.name || 'Legacy / unattributed'} / ${new Date(row.created_at).toLocaleTimeString()}`; text.append(detail);
    const button = document.createElement("button"); button.textContent = row.id === state.meta.revision_id ? "Current" : "Restore";
    button.disabled = row.id === state.meta.revision_id || !!state.meta.confirmation;
    button.addEventListener("click", task(async () => { if (confirm("Restore ALL nodules from this case snapshot? The current snapshot remains in history.")) await mutate(`${base()}/restore/${row.id}`, {}); }));
    item.append(text, button); $("history-list").append(item);
  }
  $("review-history").replaceChildren();
  for (const event of state.meta?.review_history || []) {
    const item = document.createElement("p"); item.className = "review-event";
    item.textContent = `${event.action.replaceAll('_', ' ')} / ${event.actor?.name || 'Legacy / unattributed'}${event.actor ? ' (' + event.actor.role.replaceAll('_', ' ') + ')' : ''} / v${event.version} / ${event.revision_id ? 'mask ' + event.revision_id.slice(0, 8) : 'no mask yet'} / ${new Date(event.created_at).toLocaleString()}`;
    $("review-history").append(item);
  }
}

$("draw-lesion").addEventListener("click", () => {setTool("box"); notify("Drag a box around one lesion in the axial, coronal or sagittal image.");});
$("run-model").addEventListener("click", task(startSegmentation));
async function startSegmentation() {
  if (state.busy || $("run-model").disabled) return;
  state.busy = true; updateControls();
  try {
    state.job = await post(`${base()}/jobs`, {...state.box, version: state.meta.version, nodule_label: state.selectedNodule,
      slice_radius: Number($('model-slice-radius').value), window_level: state.meta.modality === 'CT' ? state.window.level : 40,
      window_width: state.meta.modality === 'CT' ? state.window.width : 400});
    state.jobs = [state.job, ...state.jobs]; state.meta.jobs = state.jobs;
    state.runningJobId = state.job.id;
    state.noduleBoxes[state.selectedNodule] = state.box;
    state.proposal = null; state.preview = false; schedulePoll(); notify(`Segmentation submitted for N${state.selectedNodule}.`);
  } catch (error) {if (error.status === 409) await reloadDraft(); throw error;}
  finally {state.busy = false; updateControls();}
}
function schedulePoll() {clearTimeout(pollTimer); pollTimer = setTimeout(task(pollJob), 1800);}
async function pollJob() {
  const id = state.id, jobId = state.runningJobId;
  if (!jobId) return;
  try {
    const job = await json(`${base(id)}/jobs/${jobId}`);
    if (state.id !== id || state.runningJobId !== jobId) return;
    state.jobs = state.jobs.map((item) => item.id === jobId ? job : item); state.meta.jobs = state.jobs;
    if (state.job?.id === jobId) state.job = job;
    updateControls();
    if (["queued", "running"].includes(job.status)) schedulePoll();
    else if (job.status === "succeeded") {
      if (state.busy || pendingPoint || pointSave || views.some((v) => v.drag)) {schedulePoll(); return;}
      state.runningJobId = null;
      if (state.job?.id === jobId && proposalIsCurrent(job, state.meta, state.selectedNodule) && !state.meta.confirmation) {
        await showProposal(); notify(`N${jobNoduleLabel(job)} result ready. Inspect the preview, then choose Use result & edit.`);
      } else notify(`N${jobNoduleLabel(job)} segmentation finished. Select that nodule to inspect the result; edited targets require a new run.`);
    } else state.runningJobId = null;
  } catch (error) { if (state.id === id) { schedulePoll(); notify(`Job status unavailable: ${error.message}`, true); } }
}
$("cancel-job").addEventListener("click", task(async () => {
  const id = state.id, jobId = state.job.id;
  const job = await post(`${base(id)}/jobs/${jobId}/cancel`, {});
  if (id === state.id) {
    state.jobs = state.jobs.map((item) => item.id === jobId ? job : item); state.meta.jobs = state.jobs;
    if (jobId === state.job?.id) state.job = job;
    if (state.runningJobId === jobId) {state.runningJobId = null; clearTimeout(pollTimer);}
    updateControls();
  }
}));
$("preview-proposal").addEventListener("click", task(() => showProposal(!state.preview)));
async function showProposal(show = true) {
  if (state.busy || state.loading || pendingPoint || pointSave || views.some((v) => v.drag)) return;
  if (!state.job || jobNoduleLabel(state.job) !== state.selectedNodule || !activeNodule()) return;
  state.busy = true; updateControls();
  try {
    if (!state.proposal) {
      const buffer = await (await api(`${base()}/mask?job_id=${state.job.id}`)).arrayBuffer();
      if (buffer.byteLength !== state.volume.length) throw new Error("Proposal transfer size mismatch");
      state.proposal = new Uint8Array(buffer);
    }
    state.preview = show;
    if (state.preview) {
      state.overlay = true; $("toggle-overlay").classList.add("active");
      const bounds = state.job.prompt.box_xyxy;
      const plane = state.job.prompt.plane || "axial", [a, b, normal] = axes(plane);
      state.cross[a] = Math.round((bounds[0] + bounds[2]) / 2); state.cross[b] = Math.round((bounds[1] + bounds[3]) / 2); state.cross[normal] = state.job.prompt.frame_index;
      selectView(plane); if (state.focusedPlane) focusView(plane);
    }
  } finally {state.busy = false; updateControls(); requestRender();}
}
$("accept-proposal").addEventListener("click", task(async () => {
  if ($("accept-proposal").disabled) return;
  if (await mutate(`${base()}/jobs/${state.job.id}/accept`, {})) {
    setTool("brush"); notify(`N${state.selectedNodule} updated. Other nodules are unchanged. Brush / Erase to correct; Cmd/Ctrl+Z to undo.`);
  }
}));

let deleteTarget = null;
for (const button of document.querySelectorAll("[data-delete-mask]")) button.addEventListener("click", () => {
  if (button.disabled) return;
  deleteTarget = {studyId: state.id, version: state.meta.version, nodule_label: state.selectedNodule};
  const nodule = activeNodule();
  $("delete-mask-summary").textContent = `${$("study-title").textContent} / ${nodule.id} / ${nodule.volume_ml.toFixed(3)} mL. All other nodule masks will be preserved.`;
  $("delete-mask-dialog").showModal();
});
$("delete-mask-close").addEventListener("click", () => $("delete-mask-dialog").close());
$("delete-mask-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!deleteTarget || deleteTarget.studyId !== state.id || state.busy) return;
  task(async () => {
    $("delete-mask-submit").disabled = true;
    try {
      if (await mutate(`${base()}/delete-mask`, {version: deleteTarget.version, nodule_label: deleteTarget.nodule_label})) {
        state.brushHover = null;
        if (!activeNodule()) setTool('crosshair');
        notify(`N${deleteTarget.nodule_label} deleted. Other nodules and CT are unchanged. Cmd/Ctrl+Z restores it.`);
      }
    } finally {$("delete-mask-submit").disabled = false; $("delete-mask-dialog").close();}
  })();
});

let confirmationTarget = null;
function openReview(action) {
  if (action === 'approve' ? !workflow().canApprove : $("confirm-case").disabled) return;
  confirmationTarget = {studyId: state.id, version: state.meta.version, revision_id: state.meta.revision_id, action};
  const current = state.meta.history.find((row) => row.id === state.meta.revision_id);
  $("confirm-summary").textContent = `${$("study-title").textContent} / ${state.meta.nodules.length} nodules / ${current.details.volume_ml.toFixed(3)} mL total. ${state.meta.nodules.map((nodule) => `${nodule.id}: ${nodule.volume_ml.toFixed(3)} mL`).join('; ')}. Acting as ${state.identity.name} (${state.identity.role.replaceAll('_', ' ')}).`;
  $("review-heading").textContent = action === 'approve' ? 'Approve completed annotation' : 'Complete annotation';
  $("confirm-submit").textContent = action === 'approve' ? 'Approve case' : 'Mark completed';
  $("review-explanation").textContent = action === 'approve'
    ? 'Approval records the selected reviewer against this exact revision. In this test version, a reviewer may approve their own annotation. This is not independent review or a clinical signature.'
    : 'Completion covers ALL nodules in this saved case snapshot and enables export. Reopen or Undo completion to edit again. Reviewer approval is a separate step.';
  $("mask-reviewed").checked = false; $("confirm-dialog").showModal();
}
$("confirm-case").addEventListener("click", () => openReview('complete'));
$("approve-case").addEventListener("click", () => openReview('approve'));
$("confirm-close").addEventListener("click", () => $("confirm-dialog").close());
$("confirm-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!$("confirm-form").reportValidity() || state.busy || confirmationTarget?.studyId !== state.id) return;
  task(async () => {
    $("confirm-submit").disabled = true;
    try {await changeReview(confirmationTarget.action, {...confirmationTarget, reviewed: $("mask-reviewed").checked});}
    finally {$("confirm-submit").disabled = false; $("confirm-dialog").close();}
  })();
});
$("reopen-case").addEventListener("click", task(() => changeReview("reopen")));
async function changeReview(action, payload = {version: state.meta?.version}) {
  if (state.busy || state.loading || pendingPoint || pointSave || views.some((v) => v.drag)) return;
  state.busy = true; updateControls();
  try {
    await post(`${base()}/${action}`, payload);
    state.meta = await json(base()); state.preview = false;
    state.jobs = state.meta.jobs; reconcileNodules();
    if (action !== "reopen") setTool("crosshair");
    syncCaseStatus(); renderHistory();
    if (embedded) parent.postMessage({type: 'oncometra:annotation-updated', studyId: state.id}, location.origin);
    notify({complete: "Task completed. Export is available; reviewer approval is a separate step.",
      approve: "Task approved. Export this revision or Undo approval to return to Completed.",
      "undo-approval": "Approval undone. The mask is unchanged; status is Completed.",
      reopen: "Task reopened: In progress. The saved mask is unchanged and editable."}[action]);
  } catch (error) {if (error.status === 409) {state.undo = []; await reloadDraft();} throw error;}
  finally {state.busy = false; updateControls(); requestRender(); task(() => undoQueue.drain())();}
}
$("overlay-opacity").addEventListener("input", requestRender);
$('model-slice-radius').addEventListener('change', updateControls);
$("reset-view").addEventListener("click", () => {views.forEach((v) => {v.zoom = 1; v.pan = [0, 0];}); requestRender();});
$("toggle-mpr").addEventListener("click", () => focusView(state.focusedPlane ? null : state.activePlane));
$("zoom-in").addEventListener("click", () => zoomView(views.find((v) => v.plane === state.activePlane), 1.25));
$("zoom-out").addEventListener("click", () => zoomView(views.find((v) => v.plane === state.activePlane), 1 / 1.25));
function toggleOverlay() {state.overlay = !state.overlay; $("toggle-overlay").classList.toggle("active", state.overlay); requestRender();}
$("toggle-overlay").addEventListener("click", toggleOverlay);
$("toggle-inspector").addEventListener("click", () => {const hidden = $("annotation-layout").classList.toggle("no-inspector"); $("toggle-inspector").setAttribute("aria-expanded", !hidden); requestRender();});
if (innerWidth <= 760) {$("annotation-layout").classList.add("no-inspector"); $("toggle-inspector").setAttribute("aria-expanded", "false");}
$("export-mask").addEventListener("click", task(() => openExport(state.id)));
let exportTarget = null;
async function openExport(id) {
  const meta = await json(base(id));
  if (!["completed", "approved"].includes(meta.annotation_status)) throw new Error("Mark this task Completed before exporting. Refresh the worklist if its status changed.");
  exportTarget = meta;
  const row = state.cases.find((item) => item.study_id === id);
  $("export-summary").textContent = `${row?.patient_id || id.slice(0, 8)} / ${STATUS_LABELS[meta.annotation_status]} / v${meta.version} / ${meta.nodules.map((nodule) => nodule.id).join(', ')}. Exports include all nodules, even those hidden in the viewer.`;
  $("export-source-note").textContent = meta.original_dicom_available ? "Original DICOM will be included in the case package." : "NIfTI-only case: no source DICOM is available. The case package still includes CT and masks.";
  $("export-feedback").textContent = "";
  for (const link of document.querySelectorAll('[data-export]')) {
    link.hidden = link.dataset.export === 'dicom' && !meta.original_dicom_available;
    link.href = `${base(id)}/exports/${meta.revision_id}?version=${meta.version}&format=${link.dataset.export}`;
    link.setAttribute('download', '');
  }
  $("export-dialog").showModal();
}
$("export-close").addEventListener("click", () => $("export-dialog").close());
for (const link of document.querySelectorAll('[data-export]')) link.addEventListener('click', (event) => {
  event.preventDefault();
  task(async () => {
    const target = exportTarget, href = link.href;
    try {
      const current = await json(base(target.study_id));
      if (current.version !== target.version || current.revision_id !== target.revision_id || !['completed', 'approved'].includes(current.annotation_status)) throw new Error('This case changed. Close this dialog and reopen Export for the current revision.');
      const download = document.createElement('a'); download.href = href; download.download = ''; document.body.append(download); download.click(); download.remove();
      $("export-feedback").textContent = 'Download requested. Case packages may take a moment to prepare. Check your browser downloads.';
    } catch (error) {$("export-feedback").textContent = error.message; throw error;}
  })();
});
$("download-dicom").addEventListener("click", () => {
  if (state.meta?.original_dicom_available) location.assign(`/api/studies/${state.id}/dicom/download`);
});
$("window-preset").addEventListener("change", () => {
  const preset = {lung: [1500, -600], soft: [400, 50], bone: [1800, 400]}[$("window-preset").value];
  if (preset) {state.window = {width: preset[0], level: preset[1]}; $("window-width").value = preset[0]; $("window-level").value = preset[1]; requestRender();}
});
for (const id of ["window-width", "window-level"]) $(id).addEventListener("change", () => {
  state.window = {width: Math.max(1, Math.min(8000, Number($("window-width").value) || 1)), level: Math.max(-3000, Math.min(3000, Number($("window-level").value) || 0))};
  $("window-preset").value = "custom"; requestRender();
});
document.addEventListener("keydown", (event) => {
  if (isTextEditing(event.target) || document.querySelector("dialog[open]") || state.page !== "annotate") return;
  const key = event.key.toLowerCase();
  if ((event.ctrlKey || event.metaKey) && key === "z" && !event.shiftKey) {event.preventDefault(); task(requestUndo)(); return;}
  if (key === "escape" && state.focusedPlane) {event.preventDefault(); focusView(null); return;}
  if (views.some((v) => v.drag)) return;
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  const tool = {v: "crosshair", b: "box", p: "brush", e: "erase", h: "pan", z: "zoom"}[key];
  if (key === "+" || key === "=" || key === "-") {event.preventDefault(); zoomView(views.find((v) => v.plane === state.activePlane), key === "-" ? .8 : 1.25);}
  if (tool) setTool(tool); if (key === "m") toggleOverlay();
});

function openImport() {$("import-dialog").showModal();}
$("import-open").addEventListener("click", openImport); $("empty-import").addEventListener("click", openImport);
$("import-close").addEventListener("click", () => {if (!state.loading) $("import-dialog").close();});
let selectionEpoch = 0, dragDepth = 0;
function selectImport(entries) {
  state.importEntries = []; $("import-submit").disabled = true; $("deidentified").checked = false;
  const filtered = entries.filter(({path}) => !hiddenPath(path));
  const {kind, totalBytes} = classifyFiles(filtered);
  state.importEntries = filtered;
  $("selected-files").textContent = `${filtered.length} file(s) / ${(totalBytes / 1024 / 1024).toFixed(1)} MB / ${kind === 'dicom' ? 'DICOM series import' : 'NIfTI volume'}\n${filtered.slice(0, 4).map(({path}) => path).join('\n')}${filtered.length > 4 ? '\n...' : ''}`;
  $("case-id-field").hidden = kind !== 'nifti'; $("import-status").textContent = '';
  $("import-submit").disabled = false;
}
for (const id of ['scan-file', 'scan-folder']) $(id).addEventListener('change', task(() => {
  if (state.loading) return;
  ++selectionEpoch;
  selectImport([...$(id).files].map((file) => ({file, path: file.webkitRelativePath || file.name})));
}));
function filesDragged(event) {return [...(event.dataTransfer?.types || [])].includes('Files');}
document.addEventListener('dragenter', (event) => {
  if (!filesDragged(event)) return; event.preventDefault(); dragDepth++;
  $("drop-overlay").hidden = $("import-dialog").open;
  $("dicom-dropzone").classList.add('drag-active');
});
document.addEventListener('dragover', (event) => {
  if (!filesDragged(event)) return; event.preventDefault(); event.dataTransfer.dropEffect = state.loading ? 'none' : 'copy';
});
document.addEventListener('dragleave', (event) => {
  if (!filesDragged(event)) return; event.preventDefault(); dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) {$("drop-overlay").hidden = true; $("dicom-dropzone").classList.remove('drag-active');}
});
document.addEventListener('drop', (event) => {
  if (!filesDragged(event)) return;
  event.preventDefault(); dragDepth = 0; $("drop-overlay").hidden = true; $("dicom-dropzone").classList.remove('drag-active');
  if (state.loading || state.busy) return notify('Wait for the current import or edit to finish.');
  // Capture handles synchronously; DataTransfer is protected after the drop event returns.
  const items = [...event.dataTransfer.items].filter((item) => item.kind === 'file');
  const roots = items.map((item) => item.webkitGetAsEntry?.());
  const fallback = [...event.dataTransfer.files].map((file) => ({file, path: file.name}));
  const epoch = ++selectionEpoch;
  openImport(); state.importEntries = []; $("import-submit").disabled = true; $("deidentified").checked = false;
  $("selected-files").textContent = 'Reading folder contents...';
  task(async () => {
    try {
      const entries = roots.length && roots.every(Boolean) ? await collectDirectoryEntries(roots, (count) => {
        if (epoch === selectionEpoch) $("selected-files").textContent = `Reading folder: ${count} file(s)...`;
      }) : fallback;
      if (epoch === selectionEpoch) selectImport(entries);
    } catch (error) {
      if (epoch === selectionEpoch) $("selected-files").textContent = `${error.message} Try the Choose folder control if your browser does not support folder drops.`;
      throw error;
    }
  })();
});
function uploadFiles(path, data) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest(); xhr.open('POST', path); xhr.responseType = 'json';
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      const percent = Math.round(event.loaded / event.total * 100); $("upload-progress").value = percent;
      $("import-status").textContent = percent < 100 ? `Uploading ${percent}%...` : 'Upload complete. Extracting metadata, preserving DICOM originals, and converting series...';
    };
    xhr.onload = () => xhr.status >= 200 && xhr.status < 300 ? resolve(xhr.response) : reject(new Error(typeof xhr.response?.detail === 'string' ? xhr.response.detail : `Import failed (${xhr.status}).`));
    xhr.onerror = () => reject(new Error('Connection lost during import. Refresh the worklist before retrying to avoid duplicates.'));
    xhr.send(data);
  });
}
async function importSelected() {
  if (state.busy || state.loading || !$("import-form").reportValidity()) return;
  const {kind} = classifyFiles(state.importEntries);
  const data = new FormData();
  if (kind === 'dicom') state.importEntries.forEach(({file, path}) => data.append("dicom_files", file, path));
  else {const file = state.importEntries[0].file; data.append("baseline_scan", file); data.append("patient_id", $("import-case-id").value.trim() || `CT-${Date.now().toString().slice(-7)}`); data.append("modality", "CT"); data.append("description", "Research CT / " + file.name);}
  $("import-submit").disabled = true; $("import-close").disabled = true; $("import-status").textContent = 'Starting upload...'; state.loading = true;
  $("upload-progress").hidden = false; $("upload-progress").value = 0;
  try {
    const result = await uploadFiles(kind === 'dicom' ? "/api/dicom/bulk" : "/api/studies", data);
    await loadCases();
    const id = result.study_id || result.first_study_id;
    $("import-dialog").close(); state.loading = false;
    const row = state.cases.find((item) => item.study_id === id);
    if (id && row?.modality === 'CT') await openCase(id); else showPage("cases");
    notify(result.created_count ? `Imported ${result.created_count} DICOM series. Originals are available as ZIP downloads.` : 'CT imported.');
  } catch (error) {$("import-status").textContent = error.message; throw error;}
  finally {state.loading = false; $("import-submit").disabled = !state.importEntries.length; $("import-close").disabled = false; $("upload-progress").hidden = true; updateControls();}
}
$("import-form").addEventListener("submit", (event) => {event.preventDefault(); task(importSelected)();});
// Prevent Escape from dismissing a dialog while the upload request is in flight.
$("import-dialog").addEventListener("cancel", (event) => {if (state.loading) event.preventDefault();});

window.addEventListener("beforeunload", (event) => {if (state.busy || state.loading || pendingPoint || pointSave) {event.preventDefault(); event.returnValue = "";}});

async function init() {
  let identities;
  try {identities = await json('/api/lung/identities');}
  catch (error) {
    if (error.status !== 400) throw error;
    identityId = 'reviewer'; identities = await json('/api/lung/identities');
  }
  state.identity = identities.current;
  identityId = state.identity.id;
  $("test-identity").replaceChildren();
  for (const identity of identities.identities) {
    const option = document.createElement('option'); option.value = identity.id;
    option.textContent = `${identity.name} / ${identity.role.replaceAll('_', ' ')}`;
    $("test-identity").append(option);
  }
  $("test-identity").value = identityId;
  $("test-identity").title = identities.notice;
  clinicalCopy();
  state.model = await json("/api/lung/model");
  $('model-name').textContent = state.model.name;
  $('model-scope').textContent = state.model.scope;
  $('model-terms').textContent = `${state.model.license} ${state.model.validation}`;
  $('model-range-control').hidden = state.model.id !== 'litemedsam-onnx';
  $('model-preprocessing').textContent = state.model.id === 'litemedsam-onnx' ? 'CT: current display window in HU. MRI: 0.5-99.5 percentile clipping and slice normalization. Not whole-volume detection.' : 'Input requirement: CT intensities in Hounsfield units (HU), not normalized image values.';
  $("connection").textContent = state.model.available ? "Model available (CPU)" : "Model unavailable";
  $("model-availability").textContent = state.model.available ? `${state.model.name} ready. Inspect every proposal before accepting.` : "Model not configured. Manual annotation is available.";
  await loadCases();
  const initial = new URLSearchParams(location.search).get("study_id");
  if (initial) await openCase(initial); selectView(state.activePlane); setTool(state.tool); updateControls();
}
$("test-identity").addEventListener('change', task(async () => {
  const previousId = identityId, previousActor = state.identity;
  if (state.busy || state.loading || pendingPoint || pointSave || views.some((view) => view.drag)) {
    $("test-identity").value = identityId; return;
  }
  state.busy = true; identityId = $("test-identity").value; updateControls();
  try {
    state.identity = (await json('/api/lung/identities')).current;
    if (state.meta) {await reloadDraft(); state.undo = [];}
    try {localStorage.setItem('lung-test-identity', identityId);} catch {}
    notify(`Test identity: ${state.identity.name} / ${state.identity.role.replaceAll('_', ' ')}. Not an authenticated login.`);
  } catch (error) {
    identityId = previousId; state.identity = previousActor; $("test-identity").value = previousId; throw error;
  } finally {state.busy = false; updateControls();}
}));
task(init)();
