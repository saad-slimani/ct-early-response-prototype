"""Single-host research annotation API with immutable masks and explicit AI proposals."""

from contextlib import contextmanager
from datetime import datetime
import fcntl
from functools import lru_cache
import gzip
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Literal
from uuid import UUID, uuid4

import numpy as np
import SimpleITK as sitk
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import AnnotationHead, AnnotationRevision, AnnotationReviewEvent, AnnotationTaskEvent, LungSegmentationJob, StudyRecord
from app.services.annotation_review import (
    active_confirmation, public_review, annotation_db as get_db, actor_for,
    latest_task_event, task_status, task_permissions, require_reviewer, TEST_IDENTITIES,
)
from app.services.annotation_export import export_case_files
from app.services.lung_geometry import crop_for_prompt, paint_stroke, same_grid, validate_image
from app.services.nodules import MAX_NODULE_LABEL, new_nodule, snapshot_nodules, summarize_nodules
from app.services.storage import StudyStore, DATA_ROOT

router = APIRouter(prefix="/api/lung", tags=["Research annotation"])
store = StudyStore()
_runner_lock = threading.Lock()
_shutdown = threading.Event()
_processes = set()
_process_lock = threading.Lock()
logger = logging.getLogger(__name__)


class VersionRequest(BaseModel):
    version: int = Field(ge=0)


class ConfirmRequest(VersionRequest):
    revision_id: UUID
    reviewed: bool = False


class NoduleRequest(VersionRequest):
    nodule_label: int | None = Field(default=None, ge=1, le=MAX_NODULE_LABEL, strict=True)


class SegmentationRequest(NoduleRequest):
    plane: Literal["axial", "coronal", "sagittal"] = "axial"
    frame_index: int = Field(ge=0, strict=True)
    box_xyxy: list[float] = Field(min_length=4, max_length=4)


class StrokeRequest(NoduleRequest):
    plane: Literal["axial", "coronal", "sagittal"]
    slice_index: int = Field(ge=0, strict=True)
    points: list[tuple[float, float]] = Field(min_length=1, max_length=500)
    radius_mm: float = Field(gt=0, le=20, allow_inf_nan=False)
    erase: bool = False
    spherical: bool = False


def folder(study_id):
    return store.study_dir(str(study_id)) / "annotations"


def revision_path(study_id, revision_id):
    return folder(study_id) / f"{UUID(str(revision_id))}.nii.gz"


def job_folder(study_id, job_id):
    return folder(study_id) / "jobs" / str(UUID(str(job_id)))


@lru_cache(maxsize=1)
def image_for(study_id):
    path = store.image_path(str(UUID(str(study_id))), "baseline")
    reader = sitk.ImageFileReader()
    reader.SetFileName(str(path))
    reader.ReadImageInformation()
    if reader.GetNumberOfComponents() != 1 or reader.GetDimension() != 3 or np.prod(reader.GetSize()) > 128 * 1024 * 1024:
        raise HTTPException(422, "Only scalar 3D CT volumes up to 128 million voxels are supported")
    image = reader.Execute()
    try:
        validate_image(image)
        if not np.isfinite(sitk.GetArrayViewFromImage(image)).all():
            raise ValueError("CT contains nonfinite intensities")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return image


def study_head(db, study_id):
    study_id = str(study_id)
    study = db.get(StudyRecord, study_id)
    if study is None:
        raise HTTPException(404, "Study not found")
    if study.modality not in ("CT", "MR"):
        raise HTTPException(422, "Only CT and MR volumes are supported")
    head = db.get(AnnotationHead, study_id)
    if head is None:
        head = AnnotationHead(study_id=study_id, version=0)
        db.add(head)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            head = db.get(AnnotationHead, study_id)
    return head


def check_version(head, version):
    if head.version != version:
        raise HTTPException(409, "The draft changed. Reload before editing or running another model; nothing was overwritten.")


def check_editable(db, head):
    if active_confirmation(db, head):
        raise HTTPException(409, "This task is completed or approved. Reopen it before changing the mask or running segmentation.")


def lock_head(db, head, advance=False):
    # All review transitions and job starts serialize against the same mask-version CAS.
    changed = db.execute(update(AnnotationHead).where(
        AnnotationHead.study_id == head.study_id, AnnotationHead.version == head.version,
    ).values(version=head.version + int(advance)).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "The case changed in another session. Reload; nothing was overwritten.")


def current_mask(head, image):
    if not head.revision_id:
        return np.zeros(tuple(reversed(image.GetSize())), dtype=np.uint8)
    mask = sitk.ReadImage(str(revision_path(head.study_id, head.revision_id)))
    same_grid(mask, image)
    return sitk.GetArrayFromImage(mask).astype(np.uint8)


def nodules_for(db, head, revision=None):
    revision = revision or (db.get(AnnotationRevision, head.revision_id) if head.revision_id else None)
    return snapshot_nodules(revision, revision_path(head.study_id, revision.id) if revision else None)


def selected_nodule(db, head, label):
    nodules = nodules_for(db, head)
    if label is None:
        if len(nodules) != 1 or nodules[0]["label"] != 1:
            raise HTTPException(409, "Select a nodule explicitly. Reload the updated multi-nodule workspace.")
        label = 1
    selected = next((nodule for nodule in nodules if nodule["label"] == label), None)
    if selected is None:
        raise HTTPException(409, "This nodule no longer exists in the current case. Select an existing nodule.")
    return selected, nodules


def save_revision(db, head, image, mask, kind, details, nodules=None, changed_nodule=1):
    check_editable(db, head)
    revision_id = str(uuid4())
    previous_revision = db.get(AnnotationRevision, head.revision_id) if head.revision_id else None
    nodules = nodules_for(db, head) if nodules is None else nodules
    nodules = summarize_nodules(mask, nodules, image.GetSpacing())
    for nodule in nodules:
        if nodule["label"] == changed_nodule:
            nodule["mask_revision_id"] = revision_id
    high_water = max([previous_revision.details.get("max_nodule_label", 1) if previous_revision else 1,
                      *[nodule["label"] for nodule in nodules]])
    path = revision_path(head.study_id, revision_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    output = sitk.GetImageFromArray(mask.astype(np.uint8))
    output.CopyInformation(image)
    temporary = path.with_name(f"{revision_id}.partial.nii.gz")
    sitk.WriteImage(output, str(temporary), useCompression=True)
    temporary.replace(path)
    details = {**details, "volume_ml": sum(nodule["volume_ml"] for nodule in nodules),
               "nodules": nodules, "nodule_schema": 1, "max_nodule_label": high_water,
               "actor": actor_for(db), "identity_mode": actor_for(db).get("identity_mode", "self_selected_test")}
    previous, version = head.revision_id, head.version
    changed = db.execute(update(AnnotationHead).where(
        AnnotationHead.study_id == head.study_id, AnnotationHead.version == version,
    ).values(version=version + 1, revision_id=revision_id).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        db.rollback()
        path.unlink(missing_ok=True)
        raise HTTPException(409, "Concurrent edit detected; nothing was overwritten. Reload the draft.")
    db.add(AnnotationRevision(id=revision_id, study_id=head.study_id, parent_id=previous, kind=kind, details=details))
    try:
        db.commit()
    except Exception:
        db.rollback()
        path.unlink(missing_ok=True)
        raise
    return {"revision_id": revision_id, "version": version + 1, **details}


def public_job(job):
    return {"id": job.id, "study_id": job.study_id, "base_version": job.base_version,
            "status": job.status, "prompt": job.prompt, "result": job.result, "error": job.error,
            "created_at": job.created_at.isoformat() + "Z", "updated_at": job.updated_at.isoformat() + "Z"}


def get_job(db, study_id, job_id):
    job = db.get(LungSegmentationJob, str(job_id))
    if job is None or job.study_id != str(study_id):
        raise HTTPException(404, "Job not found")
    return job


def model_status():
    configured = all(bool(os.getenv(key)) and Path(os.environ[key]).exists()
                     for key in ("LUNG_MODEL_PYTHON", "LUNG_MODEL_SOURCE", "LUNG_MODEL_WEIGHTS"))
    enabled = os.getenv("LUNG_RESEARCH_ENABLED", "").lower() == "true"
    return {"id": "medsam2-efficient-tiny", "name": "Efficient MedSAM2 Tiny", "configured": configured,
            "enabled": enabled, "available": configured and enabled,
            "license": "Research/education only. Commercial rights unresolved.",
            "device": "CPU", "scope": "One user-localized lesion; at most 64 slices and 256 x 256 pixels.",
            "validation": "Three-case technical pilot, not independent clinical validation."}


@router.get("/model")
def get_model_status():
    return model_status()


@router.get("/identities")
def test_identities(db: Session = Depends(get_db)):
    return {"mode": "self_selected_test", "current": actor_for(db),
            "identities": list(TEST_IDENTITIES.values()),
            "notice": "Test identities are self-selected, not authenticated. Do not use for clinical sign-off."}


@router.get("/studies/{study_id}")
def annotation_state(study_id: UUID, db: Session = Depends(get_db)):
    head = study_head(db, study_id)
    image = image_for(str(study_id))
    history = db.execute(select(AnnotationRevision).where(AnnotationRevision.study_id == str(study_id))
                         .order_by(AnnotationRevision.created_at.desc()).limit(100)).scalars().all()
    order = (LungSegmentationJob.created_at.desc(), LungSegmentationJob.id.desc())
    ranked_jobs = select(LungSegmentationJob.id,
        func.row_number().over(partition_by=func.coalesce(LungSegmentationJob.prompt["nodule_label"].as_integer(), 1),
                               order_by=order).label("nodule_rank"),
        func.row_number().over(order_by=order).label("recent_rank"),
    ).where(LungSegmentationJob.study_id == str(study_id)).subquery()
    # Keep each nodule's latest result accessible even after many runs on another nodule.
    jobs = db.execute(select(LungSegmentationJob).join(ranked_jobs, ranked_jobs.c.id == LungSegmentationJob.id)
                      .where(or_(ranked_jobs.c.nodule_rank == 1, ranked_jobs.c.recent_rank <= 20))
                      .order_by(*order)).scalars().all()
    confirmation = active_confirmation(db, head)
    reviews = db.execute(select(AnnotationReviewEvent).where(AnnotationReviewEvent.study_id == str(study_id))
                         .order_by(AnnotationReviewEvent.version.desc()).limit(100)).scalars().all()
    events = db.execute(select(AnnotationTaskEvent).where(AnnotationTaskEvent.study_id == str(study_id))
                        .order_by(AnnotationTaskEvent.version.desc()).limit(100)).scalars().all()
    status = task_status(head, events[0] if events else None, confirmation)
    nodules = nodules_for(db, head)
    current_revision = db.get(AnnotationRevision, head.revision_id) if head.revision_id else None
    return {"study_id": str(study_id), "version": head.version, "revision_id": head.revision_id,
            "nodules": nodules, "mask_encoding": "uint8-nodule-labels",
            "can_add_nodule": (current_revision.details.get("max_nodule_label", 1) if current_revision else 1) < MAX_NODULE_LABEL,
            "annotation_status": status, "identity": actor_for(db), "permissions": task_permissions(db, status),
            "confirmation": public_review(confirmation) if confirmation else None,
            "review_history": [public_review(event) for event in sorted([*reviews, *events], key=lambda row: row.version, reverse=True)][:100],
            "original_dicom_available": store.dicom_archive_path(str(study_id)).is_file(),
            "size_xyz": image.GetSize(), "spacing_xyz": image.GetSpacing(), "origin_xyz": image.GetOrigin(),
            "direction": image.GetDirection(), "transfer_dtype": "float32-le", "array_order": "zyx",
            "modality": db.get(StudyRecord, str(study_id)).modality,
            "display_range": [float(np.percentile(sitk.GetArrayViewFromImage(image), 1)), float(np.percentile(sitk.GetArrayViewFromImage(image), 99))],
            "history": [{"id": row.id, "parent_id": row.parent_id, "kind": row.kind, "details": row.details,
                         "created_at": row.created_at.isoformat() + "Z"} for row in history],
            "jobs": [public_job(job) for job in jobs]}


def transition(db, head, action, status):
    lock_head(db, head, advance=True)
    event = AnnotationTaskEvent(study_id=head.study_id, revision_id=head.revision_id,
                                version=head.version + 1, action=action, status=status, actor=actor_for(db))
    db.add(event)
    db.commit()
    return public_review(event)


@router.post("/studies/{study_id}/nodules")
def add_nodule(study_id: UUID, req: VersionRequest, db: Session = Depends(get_db)):
    head = study_head(db, study_id)
    check_version(head, req.version)
    check_editable(db, head)
    current = db.get(AnnotationRevision, head.revision_id) if head.revision_id else None
    label = (current.details.get("max_nodule_label", 1) if current else 1) + 1
    if label > MAX_NODULE_LABEL:
        raise HTTPException(422, "This case has used all 255 nodule IDs; deleted IDs are not recycled")
    nodules = [*nodules_for(db, head), new_nodule(label)]
    image = image_for(str(study_id))
    return save_revision(db, head, image, current_mask(head, image), "add-nodule",
                         {"nodule_label": label}, nodules, changed_nodule=label)


@router.post("/studies/{study_id}/open")
def open_case(study_id: UUID, req: VersionRequest, db: Session = Depends(get_db)):
    head = study_head(db, study_id)
    check_version(head, req.version)
    status = task_status(head, latest_task_event(db, head), active_confirmation(db, head))
    if status != "new" or db.info.get("read_only_annotation"):
        return {"status": status, "version": head.version}
    return transition(db, head, "opened", "in_progress")


@router.post("/studies/{study_id}/complete")
@router.post("/studies/{study_id}/confirm", deprecated=True)
def confirm_case(study_id: UUID, req: ConfirmRequest, db: Session = Depends(get_db)):
    head = study_head(db, study_id)
    check_version(head, req.version)
    check_editable(db, head)
    if not req.reviewed:
        raise HTTPException(422, "Confirm that you inspected the saved mask across the relevant slices and planes")
    if head.revision_id != str(req.revision_id):
        raise HTTPException(409, "The selected mask revision is no longer current. Review the current mask first.")
    if not current_mask(head, image_for(str(study_id))).any():
        raise HTTPException(422, "An empty mask cannot be completed as a lesion. Negative-case review is not supported yet.")
    empty = [nodule["id"] for nodule in nodules_for(db, head) if not nodule["voxel_count"]]
    if empty:
        raise HTTPException(422, f"Annotate or delete empty nodules before completing this case: {', '.join(empty)}")
    lock_head(db, head, advance=True)
    running = db.execute(select(LungSegmentationJob.id).where(
        LungSegmentationJob.study_id == str(study_id), LungSegmentationJob.status.in_(["queued", "running"])
    ).limit(1)).first()
    if running:
        db.rollback()
        raise HTTPException(409, "Wait for segmentation to finish or cancel it before completing this task")
    event = AnnotationTaskEvent(study_id=str(study_id), revision_id=head.revision_id,
                               version=head.version + 1, action="completed", status="completed", actor=actor_for(db))
    db.add(event)
    db.commit()
    return public_review(event)


@router.post("/studies/{study_id}/approve")
def approve_case(study_id: UUID, req: ConfirmRequest, db: Session = Depends(get_db)):
    require_reviewer(db)
    head = study_head(db, study_id)
    check_version(head, req.version)
    status = task_status(head, latest_task_event(db, head), active_confirmation(db, head))
    if status != "completed":
        raise HTTPException(409, "Only a completed task can be approved")
    if str(req.revision_id) != head.revision_id:
        raise HTTPException(409, "The mask revision changed; review it again before approval")
    if not req.reviewed:
        raise HTTPException(422, "Inspect the completed mask before approving it")
    return transition(db, head, "approved", "approved")


@router.post("/studies/{study_id}/undo-approval")
def undo_approval(study_id: UUID, req: VersionRequest, db: Session = Depends(get_db)):
    require_reviewer(db)
    head = study_head(db, study_id)
    check_version(head, req.version)
    if task_status(head, latest_task_event(db, head)) != "approved":
        raise HTTPException(409, "This task is not approved")
    return transition(db, head, "approval_undone", "completed")


@router.post("/studies/{study_id}/reopen")
def reopen_case(study_id: UUID, req: VersionRequest, db: Session = Depends(get_db)):
    head = study_head(db, study_id)
    check_version(head, req.version)
    if not active_confirmation(db, head):
        raise HTTPException(409, "This task is already editable")
    if task_status(head, latest_task_event(db, head)) == "approved":
        require_reviewer(db)
    return transition(db, head, "reopened", "in_progress")


@router.get("/studies/{study_id}/volume")
def volume(study_id: UUID, db: Session = Depends(get_db)):
    study_head(db, study_id)
    image = image_for(str(study_id))
    # Preserve MRI floating-point intensities; display transfer never changes source files.
    pixels = sitk.GetArrayViewFromImage(image).astype("<f4")
    return Response(gzip.compress(pixels.tobytes(), compresslevel=1), media_type="application/octet-stream",
                    headers={"Content-Encoding": "gzip", "Cache-Control": "no-store"})


@router.get("/studies/{study_id}/mask")
def mask_bytes(study_id: UUID, revision_id: UUID | None = None, job_id: UUID | None = None,
               db: Session = Depends(get_db)):
    head = study_head(db, study_id)
    if job_id:
        job = get_job(db, study_id, job_id)
        if job.status != "succeeded":
            raise HTTPException(409, "Proposal is not ready")
        mask = sitk.GetArrayFromImage(sitk.ReadImage(str(job_folder(study_id, job_id) / "proposal.nii.gz")))
    elif revision_id:
        revision = db.get(AnnotationRevision, str(revision_id))
        if revision is None or revision.study_id != str(study_id):
            raise HTTPException(404, "Revision not found")
        mask = sitk.GetArrayFromImage(sitk.ReadImage(str(revision_path(study_id, revision_id))))
    else:
        mask = current_mask(head, image_for(str(study_id)))
    return Response(gzip.compress(mask.astype(np.uint8).tobytes(), compresslevel=1),
                    media_type="application/octet-stream", headers={"Content-Encoding": "gzip", "Cache-Control": "no-store"})


@router.post("/studies/{study_id}/strokes")
def stroke(study_id: UUID, req: StrokeRequest, db: Session = Depends(get_db)):
    head = study_head(db, study_id)
    check_version(head, req.version)
    check_editable(db, head)
    nodule, nodules = selected_nodule(db, head, req.nodule_label)
    image = image_for(str(study_id))
    mask = current_mask(head, image)
    try:
        feedback = {}
        paint_stroke(mask, image.GetSpacing(), req.plane, req.slice_index, req.points, req.radius_mm,
                     req.erase, req.spherical, label=nodule["label"], feedback=feedback)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return save_revision(db, head, image, mask, "erase" if req.erase else "brush",
                         {**req.model_dump(exclude={"version"}), "nodule_label": nodule["label"], **feedback},
                         nodules, changed_nodule=nodule["label"])


@router.post("/studies/{study_id}/restore/{revision_id}")
def restore(study_id: UUID, revision_id: UUID, req: VersionRequest, db: Session = Depends(get_db)):
    head = study_head(db, study_id)
    check_version(head, req.version)
    revision = db.get(AnnotationRevision, str(revision_id))
    if revision is None or revision.study_id != str(study_id):
        raise HTTPException(404, "Revision not found")
    image = image_for(str(study_id))
    mask = sitk.ReadImage(str(revision_path(study_id, revision_id)))
    same_grid(mask, image)
    return save_revision(db, head, image, sitk.GetArrayFromImage(mask), "restore",
                         {"restored_revision_id": str(revision_id)}, nodules_for(db, head, revision), changed_nodule=None)


@router.post("/studies/{study_id}/restore-initial")
def restore_initial(study_id: UUID, req: VersionRequest, db: Session = Depends(get_db)):
    head = study_head(db, study_id)
    check_version(head, req.version)
    image = image_for(str(study_id))
    return save_revision(db, head, image, np.zeros(image.GetSize()[::-1], dtype=np.uint8), "restore-initial", {},
                         [new_nodule(1)], changed_nodule=None)


@router.post("/studies/{study_id}/clear")
def clear(study_id: UUID, req: NoduleRequest, db: Session = Depends(get_db)):
    head = study_head(db, study_id)
    check_version(head, req.version)
    nodule, nodules = selected_nodule(db, head, req.nodule_label)
    image = image_for(str(study_id))
    mask = current_mask(head, image)
    mask[mask == nodule["label"]] = 0
    return save_revision(db, head, image, mask, "clear", {"nodule_label": nodule["label"]}, nodules,
                         changed_nodule=nodule["label"])


@router.post("/studies/{study_id}/delete-mask")
def delete_mask(study_id: UUID, req: NoduleRequest, db: Session = Depends(get_db)):
    head = study_head(db, study_id)
    check_version(head, req.version)
    check_editable(db, head)
    nodule, nodules = selected_nodule(db, head, req.nodule_label)
    image = image_for(str(study_id))
    mask = current_mask(head, image)
    if not nodule["voxel_count"] and req.nodule_label is None:
        raise HTTPException(409, "There is no saved nodule mask to delete")
    mask[mask == nodule["label"]] = 0
    return save_revision(db, head, image, mask, "delete-mask",
                         {"scope": "selected_nodule", "nodule_label": nodule["label"]},
                         [item for item in nodules if item["label"] != nodule["label"]], changed_nodule=None)


@router.get("/studies/{study_id}/export/{revision_id}")
def export_mask(study_id: UUID, revision_id: UUID, db: Session = Depends(get_db)):
    revision = db.get(AnnotationRevision, str(revision_id))
    if revision is None or revision.study_id != str(study_id):
        raise HTTPException(404, "Revision not found")
    return FileResponse(revision_path(study_id, revision_id), filename=f"lung-{study_id}-{revision_id}.nii.gz")


@router.get("/studies/{study_id}/exports/{revision_id}")
def export_completed_case(study_id: UUID, revision_id: UUID, version: int,
                          format: Literal["nifti", "nrrd", "dicom", "bundle"], db: Session = Depends(get_db)):
    head = study_head(db, study_id)
    check_version(head, version)
    confirmation = active_confirmation(db, head)
    if not confirmation or head.revision_id != str(revision_id):
        raise HTTPException(409, "Export requires the current completed or approved revision. Refresh the case.")
    revision = db.get(AnnotationRevision, head.revision_id)
    history = db.execute(select(AnnotationTaskEvent).where(AnnotationTaskEvent.study_id == str(study_id))
                         .order_by(AnnotationTaskEvent.version)).scalars().all()
    legacy = db.execute(select(AnnotationReviewEvent).where(AnnotationReviewEvent.study_id == str(study_id))).scalars().all()
    revisions = db.execute(select(AnnotationRevision).where(AnnotationRevision.study_id == str(study_id))
                           .order_by(AnnotationRevision.created_at)).scalars().all()
    provenance = {"study_id": str(study_id), "revision_id": head.revision_id, "version": head.version,
                  "identity_mode": actor_for(db).get("identity_mode", "self_selected_test"),
                  "modality": db.get(StudyRecord, str(study_id)).modality,
                  "nodules": nodules_for(db, head),
                  "status": task_status(head, latest_task_event(db, head), confirmation),
                  "revision": {"kind": revision.kind, "details": revision.details},
                  "review": public_review(confirmation),
                  "task_history": [public_review(event) for event in sorted([*legacy, *history], key=lambda event: event.version)],
                  "revision_history": [{"id": row.id, "parent_id": row.parent_id, "kind": row.kind,
                                        "details": row.details, "created_at": row.created_at.isoformat() + "Z"} for row in revisions]}
    return export_case_files(format, revision_path(study_id, revision_id),
                             store.image_path(str(study_id), "baseline"), store.dicom_archive_path(str(study_id)), provenance)


@contextmanager
def worker_slot():
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    with (DATA_ROOT / "lung-worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HTTPException(409, "The local model worker is busy. Wait for the running job or cancel it.") from exc
        try:
            yield lock
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def finish_job(job_id, status, error=None, result=None):
    with SessionLocal() as db:
        db.execute(update(LungSegmentationJob).where(
            LungSegmentationJob.id == job_id, LungSegmentationJob.status.in_(["queued", "running"]),
        ).values(status=status, error=error, result=result, updated_at=datetime.utcnow()))
        db.commit()


def execute_job(job_id, directory, slot, lock_fd):
    process = None
    try:
        with SessionLocal() as db:
            job = db.get(LungSegmentationJob, job_id)
            if job.status != "queued" or _shutdown.is_set():
                return
            changed = db.execute(update(LungSegmentationJob).where(LungSegmentationJob.id == job_id,
                                 LungSegmentationJob.status == "queued")
                                 .values(status="running", updated_at=datetime.utcnow()))
            db.commit()
            if changed.rowcount != 1:
                return
        timeout = max(30, min(1800, int(os.getenv("LUNG_JOB_TIMEOUT_SECONDS", "600"))))
        with (directory / "worker.log").open("w") as log:
            process = subprocess.Popen([os.environ["LUNG_MODEL_PYTHON"], "-m", "app.services.lung_worker",
                                        str(directory / "request.json")], cwd=str(Path(__file__).resolve().parents[1]),
                                       stdout=log, stderr=log, pass_fds=(lock_fd,),
                                       env={**os.environ, "PYTHONUNBUFFERED": "1"})
            with _process_lock:
                _processes.add(process)
            start = time.monotonic()
            while process.poll() is None:
                with SessionLocal() as db:
                    cancelled = db.get(LungSegmentationJob, job_id).status == "cancelled"
                if cancelled or _shutdown.is_set() or time.monotonic() - start > timeout:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    finish_job(job_id, "cancelled" if cancelled else "failed",
                               "Server stopped" if _shutdown.is_set() else "Local inference timed out; no draft was changed.")
                    return
                time.sleep(0.4)
        if process.returncode:
            finish_job(job_id, "failed", "Model worker failed. See the local worker.log; no heuristic was substituted.")
            return
        result = json.loads((directory / "result.json").read_text())
        request = json.loads((directory / "request.json").read_text())
        proposal = sitk.ReadImage(str(directory / "proposal.nii.gz"))
        same_grid(proposal, image_for(request["study_id"]))
        values = sitk.GetArrayViewFromImage(proposal)
        if not np.isin(values, [0, 1]).all():
            raise ValueError("Worker returned a nonbinary mask")
        finish_job(job_id, "succeeded", result=result)
    except Exception:
        logger.exception("Local segmentation job %s failed", job_id)
        finish_job(job_id, "failed", "Unable to complete the model job. Check worker configuration and local logs.")
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            with _process_lock:
                _processes.discard(process)
        slot.__exit__(None, None, None)
        _runner_lock.release()


@router.post("/studies/{study_id}/jobs", status_code=202)
def start_job(study_id: UUID, req: SegmentationRequest, db: Session = Depends(get_db)):
    head = study_head(db, study_id)
    if db.get(StudyRecord, str(study_id)).modality != "CT":
        raise HTTPException(422, "The configured model adapter is CT-only. MRI model validation is pending; manual annotation is available.")
    check_version(head, req.version)
    check_editable(db, head)
    nodule, _ = selected_nodule(db, head, req.nodule_label)
    if not model_status()["available"]:
        raise HTTPException(503, "Local research model is not configured or enabled. Manual annotation remains available.")
    try:
        crop, starts, _ = crop_for_prompt(image_for(str(study_id)), req.frame_index, req.box_xyxy, req.plane)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not _runner_lock.acquire(blocking=False):
        raise HTTPException(409, "The local model worker is busy")
    slot = worker_slot()
    acquired = False
    try:
        lock_file = slot.__enter__()
        acquired = True
        lock_head(db, head)
        # Owning the cross-process slot proves no other local job is still running.
        db.execute(update(LungSegmentationJob).where(LungSegmentationJob.status.in_(["queued", "running"]))
                   .values(status="failed", error="Previous server session ended before job completion"))
        job = LungSegmentationJob(id=str(uuid4()), study_id=str(study_id), base_version=req.version,
                                  status="queued", prompt={"plane": req.plane, "frame_index": req.frame_index, "box_xyxy": req.box_xyxy,
                                  "nodule_label": nodule["label"], "nodule_revision_id": nodule["mask_revision_id"],
                                  "actor": actor_for(db), "identity_mode": "self_selected_test",
                                  "crop_start_xyz": starts, "crop_size_xyz": list(crop.GetSize())})
        directory = job_folder(study_id, job.id)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "request.json").write_text(json.dumps({"study_id": str(study_id),
            "image": str(store.image_path(str(study_id), "baseline").resolve()), **job.prompt}))
        db.add(job)
        db.commit()
        thread = threading.Thread(target=execute_job, args=(job.id, directory, slot, lock_file.fileno()), daemon=True)
        thread.start()
        return public_job(job)
    except Exception:
        if acquired:
            slot.__exit__(None, None, None)
        _runner_lock.release()
        raise


@router.get("/studies/{study_id}/jobs/{job_id}")
def job_status(study_id: UUID, job_id: UUID, db: Session = Depends(get_db)):
    return public_job(get_job(db, study_id, job_id))


@router.post("/studies/{study_id}/jobs/{job_id}/cancel")
def cancel_job(study_id: UUID, job_id: UUID, db: Session = Depends(get_db)):
    job = get_job(db, study_id, job_id)
    if job.status in ("queued", "running"):
        db.execute(update(LungSegmentationJob).where(LungSegmentationJob.id == job.id,
                   LungSegmentationJob.status.in_(["queued", "running"]))
                   .values(status="cancelled", updated_at=datetime.utcnow()))
        db.commit()
        db.refresh(job)
    return public_job(job)


@router.post("/studies/{study_id}/jobs/{job_id}/accept")
def accept_job(study_id: UUID, job_id: UUID, req: VersionRequest, db: Session = Depends(get_db)):
    head = study_head(db, study_id)
    check_version(head, req.version)
    check_editable(db, head)
    job = get_job(db, study_id, job_id)
    if job.status != "succeeded":
        raise HTTPException(409, "Only a completed proposal can be accepted")
    nodule, nodules = selected_nodule(db, head, job.prompt.get("nodule_label", 1))
    target_changed = (nodule["mask_revision_id"] != job.prompt["nodule_revision_id"]
                      if "nodule_revision_id" in job.prompt else job.base_version != head.version)
    if target_changed:
        raise HTTPException(409, "Draft changed after this model job started. Run a new proposal to avoid replacing newer edits.")
    image = image_for(str(study_id))
    mask = sitk.ReadImage(str(job_folder(study_id, job_id) / "proposal.nii.gz"))
    same_grid(mask, image)
    proposal = sitk.GetArrayViewFromImage(mask) > 0
    labels = current_mask(head, image)
    overlap = proposal & (labels != 0) & (labels != nodule["label"])
    if overlap.any():
        conflicts = ", ".join(f"N{label}" for label in np.unique(labels[overlap]))
        raise HTTPException(409, f"AI result overlaps {conflicts}. Adjust the prompt or resolve the overlap first. No nodule was changed.")
    labels[labels == nodule["label"]] = 0
    labels[proposal] = nodule["label"]
    return save_revision(db, head, image, labels, "ai-accepted",
                         {"job_id": job.id, "model": job.result, "nodule_label": nodule["label"]},
                         nodules, changed_nodule=nodule["label"])


def recover_jobs():
    _shutdown.clear()
    try:
        with worker_slot(), SessionLocal() as db:
            db.execute(update(LungSegmentationJob).where(LungSegmentationJob.status.in_(["queued", "running"]))
                       .values(status="failed", error="Server restarted before completion; no draft was changed."))
            db.commit()
    except HTTPException:
        pass  # Another single-host web process owns the worker.


def stop_jobs():
    _shutdown.set()
    with _process_lock:
        for process in list(_processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
