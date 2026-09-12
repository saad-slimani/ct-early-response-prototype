from __future__ import annotations

import tempfile
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

import numpy as np
import SimpleITK as sitk
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import engine, get_db, init_db
from app.services.imaging import (
    create_empty_mask,
    decode_mask_png_b64,
    discover_dicom_series,
    image_to_numpy_zyx,
    load_scan_from_file,
    make_png_base64,
    normalize_image_slice,
    numpy_to_image_zyx,
    read_dicom_series_from_dir,
    save_nifti,
    scan_stats,
)
from app.services.integrations import IntegrationService
from app.services.model_catalog import MODEL_CATALOG
from app.services.radiomics import RadiomicsExample, RadiomicsModel
from app.services.segmentation import SeedPoint, Segmenter
from app.services.speech_to_text import SpeechToTextNotConfigured, SpeechToTextService
from app.services.storage import StudyStore
from app.services.workflow import WorkflowService
from app.services.dicom_archive import preserve_series, stage_dicom_inputs
from app.lung_api import router as lung_router, recover_jobs, stop_jobs


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


class MeasurementCreateRequest(BaseModel):
    timepoint: Literal["baseline", "followup"]
    plane: Literal["axial", "coronal", "sagittal"] = "axial"
    slice_index: int = Field(ge=0)
    measurement_type: Literal["length"] = "length"
    label: str = "Measurement"
    points: List[Dict[str, float]]
    value_mm: Optional[float] = None


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


class AppointmentRequest(BaseModel):
    study_id: Optional[str] = None
    patient_id: str
    patient_name: str = ""
    modality: Literal["CT", "MR", "US", "MG", "DX", "CR", "XR"] = "CT"
    procedure: str = "Imaging study"
    scheduled_for: Optional[datetime] = None
    room: str = ""
    ordering_provider: str = ""
    status: Literal["scheduled", "arrived", "in_progress", "completed", "cancelled"] = "scheduled"
    notes: str = ""


class BillingItemRequest(BaseModel):
    study_id: Optional[str] = None
    patient_id: str
    accession_number: str = ""
    cpt_code: str = "IMG"
    description: str = "Imaging interpretation"
    payer: str = ""
    amount_cents: int = Field(default=0, ge=0)
    status: Literal["draft", "coded", "submitted", "paid", "denied"] = "draft"


store = StudyStore()
segmenter = Segmenter()
radiomics = RadiomicsModel(store.model_path())
workflow = WorkflowService()
integrations = IntegrationService()
speech_to_text = SpeechToTextService()

init_db()

@asynccontextmanager
async def lifespan(_app):
    recover_jobs()
    try:
        yield
    finally:
        stop_jobs()


app = FastAPI(title="AI-Native Imaging Workspace", version="0.3.0", lifespan=lifespan)
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


def _parse_form_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid datetime: {value}") from exc


def _safe_upload_filename(filename: str, index: int) -> str:
    name = Path((filename or "").replace("\\", "/")).name or f"dicom_{index:06d}.dcm"
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
    return f"{index:06d}_{safe[:120]}"


def _bulk_accession(metadata: Dict[str, Any], study_id: str, index: int) -> str:
    raw = str(metadata.get("accession_number") or "DICOM").strip() or "DICOM"
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in raw)[:40]
    return f"{safe}-{study_id[:8]}-{index + 1}"


def _plane_size(shape_zyx: tuple[int, int, int], plane: str) -> int:
    z, y, x = shape_zyx
    if plane == "coronal":
        return y
    if plane == "sagittal":
        return x
    return z


def _slice_plane(arr_zyx: np.ndarray, plane: str, index: int) -> np.ndarray:
    if plane == "coronal":
        return arr_zyx[:, index, :]
    if plane == "sagittal":
        return arr_zyx[:, :, index]
    return arr_zyx[index, :, :]


def _plane_spacing_mm(spacing_xyz: tuple[float, float, float], plane: str) -> list[float]:
    sx, sy, sz = spacing_xyz
    if plane == "coronal":
        return [float(sz), float(sx)]
    if plane == "sagittal":
        return [float(sz), float(sy)]
    return [float(sy), float(sx)]


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
        labels = {"baseline": "Primary", "followup": "Comparison"}
        meta = _study_meta_or_empty(study_id)
        requested_timepoints = [("baseline", req.baseline_seed)]
        if meta.get("comparison_uploaded", True):
            requested_timepoints.append(("followup", req.followup_seed))
        for tp, seed in requested_timepoints:
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
                    "label": f"{labels[tp]} mask",
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
            output_summary=(
                "Generated primary and comparison segmentation masks."
                if meta.get("comparison_uploaded", True)
                else "Generated primary segmentation mask."
            ),
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
        meta = _study_meta_or_empty(study_id)
        if not meta.get("comparison_uploaded", True):
            raise ValueError("Early response scoring requires a comparison series.")
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
        modality = str(meta.get("modality") or "imaging").upper()
        if baseline:
            findings_lines.append(
                f"Primary {modality} series available with shape {baseline.get('shape_zyx')} and spacing {baseline.get('spacing_xyz')}."
            )
        if followup and meta.get("comparison_uploaded", True):
            findings_lines.append(
                f"Comparison {modality} series available with shape {followup.get('shape_zyx')} and spacing {followup.get('spacing_xyz')}."
            )
        if score_artifact and score_artifact.report_snippet:
            findings_lines.append(score_artifact.report_snippet)
        if prompt:
            findings_lines.append(f"Requested focus: {prompt.strip()}")

        findings = "\n".join(findings_lines) or "Primary imaging series is available for review."
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
    followup_scan: Optional[UploadFile] = File(default=None),
    patient_id: str = Form(default="anonymous"),
    patient_name: str = Form(default=""),
    accession_number: str = Form(default=""),
    modality: Literal["CT", "MR", "US", "MG", "DX", "CR", "XR"] = Form(default="CT"),
    body_part: str = Form(default="Chest"),
    description: str = Form(default="Imaging study"),
    priority: Literal["routine", "urgent", "stat"] = Form(default="routine"),
    indication: str = Form(default=""),
    assignee: str = Form(default=""),
    scheduled_for: str = Form(default=""),
    room: str = Form(default=""),
    ordering_provider: str = Form(default=""),
    payer: str = Form(default=""),
    cpt_code: str = Form(default="IMG"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    study_id = store.create_study()

    try:
        async def load_upload(upload: UploadFile, timepoint: str) -> sitk.Image:
            name = (upload.filename or "scan.nii.gz").lower()
            suffix = ".nii.gz" if name.endswith(".nii.gz") else Path(name).suffix
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp_path = Path(tmp.name)
            try:
                size = 0
                with tmp_path.open("wb") as destination:
                    while chunk := await upload.read(1024 * 1024):
                        size += len(chunk)
                        if size > 512 * 1024 * 1024:
                            raise HTTPException(413, "Upload exceeds the 512 MB local limit")
                        destination.write(chunk)
                return load_scan_from_file(tmp_path, dicom_archive_path=store.dicom_archive_path(study_id, timepoint))
            finally:
                tmp_path.unlink(missing_ok=True)

        baseline_image = await load_upload(baseline_scan, "baseline")
        comparison_uploaded = bool(followup_scan and followup_scan.filename)
        followup_image = await load_upload(followup_scan, "followup") if comparison_uploaded and followup_scan else baseline_image

        for timepoint, image in (("baseline", baseline_image), ("followup", followup_image)):
            save_nifti(image, store.image_path(study_id, timepoint))
            save_nifti(create_empty_mask(image), store.mask_path(study_id, timepoint))

        b_stats = scan_stats(sitk.ReadImage(str(store.image_path(study_id, "baseline"))))
        f_stats = scan_stats(sitk.ReadImage(str(store.image_path(study_id, "followup"))))

        store.write_meta(
            study_id,
            {
                "patient_id": patient_id,
                "modality": modality,
                "description": description,
                "comparison_uploaded": comparison_uploaded,
                "primary_filename": baseline_scan.filename,
                "comparison_filename": followup_scan.filename if comparison_uploaded and followup_scan else None,
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
            modality=modality,
            body_part=body_part,
            description=description,
            priority=priority,
            indication=indication or None,
            assignee=assignee or None,
            baseline_path=str(store.image_path(study_id, "baseline")),
            followup_path=str(store.image_path(study_id, "followup")),
        )
        workflow.create_appointment(
            db,
            study_id=study_id,
            patient_id=patient_id,
            patient_name=patient_name or None,
            modality=modality,
            procedure=description or f"{modality} study",
            scheduled_for=_parse_form_datetime(scheduled_for),
            room=room or None,
            ordering_provider=ordering_provider or None,
            status="scheduled",
            notes="Created from study intake.",
        )
        workflow.create_billing_item(
            db,
            study_id=study_id,
            patient_id=patient_id,
            accession_number=accession_number or workflow.get_study(db, study_id).accession_number,
            cpt_code=cpt_code or "IMG",
            description=description or "Imaging interpretation",
            payer=payer or None,
            amount_cents=0,
            status="draft",
        )
        return {"study_id": study_id, "meta": store.read_meta(study_id), "workspace": _workspace_payload(db, study_id)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/dicom/bulk")
async def bulk_upload_dicom_folder(
    dicom_files: List[UploadFile] = File(...),
    priority: Literal["routine", "urgent", "stat"] = Form(default="routine"),
    assignee: str = Form(default=""),
    room: str = Form(default=""),
    ordering_provider: str = Form(default=""),
    payer: str = Form(default=""),
    cpt_code: str = Form(default="IMG"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    if not dicom_files:
        raise HTTPException(status_code=400, detail="Upload at least one DICOM file.")
    if len(dicom_files) > 3000:
        raise HTTPException(413, "Limit DICOM uploads to 3,000 files per batch")

    created: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as td:
        uploaded_root, root = Path(td) / "uploads", Path(td) / "dicom"
        uploaded_root.mkdir()
        written = 0
        total_bytes = 0
        for index, upload in enumerate(dicom_files):
            file_bytes = 0
            with (uploaded_root / _safe_upload_filename(upload.filename or "", index)).open("wb") as destination:
                while chunk := await upload.read(1024 * 1024):
                    total_bytes += len(chunk)
                    file_bytes += len(chunk)
                    if total_bytes > 512 * 1024 * 1024:
                        raise HTTPException(413, "DICOM batch exceeds the 512 MB local limit")
                    destination.write(chunk)
            written += int(file_bytes > 0)

        if written == 0:
            raise HTTPException(status_code=400, detail="Uploaded DICOM folder was empty.")

        try:
            stage_dicom_inputs(uploaded_root, root)
        except (ValueError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        series_rows = discover_dicom_series(root)
        if not series_rows:
            raise HTTPException(status_code=400, detail="No DICOM series found in uploaded folder.")

        for index, metadata in enumerate(series_rows):
            study_id = store.create_study()
            image = read_dicom_series_from_dir(root, str(metadata["series_id"]))
            original_dicom = preserve_series(root, str(metadata["series_id"]), store.dicom_archive_path(study_id))
            for timepoint in ("baseline", "followup"):
                save_nifti(image, store.image_path(study_id, timepoint))
                save_nifti(create_empty_mask(image), store.mask_path(study_id, timepoint))

            stats = scan_stats(sitk.ReadImage(str(store.image_path(study_id, "baseline"))))
            patient_id = str(metadata.get("patient_id") or f"DICOM-{study_id[:8]}").strip()
            patient_name = str(metadata.get("patient_name") or "").replace("^", " ").strip()
            modality = str(metadata.get("modality") or "CT").strip() or "CT"
            body_part = str(metadata.get("body_part") or "Unspecified").strip() or "Unspecified"
            description = (
                str(metadata.get("series_description") or metadata.get("study_description") or f"{modality} DICOM series")
                .replace("^", " ")
                .strip()
            )
            accession = _bulk_accession(metadata, study_id, index)

            store.write_meta(
                study_id,
                {
                    "patient_id": patient_id,
                    "modality": modality,
                    "description": description,
                    "comparison_uploaded": False,
                    "source": "dicom-folder",
                    "dicom_metadata": metadata,
                    "original_dicom": original_dicom,
                    "baseline": stats,
                    "followup": stats,
                },
            )
            workflow.bootstrap_study(
                db,
                study_id=study_id,
                patient_identifier=patient_id,
                patient_name=patient_name or None,
                accession_number=accession,
                modality=modality,
                body_part=body_part,
                description=description,
                dicom_study_uid=str(metadata.get("study_instance_uid") or "") or None,
                archive_source="dicom-folder-upload",
                priority=priority,
                indication=description,
                assignee=assignee or None,
                baseline_path=str(store.image_path(study_id, "baseline")),
                followup_path=str(store.image_path(study_id, "followup")),
            )
            workflow.create_appointment(
                db,
                study_id=study_id,
                patient_id=patient_id,
                patient_name=patient_name or None,
                modality=modality,
                procedure=description,
                scheduled_for=None,
                room=room or None,
                ordering_provider=ordering_provider or None,
                status="arrived",
                notes="Created from bulk DICOM folder upload.",
            )
            workflow.create_billing_item(
                db,
                study_id=study_id,
                patient_id=patient_id,
                accession_number=accession,
                cpt_code=cpt_code or "IMG",
                description=description,
                payer=payer or None,
                amount_cents=0,
                status="draft",
            )
            created.append(
                {
                    "study_id": study_id,
                    "accession_number": accession,
                    "patient_id": patient_id,
                    "patient_name": patient_name,
                    "modality": modality,
                    "description": description,
                    "instance_count": metadata.get("instance_count", 0),
                    "metadata": metadata,
                }
            )

    first_workspace = _workspace_payload(db, created[0]["study_id"]) if created else None
    return {
        "created_count": len(created),
        "uploaded_files": written,
        "series": created,
        "first_study_id": created[0]["study_id"] if created else None,
        "first_workspace": first_workspace,
    }


@app.get("/api/studies/{study_id}/dicom/download")
def download_original_dicom(study_id: UUID, timepoint: Literal["baseline", "followup"] = "baseline",
                            db: Session = Depends(get_db)):
    workflow.get_study(db, str(study_id))
    archive = store.dicom_archive_path(str(study_id), timepoint)
    if not archive.is_file():
        raise HTTPException(404, "No original DICOM files retained for this series. Re-import the DICOM folder/ZIP; NIfTI uploads cannot be exported as original DICOM.")
    return FileResponse(archive, media_type="application/zip", filename=f"dicom-{study_id}-{timepoint}.zip",
                        headers={"Cache-Control": "no-store"})


@app.get("/api/studies/{study_id}")
def get_study(study_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        return {"meta": store.read_meta(study_id), "workspace": _workspace_payload(db, study_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/studies/{study_id}/slice")
def get_slice(
    study_id: str,
    timepoint: Literal["baseline", "followup"],
    slice_index: int,
    plane: Literal["axial", "coronal", "sagittal"] = "axial",
    window_center: Optional[float] = None,
    window_width: Optional[float] = None,
    invert: bool = False,
) -> Dict[str, Any]:
    img = sitk.ReadImage(str(store.image_path(study_id, timepoint)))
    msk = sitk.ReadImage(str(store.mask_path(study_id, timepoint)))

    img_arr = image_to_numpy_zyx(img)
    msk_arr = image_to_numpy_zyx(msk)
    max_slices = _plane_size(img_arr.shape, plane)

    if slice_index < 0 or slice_index >= max_slices:
        raise HTTPException(status_code=400, detail=f"slice_index out of range [0,{max_slices-1}]")

    img2d = normalize_image_slice(
        _slice_plane(img_arr, plane, slice_index),
        window_center=window_center,
        window_width=window_width,
        invert=invert,
    )
    m2d = (_slice_plane(msk_arr, plane, slice_index) > 0).astype(np.uint8) * 255
    return {
        "slice_index": slice_index,
        "plane": plane,
        "max_slices": int(max_slices),
        "image_png_base64": make_png_base64(img2d),
        "mask_png_base64": make_png_base64(m2d),
        "shape_hw": [int(img2d.shape[0]), int(img2d.shape[1])],
        "pixel_spacing_mm": _plane_spacing_mm(tuple(float(v) for v in img.GetSpacing()), plane),
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


@app.get("/api/studies/{study_id}/measurements")
def list_measurements(study_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        return {"items": workflow.list_measurements(db, study_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/studies/{study_id}/measurements")
def create_measurement(study_id: str, req: MeasurementCreateRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        row = workflow.create_measurement(
            db,
            study_id=study_id,
            timepoint=req.timepoint,
            plane=req.plane,
            slice_index=req.slice_index,
            measurement_type=req.measurement_type,
            label=req.label,
            points=req.points,
            value_mm=req.value_mm,
        )
        return workflow.serialize_measurement(row)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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


@app.get("/api/schedule")
def list_schedule(db: Session = Depends(get_db)) -> Dict[str, Any]:
    return {"items": workflow.list_appointments(db)}


@app.post("/api/schedule")
def create_schedule_item(req: AppointmentRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    row = workflow.create_appointment(
        db,
        study_id=req.study_id,
        patient_id=req.patient_id,
        patient_name=req.patient_name or None,
        modality=req.modality,
        procedure=req.procedure,
        scheduled_for=req.scheduled_for,
        room=req.room or None,
        ordering_provider=req.ordering_provider or None,
        status=req.status,
        notes=req.notes,
    )
    return workflow.serialize_appointment(row)


@app.get("/api/billing")
def list_billing(db: Session = Depends(get_db)) -> Dict[str, Any]:
    return {"items": workflow.list_billing_items(db)}


@app.post("/api/billing")
def create_billing_item(req: BillingItemRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    row = workflow.create_billing_item(
        db,
        study_id=req.study_id,
        patient_id=req.patient_id,
        accession_number=req.accession_number or None,
        cpt_code=req.cpt_code,
        description=req.description,
        payer=req.payer or None,
        amount_cents=req.amount_cents,
        status=req.status,
    )
    return workflow.serialize_billing_item(row)


@app.get("/api/speech/status")
def speech_status() -> Dict[str, Any]:
    return speech_to_text.status()


@app.post("/api/speech/transcribe")
async def transcribe_audio(audio_file: UploadFile = File(...)) -> Dict[str, Any]:
    suffix = Path(audio_file.filename or "dictation.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await audio_file.read())
        tmp_path = Path(tmp.name)
    try:
        return speech_to_text.transcribe(tmp_path, audio_file.content_type)
    except SpeechToTextNotConfigured as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)
        tmp_path.with_suffix(".wav").unlink(missing_ok=True)


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
        "speech_to_text": speech_to_text.status(),
    }


static_dir = Path(__file__).resolve().parent / "static"

app.include_router(lung_router)


@app.get("/", include_in_schema=False)
@app.get("/annotate", include_in_schema=False)
def lung_workspace():
    return FileResponse(static_dir / "annotate.html")


app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


@app.exception_handler(FileNotFoundError)
async def not_found_handler(_, exc: FileNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})
