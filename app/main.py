from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import numpy as np
import SimpleITK as sitk
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import engine, get_db, init_db
from app.services.imaging import (
    create_empty_mask,
    decode_mask_png_b64,
    image_to_numpy_zyx,
    load_scan_from_file,
    make_png_base64,
    normalize_ct_slice,
    numpy_to_image_zyx,
    save_nifti,
    scan_stats,
)
from app.services.integrations import IntegrationService
from app.services.model_catalog import MODEL_CATALOG
from app.services.radiomics import RadiomicsExample, RadiomicsModel
from app.services.segmentation import SeedPoint, Segmenter
from app.services.storage import StudyStore
from app.services.workflow import WorkflowService


class Seed(BaseModel):
    z: int
    y: int
    x: int


class AutoSegmentRequest(BaseModel):
    baseline_seed: Optional[Seed] = None
    followup_seed: Optional[Seed] = None


class MaskSliceUpdateRequest(BaseModel):
    timepoint: Literal["baseline", "followup"]
    slice_index: int = Field(ge=0)
    mask_png_base64: str


class TrainItem(BaseModel):
    study_id: str
    early_response_score: float = Field(ge=0, le=100)


class TrainRequest(BaseModel):
    items: List[TrainItem]


class WorklistUpdateRequest(BaseModel):
    status: Optional[Literal["new", "ready_to_read", "in_progress", "awaiting_ai_review", "signed"]] = None
    priority: Optional[Literal["routine", "urgent", "stat"]] = None
    assignee: Optional[str] = None
    report_state: Optional[Literal["empty", "draft", "final"]] = None
    ai_state: Optional[Literal["not_requested", "running", "ready_for_review", "accepted"]] = None


class ReportUpdateRequest(BaseModel):
    indication: Optional[str] = None
    findings: Optional[str] = None
    impression: Optional[str] = None
    status: Optional[Literal["draft", "final"]] = None
    signed_by: Optional[str] = None


class AiTaskRunRequest(BaseModel):
    task_type: Literal["segmentation", "early_response", "report_draft"]
    model_id: str
    prompt: str = ""
    insert_into_report: bool = False


class TrainingRunRequest(BaseModel):
    base_model_id: str
    dataset_name: str
    dataset_source: str = "accepted-segmentations"
    execution_target: str = "local-gpu"
    notes: str = ""


store = StudyStore()
segmenter = Segmenter()
radiomics = RadiomicsModel(store.model_path())
workflow = WorkflowService()
integrations = IntegrationService()

init_db()

app = FastAPI(title="AI-Native Imaging Workspace", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _study_meta_or_empty(study_id: str) -> Dict[str, Any]:
    try:
        return store.read_meta(study_id)
    except FileNotFoundError:
        return {"study_id": study_id}


def _workspace_payload(db: Session, study_id: str) -> Dict[str, Any]:
    study = workflow.get_study(db, study_id)
    return workflow.serialize_workspace(
        study,
        file_meta=_study_meta_or_empty(study_id),
        integrations=integrations.summary(),
        viewer_context=integrations.viewer_launch_context(study_id, study.dicom_study_uid),
    )


def _run_segmentation_task(
    db: Session,
    study_id: str,
    req: AutoSegmentRequest,
    *,
    model_id: str,
    prompt: str,
) -> Dict[str, Any]:
    task = workflow.create_ai_task(db, study_id, task_type="segmentation", model_id=model_id, prompt=prompt, status="running")
    try:
        outputs: Dict[str, Any] = {}
        artifacts: List[Dict[str, Any]] = []
        for tp, seed in (("baseline", req.baseline_seed), ("followup", req.followup_seed)):
            seed_obj = SeedPoint(z=seed.z, y=seed.y, x=seed.x) if seed else None
            model_name, out_path = segmenter.run(
                store.image_path(study_id, tp),
                store.mask_path(study_id, tp),
                seed_obj,
            )
            outputs[tp] = {"segmentation_model": model_name, "mask_path": str(out_path)}
            artifacts.append(
                {
                    "artifact_type": "segmentation-mask",
                    "label": f"{tp.title()} mask",
                    "storage_ref": str(out_path),
                    "provenance": {
                        "timepoint": tp,
                        "model_name": model_name,
                        "dicom_export_status": "pending-scaffold",
                    },
                }
            )
        completed = workflow.complete_ai_task(
            db,
            task,
            output_summary="Generated baseline and follow-up segmentation masks.",
            result_payload=outputs,
            artifacts=artifacts,
        )
        return {"task": workflow.serialize_ai_task(completed), "outputs": outputs}
    except Exception as exc:
        workflow.fail_ai_task(db, task, str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _run_early_response_task(
    db: Session,
    study_id: str,
    *,
    model_id: str,
    prompt: str,
    insert_into_report: bool,
) -> Dict[str, Any]:
    task = workflow.create_ai_task(db, study_id, task_type="early_response", model_id=model_id, prompt=prompt, status="running")
    try:
        features = radiomics.extract_features(
            store.image_path(study_id, "baseline"),
            store.image_path(study_id, "followup"),
            store.mask_path(study_id, "baseline"),
            store.mask_path(study_id, "followup"),
        )
        score = round(radiomics.predict(features), 2)
        snippet = f"AI early response score: {score}/100."
        if prompt:
            snippet += f" Requested focus: {prompt.strip()}."
        payload = {
            "study_id": study_id,
            "early_response_score_0_100": score,
            "features": features,
        }
        completed = workflow.complete_ai_task(
            db,
            task,
            output_summary=snippet,
            result_payload=payload,
            artifacts=[
                {
                    "artifact_type": "early-response-score",
                    "label": "Early response score",
                    "report_snippet": snippet,
                    "provenance": {"features": features},
                }
            ],
        )
        if insert_into_report:
            workflow.append_ai_result_to_report(
                db,
                study_id,
                findings_snippet=snippet,
                impression_snippet="AI-assisted score inserted into draft report. Review clinically before sign-off.",
            )
        return {**payload, "task": workflow.serialize_ai_task(completed)}
    except Exception as exc:
        workflow.fail_ai_task(db, task, str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _run_report_draft_task(
    db: Session,
    study_id: str,
    *,
    model_id: str,
    prompt: str,
    insert_into_report: bool,
) -> Dict[str, Any]:
    task = workflow.create_ai_task(db, study_id, task_type="report_draft", model_id=model_id, prompt=prompt, status="running")
    try:
        meta = _study_meta_or_empty(study_id)
        report = workflow.get_or_create_report(db, study_id)
        score_artifact = workflow.latest_result_by_type(db, study_id, "early-response-score")
        findings_lines = []
        baseline = meta.get("baseline") or {}
        followup = meta.get("followup") or {}
        if report.indication:
            findings_lines.append(f"Indication: {report.indication.strip()}")
        if baseline:
            findings_lines.append(
                f"Baseline CT available with shape {baseline.get('shape_zyx')} and spacing {baseline.get('spacing_xyz')}."
            )
        if followup:
            findings_lines.append(
                f"Follow-up CT available with shape {followup.get('shape_zyx')} and spacing {followup.get('spacing_xyz')}."
            )
        if score_artifact and score_artifact.report_snippet:
            findings_lines.append(score_artifact.report_snippet)
        if prompt:
            findings_lines.append(f"Requested focus: {prompt.strip()}")

        findings = "\n".join(findings_lines) or "Baseline and follow-up CT are available for review."
        impression = "AI-assisted draft generated from image metadata and available derived artifacts. Review before final sign-off."
        if score_artifact and score_artifact.report_snippet:
            impression = f"{score_artifact.report_snippet} Clinical correlation and physician review required."

        completed = workflow.complete_ai_task(
            db,
            task,
            output_summary="Generated a draft report suggestion.",
            result_payload={"findings": findings, "impression": impression},
            artifacts=[
                {
                    "artifact_type": "report-draft",
                    "label": "Draft report suggestion",
                    "report_snippet": impression,
                    "provenance": {"insert_into_report": insert_into_report},
                }
            ],
        )
        if insert_into_report:
            workflow.append_ai_result_to_report(
                db,
                study_id,
                findings_snippet=findings,
                impression_snippet=impression,
            )
        return {
            "study_id": study_id,
            "findings": findings,
            "impression": impression,
            "task": workflow.serialize_ai_task(completed),
        }
    except Exception as exc:
        workflow.fail_ai_task(db, task, str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/worklist")
def list_worklist(
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    return {"items": workflow.list_worklist(db, status=status, assignee=assignee)}


@app.get("/api/studies/{study_id}/workspace")
def get_workspace(study_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        return _workspace_payload(db, study_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/studies")
async def create_study(
    baseline_scan: UploadFile = File(...),
    followup_scan: UploadFile = File(...),
    patient_id: str = Form(default="anonymous"),
    patient_name: str = Form(default=""),
    accession_number: str = Form(default=""),
    body_part: str = Form(default="Chest"),
    priority: Literal["routine", "urgent", "stat"] = Form(default="routine"),
    indication: str = Form(default=""),
    assignee: str = Form(default=""),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    study_id = store.create_study()

    try:
        for timepoint, upload in (("baseline", baseline_scan), ("followup", followup_scan)):
            name = (upload.filename or "scan.nii.gz").lower()
            suffix = ".nii.gz" if name.endswith(".nii.gz") else Path(name).suffix
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(await upload.read())
                tmp_path = Path(tmp.name)

            image = load_scan_from_file(tmp_path)
            save_nifti(image, store.image_path(study_id, timepoint))
            save_nifti(create_empty_mask(image), store.mask_path(study_id, timepoint))
            tmp_path.unlink(missing_ok=True)

        b_stats = scan_stats(sitk.ReadImage(str(store.image_path(study_id, "baseline"))))
        f_stats = scan_stats(sitk.ReadImage(str(store.image_path(study_id, "followup"))))

        store.write_meta(
            study_id,
            {
                "patient_id": patient_id,
                "baseline": b_stats,
                "followup": f_stats,
            },
        )
        workflow.bootstrap_study(
            db,
            study_id=study_id,
            patient_identifier=patient_id,
            patient_name=patient_name or None,
            accession_number=accession_number or None,
            body_part=body_part,
            priority=priority,
            indication=indication or None,
            assignee=assignee or None,
            baseline_path=str(store.image_path(study_id, "baseline")),
            followup_path=str(store.image_path(study_id, "followup")),
        )
        return {"study_id": study_id, "meta": store.read_meta(study_id), "workspace": _workspace_payload(db, study_id)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/studies/{study_id}")
def get_study(study_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        return {"meta": store.read_meta(study_id), "workspace": _workspace_payload(db, study_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/studies/{study_id}/slice")
def get_slice(study_id: str, timepoint: Literal["baseline", "followup"], slice_index: int) -> Dict[str, Any]:
    img = sitk.ReadImage(str(store.image_path(study_id, timepoint)))
    msk = sitk.ReadImage(str(store.mask_path(study_id, timepoint)))

    img_arr = image_to_numpy_zyx(img)
    msk_arr = image_to_numpy_zyx(msk)

    if slice_index < 0 or slice_index >= img_arr.shape[0]:
        raise HTTPException(status_code=400, detail=f"slice_index out of range [0,{img_arr.shape[0]-1}]")

    img2d = normalize_ct_slice(img_arr[slice_index])
    m2d = (msk_arr[slice_index] > 0).astype(np.uint8) * 255
    return {
        "slice_index": slice_index,
        "max_slices": int(img_arr.shape[0]),
        "image_png_base64": make_png_base64(img2d),
        "mask_png_base64": make_png_base64(m2d),
        "shape_hw": [int(img2d.shape[0]), int(img2d.shape[1])],
    }


@app.put("/api/studies/{study_id}/mask/slice")
def update_mask_slice(study_id: str, req: MaskSliceUpdateRequest) -> Dict[str, Any]:
    ref_img = sitk.ReadImage(str(store.image_path(study_id, req.timepoint)))
    mask_img = sitk.ReadImage(str(store.mask_path(study_id, req.timepoint)))
    mask_arr = image_to_numpy_zyx(mask_img)

    if req.slice_index >= mask_arr.shape[0]:
        raise HTTPException(status_code=400, detail="slice_index out of range")

    expected_hw = (mask_arr.shape[1], mask_arr.shape[2])
    slice_mask = decode_mask_png_b64(req.mask_png_base64, expected_hw)
    mask_arr[req.slice_index] = slice_mask

    out = numpy_to_image_zyx(mask_arr, ref_img)
    save_nifti(out, store.mask_path(study_id, req.timepoint))
    return {"status": "ok", "updated_slice": req.slice_index}


@app.post("/api/studies/{study_id}/segment/auto")
def auto_segment(study_id: str, req: AutoSegmentRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    return _run_segmentation_task(db, study_id, req, model_id="totalsegmentator-ct", prompt="Quick auto segmentation")


@app.post("/api/model/train")
def train_model(req: TrainRequest) -> Dict[str, Any]:
    rows: List[RadiomicsExample] = []
    for it in req.items:
        sid = it.study_id
        features = radiomics.extract_features(
            store.image_path(sid, "baseline"),
            store.image_path(sid, "followup"),
            store.mask_path(sid, "baseline"),
            store.mask_path(sid, "followup"),
        )
        rows.append(RadiomicsExample(features=features, target=float(it.early_response_score)))
    metrics = radiomics.train(rows)
    return {"status": "trained", **metrics}


@app.post("/api/studies/{study_id}/score")
def score_study(study_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    return _run_early_response_task(
        db,
        study_id,
        model_id="radiomics-early-response-v1",
        prompt="Quick score from current masks",
        insert_into_report=False,
    )


@app.patch("/api/studies/{study_id}/worklist")
def patch_worklist(study_id: str, req: WorklistUpdateRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        item = workflow.update_worklist(
            db,
            study_id,
            status=req.status,
            priority=req.priority,
            assignee=req.assignee,
            report_state=req.report_state,
            ai_state=req.ai_state,
        )
        return workflow.serialize_worklist_item(item) or {}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/studies/{study_id}/report")
def get_report(study_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        return workflow.serialize_report(workflow.get_or_create_report(db, study_id)) or {}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/studies/{study_id}/report")
def put_report(study_id: str, req: ReportUpdateRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        report = workflow.upsert_report(
            db,
            study_id,
            indication=req.indication,
            findings=req.findings,
            impression=req.impression,
            status=req.status,
            signed_by=req.signed_by,
        )
        return workflow.serialize_report(report) or {}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/studies/{study_id}/ai/tasks")
def list_ai_tasks(study_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        return {"items": workflow.list_ai_tasks(db, study_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/studies/{study_id}/ai/tasks")
def run_ai_task(study_id: str, req: AiTaskRunRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    if req.task_type == "segmentation":
        return _run_segmentation_task(
            db,
            study_id,
            AutoSegmentRequest(),
            model_id=req.model_id,
            prompt=req.prompt,
        )
    if req.task_type == "early_response":
        return _run_early_response_task(
            db,
            study_id,
            model_id=req.model_id,
            prompt=req.prompt,
            insert_into_report=req.insert_into_report,
        )
    return _run_report_draft_task(
        db,
        study_id,
        model_id=req.model_id,
        prompt=req.prompt,
        insert_into_report=req.insert_into_report,
    )


@app.get("/api/models")
def list_models() -> Dict[str, Any]:
    return {"items": MODEL_CATALOG}


@app.get("/api/integrations")
def list_integrations() -> Dict[str, Any]:
    return integrations.summary()


@app.get("/api/training-runs")
def list_training_runs(db: Session = Depends(get_db)) -> Dict[str, Any]:
    return {"items": workflow.list_training_runs(db)}


@app.post("/api/training-runs")
def create_training_run(req: TrainingRunRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    run = workflow.create_training_run(
        db,
        base_model_id=req.base_model_id,
        dataset_name=req.dataset_name,
        dataset_source=req.dataset_source,
        execution_target=req.execution_target,
        notes=req.notes,
    )
    return workflow.serialize_training_run(run)


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    summary = integrations.summary()
    db_status = "ok"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        db_status = "error"
    return {
        "status": "ok",
        "database": db_status,
        "archive_configured": summary["archive"]["configured"],
        "viewer_configured": summary["viewer"]["configured"],
    }


static_dir = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


@app.exception_handler(FileNotFoundError)
async def not_found_handler(_, exc: FileNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})
