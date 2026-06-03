const state = {
  currentStudyId: new URLSearchParams(window.location.search).get("study_id"),
  workspace: null,
  worklist: [],
  models: [],
  trainingRuns: [],
  integrations: null,
  views: {
    baseline: { slice: 0, max: 1, imageB64: null },
    followup: { slice: 0, max: 1, imageB64: null },
  },
  painting: { active: false, timepoint: null },
};

const el = {
  integrationStrip: document.getElementById("integration-strip"),
  uploadForm: document.getElementById("upload-form"),
  studyStatus: document.getElementById("study-status"),
  refreshWorklist: document.getElementById("refresh-worklist"),
  worklistBody: document.getElementById("worklist-body"),
  worklistEmpty: document.getElementById("worklist-empty"),
  studySummary: document.getElementById("study-summary"),
  launchLinks: document.getElementById("launch-links"),
  worklistStatus: document.getElementById("worklist-status"),
  worklistPriority: document.getElementById("worklist-priority"),
  worklistAssignee: document.getElementById("worklist-assignee"),
  saveWorklist: document.getElementById("save-worklist"),
  reportIndication: document.getElementById("report-indication"),
  reportFindings: document.getElementById("report-findings"),
  reportImpression: document.getElementById("report-impression"),
  reportStatus: document.getElementById("report-status"),
  reportSignedBy: document.getElementById("report-signed-by"),
  reportStatusNote: document.getElementById("report-status-note"),
  saveReport: document.getElementById("save-report"),
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
  baselineSlice: document.getElementById("baseline-slice"),
  followupSlice: document.getElementById("followup-slice"),
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

function renderIntegrations() {
  if (!state.integrations) return;
  const { archive, viewer, ehr } = state.integrations;
  const pills = [
    {
      label: `Archive ${archive.configured ? "configured" : "scaffold"}`,
      warning: !archive.configured,
    },
    {
      label: `Viewer ${viewer.configured ? "configured" : "scaffold"}`,
      warning: !viewer.configured,
    },
    {
      label: `EHR ${ehr.configured ? "configured" : "scaffold"}`,
      warning: !ehr.configured,
    },
  ];
  el.integrationStrip.innerHTML = pills
    .map((item) => `<span class="pill${item.warning ? " warning" : ""}">${escapeHtml(item.label)}</span>`)
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

  el.studySummary.innerHTML = `
    <strong>${escapeHtml(study.patient_name || study.patient_id || "Unknown patient")}</strong>
    <div class="muted-line">Study ${escapeHtml(study.study_id)} | Accession ${escapeHtml(study.accession_number)}</div>
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
  el.reportFindings.value = report.findings || "";
  el.reportImpression.value = report.impression || "";
  el.reportStatus.value = report.status || "draft";
  el.reportSignedBy.value = report.signed_by || "";
  setStatus(el.reportStatusNote, report.updated_at ? `Report updated ${new Date(report.updated_at).toLocaleString()}` : "Draft report ready.");

  const launchRows = [];
  if (workspace.viewer_context?.ohif_launch_url) {
    launchRows.push(`<div class="list-card"><a href="${escapeHtml(workspace.viewer_context.ohif_launch_url)}" target="_blank" rel="noreferrer">Launch OHIF for this study</a></div>`);
  } else {
    launchRows.push(`<div class="list-card">OHIF launch not configured yet. The embedded slice viewer below remains active.</div>`);
  }
  const ehrConfigured = workspace.integrations?.ehr?.configured;
  launchRows.push(
    `<div class="list-card">${ehrConfigured ? "Epic SMART on FHIR adapter is configured." : "Epic launch adapter is still in scaffold mode."}</div>`
  );
  el.launchLinks.innerHTML = launchRows.join("");

  renderAiTasks();
  renderWorklist();
  setUrlStudyId(study.study_id);
  toggleWorkspaceButtons(true);
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
            ${(task.artifacts || [])
              .map((artifact) => `<span class="tag">${escapeHtml(artifact.label)}</span>`)
              .join("")}
          </div>
        </div>
      `
    )
    .join("");
}

function toggleWorkspaceButtons(enabled) {
  [el.saveWorklist, el.saveReport, el.runTask, el.quickSegmentation, el.quickScore, el.saveBaseline, el.saveFollowup].forEach(
    (node) => {
      node.disabled = !enabled;
    }
  );
}

async function loadImageB64(b64) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.src = `data:image/png;base64,${b64}`;
  });
}

function redrawComposite(timepoint, baseImg) {
  const context = displayCtx[timepoint];
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
}

async function drawView(timepoint, maskB64) {
  const view = state.views[timepoint];
  if (!view.imageB64 || !maskB64) return;

  const [base, mask] = await Promise.all([loadImageB64(view.imageB64), loadImageB64(maskB64)]);
  maskCtx[timepoint].clearRect(0, 0, 512, 512);
  maskCtx[timepoint].drawImage(mask, 0, 0, 512, 512);
  redrawComposite(timepoint, base);
}

async function refreshSlice(timepoint) {
  if (!state.currentStudyId) return;
  const view = state.views[timepoint];
  const data = await api(`/api/studies/${state.currentStudyId}/slice?timepoint=${timepoint}&slice_index=${view.slice}`);
  view.imageB64 = data.image_png_base64;
  view.max = data.max_slices;

  const slider = timepoint === "baseline" ? el.baselineSlice : el.followupSlice;
  slider.max = String(Math.max(0, view.max - 1));
  slider.disabled = false;
  await drawView(timepoint, data.mask_png_base64);
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

async function redrawAfterPaint(timepoint) {
  const base = await loadImageB64(state.views[timepoint].imageB64);
  redrawComposite(timepoint, base);
}

function initPainter(timepoint, canvas) {
  const mapPoint = (event) => {
    const rect = canvas.getBoundingClientRect();
    return {
      x: ((event.clientX - rect.left) / rect.width) * 512,
      y: ((event.clientY - rect.top) / rect.height) * 512,
    };
  };

  canvas.addEventListener("pointerdown", async (event) => {
    if (!state.currentStudyId) return;
    state.painting.active = true;
    state.painting.timepoint = timepoint;
    const point = mapPoint(event);
    drawBrushStroke(timepoint, point.x, point.y, Number(el.brush.value));
    await redrawAfterPaint(timepoint);
  });

  canvas.addEventListener("pointermove", async (event) => {
    if (!state.painting.active || state.painting.timepoint !== timepoint) return;
    const point = mapPoint(event);
    drawBrushStroke(timepoint, point.x, point.y, Number(el.brush.value));
    await redrawAfterPaint(timepoint);
  });

  window.addEventListener("pointerup", () => {
    state.painting.active = false;
  });
}

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

async function loadIntegrations() {
  state.integrations = await api("/api/integrations");
  renderIntegrations();
}

async function loadWorkspace(studyId) {
  const data = await api(`/api/studies/${studyId}/workspace`);
  state.workspace = data;
  state.currentStudyId = studyId;
  renderWorkspace();
  if (data.study?.file_meta?.baseline) {
    state.views.baseline.slice = 0;
    state.views.followup.slice = 0;
    await refreshSlice("baseline");
    await refreshSlice("followup");
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
    await refreshSlice("baseline");
    await refreshSlice("followup");
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

el.uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const formData = new FormData(el.uploadForm);
    const data = await api("/api/studies", { method: "POST", body: formData });
    setStatus(el.studyStatus, `Study created: ${data.study_id}`);
    await loadWorklist();
    await loadWorkspace(data.study_id);
  } catch (error) {
    setStatus(el.studyStatus, error.message);
  }
});

el.refreshWorklist.addEventListener("click", () => loadWorklist());

el.worklistBody.addEventListener("click", async (event) => {
  const row = event.target.closest("tr[data-study-id]");
  if (!row) return;
  await loadWorkspace(row.dataset.studyId);
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

el.saveReport.addEventListener("click", async () => {
  try {
    await api(`/api/studies/${state.currentStudyId}/report`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        indication: el.reportIndication.value,
        findings: el.reportFindings.value,
        impression: el.reportImpression.value,
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
    await refreshSlice("baseline");
    await refreshSlice("followup");
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
    const formData = new FormData(el.trainingForm);
    const payload = Object.fromEntries(formData.entries());
    await api("/api/training-runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    el.trainingForm.reset();
    el.trainingForm.elements.base_model_id.value = "tenant-finetune-slot";
    el.trainingForm.elements.dataset_name.value = "site-ct-seg-dataset";
    el.trainingForm.elements.dataset_source.value = "accepted-segmentations";
    el.trainingForm.elements.execution_target.value = "local-gpu";
    await loadTrainingRuns();
  } catch (error) {
    setStatus(el.studyStatus, error.message);
  }
});

el.baselineSlice.addEventListener("input", async () => {
  state.views.baseline.slice = Number(el.baselineSlice.value);
  await refreshSlice("baseline");
});

el.followupSlice.addEventListener("input", async () => {
  state.views.followup.slice = Number(el.followupSlice.value);
  await refreshSlice("followup");
});

el.saveBaseline.addEventListener("click", () => saveMaskSlice("baseline"));
el.saveFollowup.addEventListener("click", () => saveMaskSlice("followup"));

initPainter("baseline", el.baselineView);
initPainter("followup", el.followupView);

async function bootstrap() {
  toggleWorkspaceButtons(false);
  await Promise.all([loadIntegrations(), loadModels(), loadTrainingRuns(), loadWorklist()]);
  if (state.currentStudyId) {
    try {
      await loadWorkspace(state.currentStudyId);
    } catch (error) {
      setStatus(el.studyStatus, error.message);
    }
  }
}

bootstrap();
