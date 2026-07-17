const WINDOW_PRESETS = {
  "ct-soft": { center: 50, width: 500 },
  "ct-lung": { center: -600, width: 1500 },
  "ct-bone": { center: 400, width: 1800 },
  mr: { center: "", width: "" },
  xray: { center: "", width: "", invert: true },
  mammo: { center: "", width: "", invert: false },
  us: { center: "", width: "" },
};

const SERIES = {
  baseline: {
    label: "Primary",
    role: "primary",
    slice: "baselineSlice",
    sliceLabel: "baselineSliceLabel",
    canvas: "baselineView",
    card: "baselineViewerCard",
    save: "saveBaseline",
  },
  followup: {
    label: "Comparison",
    role: "comparison",
    slice: "followupSlice",
    sliceLabel: "followupSliceLabel",
    canvas: "followupView",
    card: "followupViewerCard",
    save: "saveFollowup",
  },
};

const SERIES_KEYS = Object.keys(SERIES);

const state = {
  currentStudyId: new URLSearchParams(window.location.search).get("study_id"),
  workspace: null,
  worklist: [],
  models: [],
  trainingRuns: [],
  schedule: [],
  billing: [],
  measurements: [],
  lastMeasurement: null,
  integrations: null,
  speech: null,
  viewer: {
    plane: "axial",
    windowCenter: "50",
    windowWidth: "500",
    invert: false,
    zoom: 1,
    tool: "pan",
    layout: "single",
    activeSeries: "baseline",
    cineTimer: null,
  },
  views: {
    baseline: { slice: 0, max: 1, imageB64: null, shape: [512, 512], spacing: [1, 1], baseImg: null },
    followup: { slice: 0, max: 1, imageB64: null, shape: [512, 512], spacing: [1, 1], baseImg: null },
  },
  painting: { active: false, timepoint: null },
  measuring: { active: false, timepoint: null, start: null, preview: null },
  windowing: { active: false, timepoint: null, start: null, center: 50, width: 500 },
  dictation: { recorder: null, chunks: [], stream: null, recognition: null, mode: null, transcript: "" },
  activeEditor: null,
};

function seriesLabel(timepoint) {
  return SERIES[timepoint]?.label || timepoint;
}

function resetSeriesSlices() {
  SERIES_KEYS.forEach((timepoint) => {
    state.views[timepoint].slice = 0;
  });
}

const el = {
  navTabs: Array.from(document.querySelectorAll(".workspace-tab")),
  panels: Array.from(document.querySelectorAll(".workspace-panel")),
  integrationStrip: document.getElementById("integration-strip"),
  jumpUpload: document.getElementById("jump-upload"),
  jumpViewer: document.getElementById("jump-viewer"),
  bulkDicomForm: document.getElementById("bulk-dicom-form"),
  bulkDicomFolder: document.getElementById("bulk-dicom-folder"),
  bulkDicomStatus: document.getElementById("bulk-dicom-status"),
  bulkDicomResults: document.getElementById("bulk-dicom-results"),
  uploadForm: document.getElementById("upload-form"),
  studyStatus: document.getElementById("study-status"),
  refreshWorklist: document.getElementById("refresh-worklist"),
  worklistBody: document.getElementById("worklist-body"),
  worklistEmpty: document.getElementById("worklist-empty"),
  studySummary: document.getElementById("study-summary"),
  metadataPanel: document.getElementById("metadata-panel"),
  launchLinks: document.getElementById("launch-links"),
  worklistStatus: document.getElementById("worklist-status"),
  worklistPriority: document.getElementById("worklist-priority"),
  worklistAssignee: document.getElementById("worklist-assignee"),
  saveWorklist: document.getElementById("save-worklist"),
  scheduleForm: document.getElementById("schedule-form"),
  scheduleList: document.getElementById("schedule-list"),
  billingForm: document.getElementById("billing-form"),
  billingList: document.getElementById("billing-list"),
  reportIndication: document.getElementById("report-indication"),
  reportFindings: document.getElementById("report-findings"),
  reportImpression: document.getElementById("report-impression"),
  reportStatus: document.getElementById("report-status"),
  reportSignedBy: document.getElementById("report-signed-by"),
  reportStatusNote: document.getElementById("report-status-note"),
  saveReport: document.getElementById("save-report"),
  editorToolbar: document.querySelector(".editor-toolbar"),
  insertMeasurement: document.getElementById("insert-measurement"),
  speechStatus: document.getElementById("speech-status"),
  dictationTarget: document.getElementById("dictation-target"),
  startDictation: document.getElementById("start-dictation"),
  stopDictation: document.getElementById("stop-dictation"),
  dictationStatus: document.getElementById("dictation-status"),
  taskType: document.getElementById("task-type"),
  modelSelect: document.getElementById("model-select"),
  taskPrompt: document.getElementById("task-prompt"),
  insertIntoReport: document.getElementById("insert-into-report"),
  runTask: document.getElementById("run-task"),
  quickSegmentation: document.getElementById("quick-segmentation"),
  quickScore: document.getElementById("quick-score"),
  taskStatus: document.getElementById("task-status"),
  aiTasks: document.getElementById("ai-tasks"),
  modelCatalog: document.getElementById("model-catalog"),
  trainingForm: document.getElementById("training-form"),
  trainingRuns: document.getElementById("training-runs"),
  viewerLayout: document.getElementById("viewer-layout"),
  activeSeries: document.getElementById("active-series"),
  viewerPlane: document.getElementById("viewer-plane"),
  viewerPreset: document.getElementById("viewer-preset"),
  viewerTool: document.getElementById("viewer-tool"),
  windowCenter: document.getElementById("window-center"),
  windowWidth: document.getElementById("window-width"),
  viewerInvert: document.getElementById("viewer-invert"),
  viewerZoom: document.getElementById("viewer-zoom"),
  cineFps: document.getElementById("cine-fps"),
  applyWindow: document.getElementById("apply-window"),
  playCine: document.getElementById("play-cine"),
  stopCine: document.getElementById("stop-cine"),
  resetViewer: document.getElementById("reset-viewer"),
  measurementStatus: document.getElementById("measurement-status"),
  measurementList: document.getElementById("measurement-list"),
  baselineViewerCard: document.getElementById("baseline-viewer-card"),
  followupViewerCard: document.getElementById("followup-viewer-card"),
  baselineSlice: document.getElementById("baseline-slice"),
  followupSlice: document.getElementById("followup-slice"),
  baselineSliceLabel: document.getElementById("baseline-slice-label"),
  followupSliceLabel: document.getElementById("followup-slice-label"),
  baselineView: document.getElementById("baseline-view"),
  followupView: document.getElementById("followup-view"),
  brush: document.getElementById("brush-size"),
  saveBaseline: document.getElementById("save-baseline-slice"),
  saveFollowup: document.getElementById("save-followup-slice"),
};

const displayCtx = {
  baseline: el.baselineView.getContext("2d"),
  followup: el.followupView.getContext("2d"),
};

const maskLayers = {
  baseline: document.createElement("canvas"),
  followup: document.createElement("canvas"),
};
maskLayers.baseline.width = maskLayers.followup.width = 512;
maskLayers.baseline.height = maskLayers.followup.height = 512;

const maskCtx = {
  baseline: maskLayers.baseline.getContext("2d"),
  followup: maskLayers.followup.getContext("2d"),
};

async function api(path, opts = {}) {
  const response = await fetch(path, opts);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail || detail;
    } catch (_) {
      // Keep statusText fallback.
    }
    throw new Error(detail);
  }
  return response.json();
}

function setUrlStudyId(studyId) {
  const url = new URL(window.location.href);
  if (studyId) {
    url.searchParams.set("study_id", studyId);
  } else {
    url.searchParams.delete("study_id");
  }
  window.history.replaceState({}, "", url);
}

function setStatus(target, message) {
  target.textContent = message;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function htmlFromReport(value) {
  const raw = String(value || "");
  if (/<[a-z][\s\S]*>/i.test(raw)) return raw;
  return escapeHtml(raw).replace(/\n/g, "<br>");
}

function getEditorHtml(editor) {
  return editor.innerHTML.trim();
}

function insertTextIntoEditor(editor, text) {
  editor.focus();
  const spacer = editor.innerText.trim() ? " " : "";
  if (!document.execCommand("insertText", false, `${spacer}${text}`)) {
    editor.innerHTML = `${editor.innerHTML}${escapeHtml(spacer + text)}`;
  }
}

function formPayload(form) {
  const payload = Object.fromEntries(new FormData(form).entries());
  for (const key of Object.keys(payload)) {
    if (payload[key] === "") delete payload[key];
  }
  return payload;
}

function switchPanel(panelName) {
  el.navTabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.panel === panelName));
  el.panels.forEach((panel) => panel.classList.toggle("active", panel.id === `panel-${panelName}`));
}

function speechRecognitionCtor() {
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

function comparisonAvailable() {
  return Boolean(state.workspace) && state.workspace.study?.file_meta?.comparison_uploaded !== false;
}

function visibleTimepoints() {
  if (state.viewer.layout === "compare" && comparisonAvailable()) {
    return SERIES_KEYS;
  }
  return [state.viewer.activeSeries];
}

function applyViewerLayout() {
  const canCompare = comparisonAvailable();
  const comparisonOption = Array.from(el.activeSeries.options).find((option) => option.value === "followup");
  comparisonOption.disabled = !canCompare;
  el.viewerLayout.querySelector('option[value="compare"]').disabled = !canCompare;

  if (!canCompare) {
    state.viewer.layout = "single";
    state.viewer.activeSeries = "baseline";
  }
  el.viewerLayout.value = state.viewer.layout;
  el.activeSeries.value = state.viewer.activeSeries;

  const showBaseline = state.viewer.layout === "compare" || state.viewer.activeSeries === "baseline";
  const showFollowup = canCompare && (state.viewer.layout === "compare" || state.viewer.activeSeries === "followup");
  el[SERIES.baseline.card].hidden = !showBaseline;
  el[SERIES.followup.card].hidden = !showFollowup;
  SERIES_KEYS.forEach((key) => {
    el[SERIES[key].card].classList.toggle("active-series-card", state.viewer.activeSeries === key);
  });
  document.getElementById("viewer-card").classList.toggle("single-viewer", state.viewer.layout === "single");
  el[SERIES.baseline.save].disabled = !state.currentStudyId || (state.viewer.layout === "single" && state.viewer.activeSeries !== "baseline");
  el[SERIES.followup.save].disabled = !state.currentStudyId || !canCompare || (state.viewer.layout === "single" && state.viewer.activeSeries !== "followup");
  el.quickScore.disabled = !state.currentStudyId || !canCompare;
}

function renderIntegrations() {
  if (!state.integrations) return;
  const { archive, viewer, ehr } = state.integrations;
  const speechConfigured = state.speech?.configured || Boolean(speechRecognitionCtor());
  const pills = [
    { label: `Archive ${archive.configured ? "configured" : "scaffold"}`, warning: !archive.configured },
    { label: `Viewer ${viewer.configured ? "configured" : "embedded"}`, warning: false },
    { label: `EHR ${ehr.configured ? "configured" : "scaffold"}`, warning: !ehr.configured },
    { label: `STT ${speechConfigured ? "ready" : "needs model"}`, warning: !speechConfigured },
  ];
  el.integrationStrip.innerHTML = pills
    .map((item) => `<span class="pill${item.warning ? " warning" : ""}">${escapeHtml(item.label)}</span>`)
    .join("");
}

function renderBulkDicomResults(result) {
  const series = result.series || [];
  if (!series.length) {
    el.bulkDicomResults.innerHTML = "";
    return;
  }
  el.bulkDicomResults.innerHTML = series
    .map(
      (item) => `
        <div class="list-card">
          <div class="section-head">
            <strong>${escapeHtml(item.patient_name || item.patient_id || "Unknown patient")}</strong>
            <span class="tag">${escapeHtml(item.modality || "DICOM")}</span>
          </div>
          <div>${escapeHtml(item.description || "DICOM series")}</div>
          <div class="muted-line">Accession ${escapeHtml(item.accession_number)} | ${Number(item.instance_count || 0)} instances</div>
          <div class="mono">${escapeHtml(item.metadata?.study_instance_uid || item.study_id)}</div>
        </div>
      `
    )
    .join("");
}

function renderModelCatalog() {
  el.modelCatalog.innerHTML = state.models
    .map(
      (model) => `
        <div class="list-card">
          <div class="section-head">
            <strong>${escapeHtml(model.name)}</strong>
            <span class="tag ${model.status === "planned" ? "warm" : "good"}">${escapeHtml(model.status)}</span>
          </div>
          <div class="muted-line">${escapeHtml(model.description)}</div>
          <div class="tag-row">
            <span class="tag">${escapeHtml(model.task_type)}</span>
            <span class="tag">${escapeHtml(model.execution_mode)}</span>
            <span class="tag">${escapeHtml(model.modality)}</span>
          </div>
        </div>
      `
    )
    .join("");
}

function renderTrainingRuns() {
  if (!state.trainingRuns.length) {
    el.trainingRuns.innerHTML = `<div class="empty-state" style="display:block">No training runs queued.</div>`;
    return;
  }
  el.trainingRuns.innerHTML = state.trainingRuns
    .map(
      (run) => `
        <div class="list-card">
          <div class="section-head">
            <strong>${escapeHtml(run.dataset_name)}</strong>
            <span class="tag warm">${escapeHtml(run.status)}</span>
          </div>
          <div class="muted-line">Base model: ${escapeHtml(run.base_model_id)}</div>
          <div class="muted-line">Target: ${escapeHtml(run.execution_target)}</div>
          <div class="mono">${escapeHtml(run.notes || "No notes")}</div>
        </div>
      `
    )
    .join("");
}

function renderWorklist() {
  el.worklistEmpty.style.display = state.worklist.length ? "none" : "block";
  el.worklistBody.innerHTML = state.worklist
    .map((item) => {
      const selected = item.study_id === state.currentStudyId ? "selected" : "";
      return `
        <tr class="${selected}" data-study-id="${escapeHtml(item.study_id)}">
          <td>${escapeHtml(item.patient_name || item.patient_id || "Unknown")}</td>
          <td>${escapeHtml(item.accession_number || item.study_id)}</td>
          <td>${escapeHtml(item.status)}</td>
          <td>${escapeHtml(item.priority)}</td>
        </tr>
      `;
    })
    .join("");
}

function renderSchedule() {
  if (!state.schedule.length) {
    el.scheduleList.innerHTML = `<div class="empty-state" style="display:block">No appointments scheduled.</div>`;
    return;
  }
  el.scheduleList.innerHTML = state.schedule
    .slice(0, 8)
    .map((item) => {
      const time = item.scheduled_for ? new Date(item.scheduled_for).toLocaleString() : "unscheduled";
      return `
        <div class="list-card">
          <div class="section-head">
            <strong>${escapeHtml(item.patient_name || item.patient_id)}</strong>
            <span class="tag">${escapeHtml(item.modality)}</span>
          </div>
          <div>${escapeHtml(item.procedure)}</div>
          <div class="muted-line">${escapeHtml(time)} | ${escapeHtml(item.room || "room TBD")}</div>
          <div class="tag-row">
            <span class="tag warm">${escapeHtml(item.status)}</span>
            <span class="tag">${escapeHtml(item.ordering_provider || "provider TBD")}</span>
          </div>
        </div>
      `;
    })
    .join("");
}

function renderBilling() {
  if (!state.billing.length) {
    el.billingList.innerHTML = `<div class="empty-state" style="display:block">No charges created.</div>`;
    return;
  }
  el.billingList.innerHTML = state.billing
    .slice(0, 8)
    .map(
      (item) => `
        <div class="list-card">
          <div class="section-head">
            <strong>${escapeHtml(item.cpt_code)}</strong>
            <span class="tag ${item.status === "paid" ? "good" : "warm"}">${escapeHtml(item.status)}</span>
          </div>
          <div>${escapeHtml(item.description)}</div>
          <div class="muted-line">${escapeHtml(item.patient_id)} | ${escapeHtml(item.accession_number || "no accession")}</div>
          <div class="muted-line">${escapeHtml(item.payer || "payer TBD")} | $${(Number(item.amount_cents || 0) / 100).toFixed(2)}</div>
        </div>
      `
    )
    .join("");
}

function populateModelSelect() {
  el.modelSelect.innerHTML = state.models
    .filter((model) => model.task_type !== "fine_tune")
    .map((model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.name)}</option>`)
    .join("");
  syncModelToTask();
}

function syncModelToTask() {
  const match = state.models.find((model) => model.task_type === el.taskType.value);
  if (match) {
    el.modelSelect.value = match.id;
  }
}

function renderWorkspace() {
  const workspace = state.workspace;
  if (!workspace) {
    el.studySummary.textContent = "Select or create a study to start reading.";
    el.launchLinks.innerHTML = "";
    return;
  }

  const study = workspace.study;
  const worklist = workspace.worklist || {};
  const report = workspace.report || {};
  const meta = study.file_meta || {};

  el.studySummary.innerHTML = `
    <strong>${escapeHtml(study.patient_name || study.patient_id || "Unknown patient")}</strong>
    <div class="muted-line">Study ${escapeHtml(study.study_id)} | Accession ${escapeHtml(study.accession_number)}</div>
    <div class="muted-line">${escapeHtml(study.description)} ${meta.comparison_uploaded === false ? "| no comparison uploaded" : ""}</div>
    <div class="tag-row">
      <span class="tag">${escapeHtml(study.modality)}</span>
      <span class="tag">${escapeHtml(study.body_part)}</span>
      <span class="tag warm">${escapeHtml(worklist.status || study.status)}</span>
      <span class="tag">${escapeHtml(worklist.ai_state || "not_requested")}</span>
    </div>
  `;

  el.worklistStatus.value = worklist.status || "ready_to_read";
  el.worklistPriority.value = worklist.priority || "routine";
  el.worklistAssignee.value = worklist.assignee || "";
  el.reportIndication.value = report.indication || "";
  el.reportFindings.innerHTML = htmlFromReport(report.findings);
  el.reportImpression.innerHTML = htmlFromReport(report.impression);
  el.reportStatus.value = report.status || "draft";
  el.reportSignedBy.value = report.signed_by || "";
  setStatus(el.reportStatusNote, report.updated_at ? `Report updated ${new Date(report.updated_at).toLocaleString()}` : "Draft report ready.");

  el.billingForm.elements.patient_id.value = study.patient_id || "";
  el.billingForm.elements.accession_number.value = study.accession_number || "";
  el.billingForm.elements.description.value = `${study.modality} interpretation`;

  const launchRows = [];
  if (workspace.viewer_context?.ohif_launch_url) {
    launchRows.push(`<div class="list-card"><a href="${escapeHtml(workspace.viewer_context.ohif_launch_url)}" target="_blank" rel="noreferrer">Launch OHIF for this study</a></div>`);
  } else {
    launchRows.push(`<div class="list-card">Embedded PACS viewer active. OHIF launch can be configured with OHIF_BASE_URL.</div>`);
  }
  const ehrConfigured = workspace.integrations?.ehr?.configured;
  launchRows.push(
    `<div class="list-card">${ehrConfigured ? "Epic SMART on FHIR adapter is configured." : "Epic launch adapter is in scaffold mode."}</div>`
  );
  el.launchLinks.innerHTML = launchRows.join("");

  renderMetadataPanel(meta);
  state.measurements = workspace.measurements || [];
  renderMeasurements();
  renderAiTasks();
  renderWorklist();
  setUrlStudyId(study.study_id);
  toggleWorkspaceButtons(true);
  applyViewerLayout();
}

function renderMetadataPanel(meta) {
  const dicom = meta.dicom_metadata || {};
  const baseline = meta.baseline || {};
  const rows = [
    ["Patient ID", dicom.patient_id || meta.patient_id],
    ["Patient name", (dicom.patient_name || "").replaceAll("^", " ")],
    ["Accession", dicom.accession_number],
    ["Modality", dicom.modality || meta.modality],
    ["Body part", dicom.body_part],
    ["Study", dicom.study_description || meta.description],
    ["Series", dicom.series_description],
    ["Study UID", dicom.study_instance_uid],
    ["Series UID", dicom.series_instance_uid],
    ["Instances", dicom.instance_count],
    ["Shape", baseline.shape_zyx?.join(" x ")],
    ["Spacing", baseline.spacing_xyz?.map((v) => Number(v).toFixed(3)).join(" / ")],
  ].filter(([, value]) => value !== undefined && value !== null && value !== "");

  if (!rows.length) {
    el.metadataPanel.innerHTML = `<div class="empty-state" style="display:block">No DICOM metadata loaded yet.</div>`;
    return;
  }
  el.metadataPanel.innerHTML = rows
    .map(
      ([label, value]) => `
        <div class="metadata-row">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </div>
      `
    )
    .join("");
}

function renderAiTasks() {
  const tasks = state.workspace?.ai_tasks || [];
  if (!tasks.length) {
    el.aiTasks.innerHTML = `<div class="empty-state" style="display:block">No AI tasks run on this study yet.</div>`;
    return;
  }
  el.aiTasks.innerHTML = tasks
    .map(
      (task) => `
        <div class="list-card">
          <div class="section-head">
            <strong>${escapeHtml(task.task_type)}</strong>
            <span class="tag ${task.status === "completed" ? "good" : "warm"}">${escapeHtml(task.status)}</span>
          </div>
          <div class="muted-line">${escapeHtml(task.model_id)}</div>
          <div>${escapeHtml(task.output_summary || "No summary")}</div>
          <div class="tag-row">
            ${(task.artifacts || []).map((artifact) => `<span class="tag">${escapeHtml(artifact.label)}</span>`).join("")}
          </div>
        </div>
      `
    )
    .join("");
}

function renderMeasurements() {
  if (!state.measurements.length) {
    el.measurementList.innerHTML = `<div class="empty-state" style="display:block">No saved measurements.</div>`;
    return;
  }
  el.measurementList.innerHTML = state.measurements
    .slice(0, 8)
    .map(
      (m) => `
        <div class="list-card">
          <div class="section-head">
            <strong>${escapeHtml(m.label)}</strong>
            <span class="tag">${Number(m.value_mm || 0).toFixed(1)} mm</span>
          </div>
          <div class="muted-line">${escapeHtml(seriesLabel(m.timepoint))} | ${escapeHtml(m.plane)} | slice ${m.slice_index + 1}</div>
        </div>
      `
    )
    .join("");
}

function toggleWorkspaceButtons(enabled) {
  [
    el.saveWorklist,
    el.saveReport,
    el.runTask,
    el.quickSegmentation,
    el.quickScore,
    el.saveBaseline,
    el.saveFollowup,
    el.applyWindow,
    el.playCine,
    el.resetViewer,
  ].forEach(
    (node) => {
      node.disabled = !enabled;
    }
  );
  applyViewerLayout();
}

async function loadImageB64(b64) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.src = `data:image/png;base64,${b64}`;
  });
}

function imagePointToCanvas(timepoint, point) {
  const [height, width] = state.views[timepoint].shape;
  return {
    x: (Number(point.x) / Math.max(1, width)) * 512,
    y: (Number(point.y) / Math.max(1, height)) * 512,
  };
}

function canvasPointToImage(timepoint, canvasPoint) {
  const [height, width] = state.views[timepoint].shape;
  return {
    x: (canvasPoint.x / 512) * width,
    y: (canvasPoint.y / 512) * height,
  };
}

function drawMeasurementLine(context, timepoint, measurement, color = "#ffe066") {
  if (!measurement.points || measurement.points.length < 2) return;
  const start = imagePointToCanvas(timepoint, measurement.points[0]);
  const end = imagePointToCanvas(timepoint, measurement.points[1]);
  context.save();
  context.strokeStyle = color;
  context.fillStyle = color;
  context.lineWidth = 2;
  context.beginPath();
  context.moveTo(start.x, start.y);
  context.lineTo(end.x, end.y);
  context.stroke();
  context.beginPath();
  context.arc(start.x, start.y, 4, 0, Math.PI * 2);
  context.arc(end.x, end.y, 4, 0, Math.PI * 2);
  context.fill();
  context.font = "14px sans-serif";
  context.fillText(`${Number(measurement.value_mm || 0).toFixed(1)} mm`, end.x + 8, end.y - 8);
  context.restore();
}

function redrawComposite(timepoint) {
  const context = displayCtx[timepoint];
  const baseImg = state.views[timepoint].baseImg;
  if (!baseImg) return;
  const maskData = maskCtx[timepoint].getImageData(0, 0, 512, 512).data;
  context.clearRect(0, 0, 512, 512);
  context.drawImage(baseImg, 0, 0, 512, 512);

  const overlay = context.createImageData(512, 512);
  for (let index = 0; index < maskData.length; index += 4) {
    if (maskData[index] > 20) {
      overlay.data[index] = 255;
      overlay.data[index + 1] = 93;
      overlay.data[index + 2] = 61;
      overlay.data[index + 3] = 150;
    }
  }
  context.putImageData(overlay, 0, 0);

  const visible = state.measurements.filter(
    (m) =>
      m.timepoint === timepoint &&
      m.plane === state.viewer.plane &&
      Number(m.slice_index) === Number(state.views[timepoint].slice)
  );
  visible.forEach((measurement) => drawMeasurementLine(context, timepoint, measurement));

  if (state.measuring.active && state.measuring.timepoint === timepoint && state.measuring.preview) {
    const start = canvasPointToImage(timepoint, state.measuring.start.canvas);
    const end = canvasPointToImage(timepoint, state.measuring.preview.canvas);
    const value = measurementLengthMm(timepoint, start, end);
    drawMeasurementLine(context, timepoint, { points: [start, end], value_mm: value }, "#7dd3fc");
  }
}

async function drawView(timepoint, maskB64) {
  const view = state.views[timepoint];
  if (!view.imageB64 || !maskB64) return;

  const [base, mask] = await Promise.all([loadImageB64(view.imageB64), loadImageB64(maskB64)]);
  view.baseImg = base;
  maskCtx[timepoint].clearRect(0, 0, 512, 512);
  maskCtx[timepoint].drawImage(mask, 0, 0, 512, 512);
  redrawComposite(timepoint);
}

function sliceQuery(timepoint) {
  const view = state.views[timepoint];
  const params = new URLSearchParams({
    timepoint,
    slice_index: String(view.slice),
    plane: state.viewer.plane,
    invert: String(state.viewer.invert),
  });
  if (state.viewer.windowCenter !== "" && state.viewer.windowWidth !== "") {
    params.set("window_center", String(state.viewer.windowCenter));
    params.set("window_width", String(state.viewer.windowWidth));
  }
  return params;
}

async function refreshSlice(timepoint) {
  if (!state.currentStudyId) return;
  const view = state.views[timepoint];
  const data = await api(`/api/studies/${state.currentStudyId}/slice?${sliceQuery(timepoint).toString()}`);
  view.imageB64 = data.image_png_base64;
  view.max = data.max_slices;
  view.shape = data.shape_hw || [512, 512];
  view.spacing = data.pixel_spacing_mm || [1, 1];

  const slider = el[SERIES[timepoint].slice];
  const label = el[SERIES[timepoint].sliceLabel];
  slider.max = String(Math.max(0, view.max - 1));
  slider.value = String(view.slice);
  slider.disabled = view.max <= 1;
  label.textContent = `${state.viewer.plane} ${view.slice + 1}/${view.max}`;
  await drawView(timepoint, data.mask_png_base64);
}

async function refreshViewer() {
  if (!state.currentStudyId) return;
  await Promise.all(visibleTimepoints().map((timepoint) => refreshSlice(timepoint)));
}

function drawBrushStroke(timepoint, x, y, radius) {
  const context = maskCtx[timepoint];
  context.save();
  context.fillStyle = "rgba(255,255,255,1)";
  context.beginPath();
  context.arc(x, y, radius, 0, Math.PI * 2);
  context.fill();
  context.restore();
}

function mapCanvasPoint(event, canvas) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: ((event.clientX - rect.left) / rect.width) * 512,
    y: ((event.clientY - rect.top) / rect.height) * 512,
  };
}

function measurementLengthMm(timepoint, start, end) {
  const [rowSpacing, colSpacing] = state.views[timepoint].spacing;
  const dx = (Number(end.x) - Number(start.x)) * Number(colSpacing || 1);
  const dy = (Number(end.y) - Number(start.y)) * Number(rowSpacing || 1);
  return Math.sqrt(dx * dx + dy * dy);
}

async function saveMeasurement(timepoint, startCanvas, endCanvas) {
  const start = canvasPointToImage(timepoint, startCanvas);
  const end = canvasPointToImage(timepoint, endCanvas);
  const value = measurementLengthMm(timepoint, start, end);
  const payload = {
    timepoint,
    plane: state.viewer.plane,
    slice_index: state.views[timepoint].slice,
    measurement_type: "length",
    label: `${seriesLabel(timepoint)} ${state.viewer.plane} length`,
    points: [start, end],
    value_mm: value,
  };
  const saved = await api(`/api/studies/${state.currentStudyId}/measurements`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  state.measurements.unshift(saved);
  state.lastMeasurement = saved;
  renderMeasurements();
  setStatus(el.measurementStatus, `Saved ${saved.label}: ${Number(saved.value_mm || 0).toFixed(1)} mm`);
  redrawComposite(timepoint);
}

function initViewerCanvas(timepoint, canvas) {
  canvas.addEventListener("pointerdown", async (event) => {
    if (!state.currentStudyId) return;
    const canvasPoint = mapCanvasPoint(event, canvas);
    state.viewer.activeSeries = timepoint;
    el.activeSeries.value = timepoint;
    applyViewerLayout();
    if (state.viewer.tool === "pan") {
      state.windowing = {
        active: true,
        timepoint,
        start: { clientX: event.clientX, clientY: event.clientY },
        center: Number(el.windowCenter.value || 0),
        width: Number(el.windowWidth.value || 500),
      };
      canvas.setPointerCapture?.(event.pointerId);
      return;
    }
    if (state.viewer.tool === "segment") {
      if (state.viewer.plane !== "axial") {
        setStatus(el.measurementStatus, "Segmentation brush writes axial masks only. Switch to axial to edit masks.");
        return;
      }
      state.painting.active = true;
      state.painting.timepoint = timepoint;
      drawBrushStroke(timepoint, canvasPoint.x, canvasPoint.y, Number(el.brush.value));
      redrawComposite(timepoint);
      return;
    }
    if (state.viewer.tool === "measure") {
      state.measuring = { active: true, timepoint, start: { canvas: canvasPoint }, preview: { canvas: canvasPoint } };
      redrawComposite(timepoint);
    }
  });

  canvas.addEventListener("pointermove", async (event) => {
    if (!state.currentStudyId) return;
    const canvasPoint = mapCanvasPoint(event, canvas);
    if (state.windowing.active && state.windowing.timepoint === timepoint) {
      const dx = event.clientX - state.windowing.start.clientX;
      const dy = event.clientY - state.windowing.start.clientY;
      const nextCenter = Math.round(state.windowing.center + dx * 2);
      const nextWidth = Math.max(1, Math.round(state.windowing.width + dy * 4));
      el.windowCenter.value = String(nextCenter);
      el.windowWidth.value = String(nextWidth);
      setStatus(el.measurementStatus, `Window/level preview: C ${nextCenter}, W ${nextWidth}. Release to apply.`);
      return;
    }
    if (state.painting.active && state.painting.timepoint === timepoint) {
      drawBrushStroke(timepoint, canvasPoint.x, canvasPoint.y, Number(el.brush.value));
      redrawComposite(timepoint);
      return;
    }
    if (state.measuring.active && state.measuring.timepoint === timepoint) {
      state.measuring.preview = { canvas: canvasPoint };
      redrawComposite(timepoint);
    }
  });

  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    const next = Math.min(4, Math.max(1, Number(el.viewerZoom.value) + (event.deltaY < 0 ? 0.1 : -0.1)));
    el.viewerZoom.value = next.toFixed(1);
    state.viewer.zoom = next;
    applyZoom();
  });
}

window.addEventListener("pointerup", async () => {
  if (state.painting.active) {
    state.painting.active = false;
  }
  if (state.measuring.active && state.measuring.preview) {
    const { timepoint, start, preview } = state.measuring;
    state.measuring = { active: false, timepoint: null, start: null, preview: null };
    try {
      await saveMeasurement(timepoint, start.canvas, preview.canvas);
    } catch (error) {
      setStatus(el.measurementStatus, error.message);
    }
  }
  if (state.windowing.active) {
    state.windowing.active = false;
    state.viewer.windowCenter = el.windowCenter.value;
    state.viewer.windowWidth = el.windowWidth.value;
    try {
      await refreshViewer();
      setStatus(el.measurementStatus, `Applied window/level: C ${state.viewer.windowCenter}, W ${state.viewer.windowWidth}.`);
    } catch (error) {
      setStatus(el.measurementStatus, error.message);
    }
  }
});

async function saveMaskSlice(timepoint) {
  const b64 = maskLayers[timepoint].toDataURL("image/png").split(",")[1];
  await api(`/api/studies/${state.currentStudyId}/mask/slice`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      timepoint,
      slice_index: state.views[timepoint].slice,
      mask_png_base64: b64,
    }),
  });
  await refreshSlice(timepoint);
}

function applyZoom() {
  SERIES_KEYS.forEach((timepoint) => {
    const canvas = el[SERIES[timepoint].canvas];
    canvas.style.transform = `scale(${state.viewer.zoom})`;
  });
}

function stopCine() {
  if (state.viewer.cineTimer) {
    window.clearInterval(state.viewer.cineTimer);
    state.viewer.cineTimer = null;
  }
  el.playCine.disabled = !state.currentStudyId;
  el.stopCine.disabled = true;
}

function playCine() {
  if (!state.currentStudyId) return;
  stopCine();
  const fps = Math.min(30, Math.max(1, Number(el.cineFps.value || 8)));
  const timepoint = state.viewer.activeSeries;
  state.viewer.cineTimer = window.setInterval(async () => {
    const view = state.views[timepoint];
    view.slice = view.max <= 1 ? 0 : (view.slice + 1) % view.max;
    const slider = el[SERIES[timepoint].slice];
    slider.value = String(view.slice);
    try {
      await refreshSlice(timepoint);
    } catch (error) {
      stopCine();
      setStatus(el.measurementStatus, error.message);
    }
  }, Math.round(1000 / fps));
  el.playCine.disabled = true;
  el.stopCine.disabled = false;
  setStatus(el.measurementStatus, `Cine running on ${seriesLabel(timepoint).toLowerCase()} at ${fps} fps.`);
}

async function resetViewer() {
  stopCine();
  state.viewer.plane = "axial";
  state.viewer.windowCenter = "50";
  state.viewer.windowWidth = "500";
  state.viewer.invert = false;
  state.viewer.zoom = 1;
  state.viewer.tool = "pan";
  resetSeriesSlices();
  el.viewerPlane.value = "axial";
  el.windowCenter.value = "50";
  el.windowWidth.value = "500";
  el.viewerInvert.checked = false;
  el.viewerZoom.value = "1";
  el.viewerTool.value = "pan";
  applyZoom();
  await refreshViewer();
  setStatus(el.measurementStatus, "Viewer reset to axial review mode.");
}

async function loadWorklist() {
  const data = await api("/api/worklist");
  state.worklist = data.items || [];
  renderWorklist();
}

async function loadModels() {
  const data = await api("/api/models");
  state.models = data.items || [];
  populateModelSelect();
  renderModelCatalog();
}

async function loadTrainingRuns() {
  const data = await api("/api/training-runs");
  state.trainingRuns = data.items || [];
  renderTrainingRuns();
}

async function loadSchedule() {
  const data = await api("/api/schedule");
  state.schedule = data.items || [];
  renderSchedule();
}

async function loadBilling() {
  const data = await api("/api/billing");
  state.billing = data.items || [];
  renderBilling();
}

async function loadIntegrations() {
  state.integrations = await api("/api/integrations");
  renderIntegrations();
}

async function loadSpeechStatus() {
  state.speech = await api("/api/speech/status");
  const browserFallback = Boolean(speechRecognitionCtor());
  const message = state.speech.configured
    ? state.speech.message
    : browserFallback
      ? "Hosted browser dictation is available. Configure Whisper.cpp for local open-source backend dictation."
      : state.speech.message;
  setStatus(el.speechStatus, message);
  renderIntegrations();
}

async function loadWorkspace(studyId) {
  const data = await api(`/api/studies/${studyId}/workspace`);
  state.workspace = data;
  state.currentStudyId = studyId;
  renderWorkspace();
  if (data.study?.file_meta?.baseline) {
    resetSeriesSlices();
    applyViewerLayout();
    await refreshViewer();
  }
}

async function runTask(taskType, modelId, prompt, insertIntoReport) {
  const payload = { task_type: taskType, model_id: modelId, prompt, insert_into_report: insertIntoReport };
  const result = await api(`/api/studies/${state.currentStudyId}/ai/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (taskType === "segmentation") {
    await refreshViewer();
  }
  if (result.early_response_score_0_100 !== undefined) {
    setStatus(el.taskStatus, `AI early response score: ${result.early_response_score_0_100}/100`);
  } else if (result.impression) {
    setStatus(el.taskStatus, result.impression);
  } else {
    setStatus(el.taskStatus, "AI task completed.");
  }
  await loadWorkspace(state.currentStudyId);
  await loadWorklist();
}

el.navTabs.forEach((tab) => {
  tab.addEventListener("click", () => switchPanel(tab.dataset.panel));
});

el.jumpUpload.addEventListener("click", () => {
  switchPanel("worklist");
  document.getElementById("intake-card").scrollIntoView({ behavior: "smooth", block: "start" });
});
el.jumpViewer.addEventListener("click", () => {
  switchPanel("pacs");
  document.getElementById("viewer-card").scrollIntoView({ behavior: "smooth", block: "start" });
});

el.bulkDicomFolder.addEventListener("change", () => {
  const count = el.bulkDicomFolder.files?.length || 0;
  setStatus(el.bulkDicomStatus, count ? `${count} DICOM folder files selected.` : "No DICOM folder selected.");
});

el.bulkDicomForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const files = Array.from(el.bulkDicomFolder.files || []);
    if (!files.length) {
      throw new Error("Select a DICOM folder first.");
    }
    const formData = new FormData();
    files.forEach((file) => formData.append("dicom_files", file, file.webkitRelativePath || file.name));
    ["priority", "assignee", "room", "ordering_provider", "payer", "cpt_code"].forEach((name) => {
      const value = el.bulkDicomForm.elements[name]?.value || "";
      if (value) formData.append(name, value);
    });
    setStatus(el.bulkDicomStatus, `Importing ${files.length} DICOM files and extracting metadata...`);
    const data = await api("/api/dicom/bulk", { method: "POST", body: formData });
    setStatus(el.bulkDicomStatus, `Imported ${data.created_count} DICOM series from ${data.uploaded_files} files.`);
    renderBulkDicomResults(data);
    await Promise.all([loadWorklist(), loadSchedule(), loadBilling()]);
    if (data.first_study_id) {
      await loadWorkspace(data.first_study_id);
      switchPanel("pacs");
    }
  } catch (error) {
    setStatus(el.bulkDicomStatus, error.message);
  }
});

el.uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const formData = new FormData(el.uploadForm);
    const data = await api("/api/studies", { method: "POST", body: formData });
    setStatus(el.studyStatus, `Study created: ${data.study_id}`);
    await Promise.all([loadWorklist(), loadSchedule(), loadBilling()]);
    await loadWorkspace(data.study_id);
    switchPanel("pacs");
    document.getElementById("viewer-card").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    setStatus(el.studyStatus, error.message);
  }
});

el.refreshWorklist.addEventListener("click", () => loadWorklist());

el.worklistBody.addEventListener("click", async (event) => {
  const row = event.target.closest("tr[data-study-id]");
  if (!row) return;
  await loadWorkspace(row.dataset.studyId);
  switchPanel("pacs");
});

el.saveWorklist.addEventListener("click", async () => {
  try {
    await api(`/api/studies/${state.currentStudyId}/worklist`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        status: el.worklistStatus.value,
        priority: el.worklistPriority.value,
        assignee: el.worklistAssignee.value,
      }),
    });
    setStatus(el.studyStatus, "Workflow state saved.");
    await loadWorklist();
    await loadWorkspace(state.currentStudyId);
  } catch (error) {
    setStatus(el.studyStatus, error.message);
  }
});

el.scheduleForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const payload = formPayload(el.scheduleForm);
    if (state.currentStudyId) payload.study_id = state.currentStudyId;
    await api("/api/schedule", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await loadSchedule();
  } catch (error) {
    setStatus(el.studyStatus, error.message);
  }
});

el.billingForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const payload = formPayload(el.billingForm);
    if (state.currentStudyId) payload.study_id = state.currentStudyId;
    payload.amount_cents = Number(payload.amount_cents || 0);
    await api("/api/billing", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await loadBilling();
  } catch (error) {
    setStatus(el.studyStatus, error.message);
  }
});

el.saveReport.addEventListener("click", async () => {
  try {
    await api(`/api/studies/${state.currentStudyId}/report`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        indication: el.reportIndication.value,
        findings: getEditorHtml(el.reportFindings),
        impression: getEditorHtml(el.reportImpression),
        status: el.reportStatus.value,
        signed_by: el.reportSignedBy.value,
      }),
    });
    setStatus(el.reportStatusNote, "Report saved.");
    await loadWorklist();
    await loadWorkspace(state.currentStudyId);
  } catch (error) {
    setStatus(el.reportStatusNote, error.message);
  }
});

[el.reportFindings, el.reportImpression].forEach((editor) => {
  editor.addEventListener("focus", () => {
    state.activeEditor = editor;
  });
});
state.activeEditor = el.reportFindings;

el.editorToolbar.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-command]");
  if (!button) return;
  state.activeEditor?.focus();
  document.execCommand(button.dataset.command, false, null);
});

el.insertMeasurement.addEventListener("click", () => {
  if (!state.lastMeasurement) {
    setStatus(el.measurementStatus, "No measurement has been saved yet.");
    return;
  }
  const text = `${state.lastMeasurement.label}: ${Number(state.lastMeasurement.value_mm || 0).toFixed(1)} mm.`;
  insertTextIntoEditor(el.reportFindings, text);
});

el.taskType.addEventListener("change", syncModelToTask);

el.runTask.addEventListener("click", async () => {
  try {
    await runTask(el.taskType.value, el.modelSelect.value, el.taskPrompt.value, el.insertIntoReport.checked);
  } catch (error) {
    setStatus(el.taskStatus, error.message);
  }
});

el.quickSegmentation.addEventListener("click", async () => {
  try {
    const result = await api(`/api/studies/${state.currentStudyId}/segment/auto`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    setStatus(el.taskStatus, result.task?.output_summary || "Segmentation complete.");
    await refreshViewer();
    await loadWorkspace(state.currentStudyId);
    await loadWorklist();
  } catch (error) {
    setStatus(el.taskStatus, error.message);
  }
});

el.quickScore.addEventListener("click", async () => {
  try {
    const result = await api(`/api/studies/${state.currentStudyId}/score`, { method: "POST" });
    setStatus(el.taskStatus, `Quick score: ${result.early_response_score_0_100}/100`);
    await loadWorkspace(state.currentStudyId);
    await loadWorklist();
  } catch (error) {
    setStatus(el.taskStatus, error.message);
  }
});

el.trainingForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const payload = formPayload(el.trainingForm);
    await api("/api/training-runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    el.trainingForm.reset();
    el.trainingForm.elements.base_model_id.value = "tenant-finetune-slot";
    el.trainingForm.elements.dataset_name.value = "site-imaging-dataset";
    el.trainingForm.elements.dataset_source.value = "accepted-segmentations-and-reports";
    el.trainingForm.elements.execution_target.value = "local-gpu";
    await loadTrainingRuns();
  } catch (error) {
    setStatus(el.studyStatus, error.message);
  }
});

el.viewerPreset.addEventListener("change", async () => {
  const preset = WINDOW_PRESETS[el.viewerPreset.value] || WINDOW_PRESETS["ct-soft"];
  el.windowCenter.value = preset.center;
  el.windowWidth.value = preset.width;
  el.viewerInvert.checked = Boolean(preset.invert);
  state.viewer.windowCenter = el.windowCenter.value;
  state.viewer.windowWidth = el.windowWidth.value;
  state.viewer.invert = el.viewerInvert.checked;
  await refreshViewer();
});

el.viewerLayout.addEventListener("change", async () => {
  stopCine();
  state.viewer.layout = el.viewerLayout.value;
  applyViewerLayout();
  await refreshViewer();
});

el.activeSeries.addEventListener("change", async () => {
  stopCine();
  state.viewer.activeSeries = el.activeSeries.value;
  applyViewerLayout();
  await refreshViewer();
});

el.viewerPlane.addEventListener("change", async () => {
  stopCine();
  state.viewer.plane = el.viewerPlane.value;
  resetSeriesSlices();
  await refreshViewer();
});

el.viewerTool.addEventListener("change", () => {
  state.viewer.tool = el.viewerTool.value;
  const message =
    state.viewer.tool === "measure"
      ? "Measure tool active. Click and drag on either image."
      : state.viewer.tool === "segment"
        ? "Segment brush active. Axial mask edits can be saved for the active series."
        : "Review mode active. Use presets, window values, zoom, and slice controls.";
  setStatus(el.measurementStatus, message);
});

el.applyWindow.addEventListener("click", async () => {
  stopCine();
  state.viewer.windowCenter = el.windowCenter.value;
  state.viewer.windowWidth = el.windowWidth.value;
  state.viewer.invert = el.viewerInvert.checked;
  await refreshViewer();
});

el.viewerZoom.addEventListener("input", () => {
  state.viewer.zoom = Number(el.viewerZoom.value);
  applyZoom();
});

el.playCine.addEventListener("click", playCine);
el.stopCine.addEventListener("click", stopCine);
el.resetViewer.addEventListener("click", () => resetViewer());

el.baselineSlice.addEventListener("input", async () => {
  stopCine();
  state.views.baseline.slice = Number(el.baselineSlice.value);
  state.viewer.activeSeries = "baseline";
  applyViewerLayout();
  await refreshSlice("baseline");
});

el.followupSlice.addEventListener("input", async () => {
  stopCine();
  state.views.followup.slice = Number(el.followupSlice.value);
  state.viewer.activeSeries = "followup";
  applyViewerLayout();
  await refreshSlice("followup");
});

el.saveBaseline.addEventListener("click", () => saveMaskSlice("baseline"));
el.saveFollowup.addEventListener("click", () => saveMaskSlice("followup"));

document.addEventListener("keydown", async (event) => {
  const typingTarget = ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName) || event.target.isContentEditable;
  const pacsActive = document.getElementById("panel-pacs").classList.contains("active");
  if (typingTarget || !pacsActive || !state.currentStudyId) return;
  const view = state.views[state.viewer.activeSeries];
  if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
    event.preventDefault();
    stopCine();
    const delta = event.key === "ArrowRight" ? 1 : -1;
    view.slice = Math.min(Math.max(view.slice + delta, 0), Math.max(0, view.max - 1));
    await refreshSlice(state.viewer.activeSeries);
  }
  if (event.code === "Space") {
    event.preventDefault();
    if (state.viewer.cineTimer) {
      stopCine();
    } else {
      playCine();
    }
  }
});

el.startDictation.addEventListener("click", async () => {
  try {
    if (!state.speech?.configured && speechRecognitionCtor()) {
      startBrowserDictation();
      return;
    }
    if (!state.speech?.configured) {
      throw new Error("Configure Whisper.cpp or use a browser with SpeechRecognition support for hosted dictation.");
    }
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      throw new Error("This browser does not expose microphone recording APIs.");
    }
    state.dictation.mode = "backend";
    state.dictation.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    state.dictation.chunks = [];
    state.dictation.recorder = new MediaRecorder(state.dictation.stream);
    state.dictation.recorder.ondataavailable = (event) => {
      if (event.data.size) state.dictation.chunks.push(event.data);
    };
    state.dictation.recorder.onstop = transcribeDictation;
    state.dictation.recorder.start();
    el.startDictation.disabled = true;
    el.stopDictation.disabled = false;
    setStatus(el.dictationStatus, "Recording dictation...");
  } catch (error) {
    setStatus(el.dictationStatus, error.message);
  }
});

el.stopDictation.addEventListener("click", () => {
  if (state.dictation.mode === "browser" && state.dictation.recognition) {
    state.dictation.recognition.stop();
    setStatus(el.dictationStatus, "Finalizing browser transcript...");
    return;
  }
  if (!state.dictation.recorder) return;
  state.dictation.recorder.stop();
  state.dictation.stream?.getTracks().forEach((track) => track.stop());
  el.stopDictation.disabled = true;
  setStatus(el.dictationStatus, "Transcribing with configured open-source model...");
});

function startBrowserDictation() {
  const Recognition = speechRecognitionCtor();
  if (!Recognition) {
    throw new Error("This browser does not support built-in speech recognition.");
  }
  const recognition = new Recognition();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = "en-US";
  state.dictation.mode = "browser";
  state.dictation.transcript = "";
  state.dictation.recognition = recognition;

  recognition.onresult = (event) => {
    let interim = "";
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const chunk = event.results[index][0]?.transcript || "";
      if (event.results[index].isFinal) {
        state.dictation.transcript += `${chunk.trim()} `;
      } else {
        interim += chunk;
      }
    }
    const preview = `${state.dictation.transcript}${interim}`.trim();
    setStatus(el.dictationStatus, preview ? `Dictating: ${preview}` : "Listening...");
  };

  recognition.onerror = (event) => {
    setStatus(el.dictationStatus, event.error || "Browser dictation failed.");
  };

  recognition.onend = () => {
    const transcript = state.dictation.transcript.trim();
    if (transcript) {
      const target = el.dictationTarget.value === "impression" ? el.reportImpression : el.reportFindings;
      insertTextIntoEditor(target, transcript);
      setStatus(el.dictationStatus, "Inserted transcript from browser speech recognition.");
    } else {
      setStatus(el.dictationStatus, "No transcript captured.");
    }
    state.dictation.mode = null;
    state.dictation.recognition = null;
    el.startDictation.disabled = false;
    el.stopDictation.disabled = true;
  };

  recognition.start();
  el.startDictation.disabled = true;
  el.stopDictation.disabled = false;
  setStatus(el.dictationStatus, "Listening with browser speech recognition...");
}

async function transcribeDictation() {
  try {
    const blob = new Blob(state.dictation.chunks, { type: "audio/webm" });
    const form = new FormData();
    form.append("audio_file", blob, "dictation.webm");
    const result = await api("/api/speech/transcribe", { method: "POST", body: form });
    const target = el.dictationTarget.value === "impression" ? el.reportImpression : el.reportFindings;
    insertTextIntoEditor(target, result.text);
    setStatus(el.dictationStatus, `Inserted transcript from ${result.model || result.provider}.`);
  } catch (error) {
    setStatus(el.dictationStatus, error.message);
  } finally {
    state.dictation.mode = null;
    el.startDictation.disabled = false;
    el.stopDictation.disabled = true;
  }
}

initViewerCanvas("baseline", el.baselineView);
initViewerCanvas("followup", el.followupView);

async function bootstrap() {
  toggleWorkspaceButtons(false);
  await Promise.all([loadIntegrations(), loadSpeechStatus(), loadModels(), loadTrainingRuns(), loadWorklist(), loadSchedule(), loadBilling()]);
  if (state.currentStudyId) {
    try {
      await loadWorkspace(state.currentStudyId);
    } catch (error) {
      setStatus(el.studyStatus, error.message);
    }
  }
}

bootstrap();
