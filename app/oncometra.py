"""Authenticated Oncometra demonstration using the versioned annotation engine.

Default deployment is deliberately single-instance and disposable on Render Free.
Production storage, distributed jobs, and clinical validation are separate gates.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import csv
import gzip
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import threading
import tempfile
import zipfile
from uuid import UUID, uuid4

import numpy as np
import SimpleITK as sitk
from fastapi import FastAPI, Depends, HTTPException, Request, Response, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import Base, engine, SessionLocal
from app.models import Patient, StudyRecord, WorklistItem, AnnotationHead, AnnotationRevision
from app.oncometra_models import WorkspaceUser, WorkspaceSession, WorkspaceCase, WorkspaceAssignment, WorkspaceFeatureJob, WorkspaceAudit
os.environ.setdefault("LUNG_MODEL_BACKEND", "litemedsam")
from app import lung_api as lung
from app.services.annotation_review import annotation_db, latest_task_event, task_status, actor_for
from app.services.storage import DATA_ROOT
from app.services.feature_protocol import FeatureRequest

ROOT = Path(__file__).resolve().parents[1]
COOKIE = "oncometra_session"
SECURE = bool(os.getenv("RENDER")) or os.getenv("COOKIE_SECURE") == "true"
PROJECTS = [
    {"id": "pancreas-ct", "name": "Pancreas", "modality": "CT", "target": "Pancreatic tumor", "color": "#2a86ff"},
    {"id": "liver-ct", "name": "Liver", "modality": "CT", "target": "Liver tumor", "color": "#00a6a0"},
    {"id": "lung-ct", "name": "Lung", "modality": "CT", "target": "Lung tumor", "color": "#527dea"},
    {"id": "brain-mr", "name": "Brain", "modality": "MR", "target": "Tumor and associated subregions", "color": "#8c71c5"},
    {"id": "brain-ct", "name": "Brain", "modality": "CT", "target": "Brain tumor", "color": "#7493aa", "pending": "A rights-cleared tumor-positive CT case is required."},
    {"id": "colon-ct", "name": "Colon", "modality": "CT", "target": "Colon primary tumor", "color": "#d18640"},
    {"id": "rectum-mr", "name": "Rectum", "modality": "MR", "target": "Rectal tumor", "color": "#ab7290", "pending": "TCGA-READ MRI selection awaits image and tumor verification."},
]
PROJECT_IDS = [project["id"] for project in PROJECTS]
_feature_slot = threading.Lock()
_login_attempts = {}
_login_lock = threading.Lock()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    return f"{salt}${value}"


def user_payload(user):
    return {"id": user.id, "name": user.name, "username": user.username, "role": user.role,
            "projects": user.projects, "identity_mode": "authenticated"}


def audit(db, user, action, subject=None, details=None):
    db.add(WorkspaceAudit(user_id=user.id, action=action, subject_id=subject, details=details or {}))


def current_user(request: Request):
    token = request.cookies.get(COOKIE, "")
    with SessionLocal() as db:
        session = db.get(WorkspaceSession, digest(token)) if token else None
        if not session or session.expires_at <= datetime.utcnow():
            raise HTTPException(401, "Sign in to continue")
        user = db.get(WorkspaceUser, session.user_id)
        if not user:
            raise HTTPException(401, "Session no longer valid")
        return user


def require_admin(user):
    if user.role != "admin":
        raise HTTPException(403, "Administrator access required")


def can_review(user):
    # The initial owner is explicitly a reviewer + administrator in this demo.
    return user.role in ("reviewer", "admin")


def require_project(user, project_id):
    if project_id not in user.projects:
        raise HTTPException(403, "Project access required")


def get_assignment(db, study_id, user):
    assignment = db.get(WorkspaceAssignment, str(study_id))
    if not assignment:
        raise HTTPException(404, "Assignment not found")
    case = db.get(WorkspaceCase, assignment.case_id)
    require_project(user, case.project_id)
    if assignment.user_id != user.id and not can_review(user):
        raise HTTPException(403, "This is another reader's independent assignment")
    return assignment, case


def workspace_db(request: Request, user=Depends(current_user)):
    with SessionLocal() as db:
        actor = user_payload(user)
        if user.role == "admin":
            actor["role"] = "reviewer"
        db.info["annotation_actor"] = actor
        if "study_id" in request.path_params:
            assignment, case = get_assignment(db, request.path_params["study_id"], user)
            own = assignment.user_id == user.id
            action = request.url.path.rsplit("/", 1)[-1]
            db.info["read_only_annotation"] = not own
            db.info["independent_approval"] = not own
            if request.method not in ("GET", "HEAD"):
                if action in ("approve", "undo-approval"):
                    if not can_review(user) or own:
                        raise HTTPException(403, "Approval requires a different reviewer from the annotator")
                elif action == "reopen" and can_review(user):
                    pass
                elif action == "open" and not own:
                    pass
                elif not own:
                    raise HTTPException(403, "Use adjudication to create your own editable copy")
            db.info["workspace_case"] = case.id
        yield db


def state_for(db, assignment):
    head = db.get(AnnotationHead, assignment.study_id)
    event = latest_task_event(db, head) if head else None
    status = task_status(head, event) if head else "new"
    revision = db.get(AnnotationRevision, head.revision_id) if head and head.revision_id else None
    lesions = [n for n in (revision.details.get("nodules", []) if revision else []) if n.get("voxel_count", 0)]
    return {"status": status, "version": head.version if head else 0,
            "revision_id": head.revision_id if head else None, "lesions": len(lesions),
            "volume_ml": sum(n.get("volume_ml", 0) for n in lesions)}


def assignment_payload(db, assignment):
    case = db.get(WorkspaceCase, assignment.case_id)
    owner = db.get(WorkspaceUser, assignment.user_id)
    return {"id": assignment.study_id, "case_id": case.id, "project_id": case.project_id,
            "patient_id": case.patient_id, "modality": case.modality, "reader": owner.name,
            "user_id": owner.id, "kind": assignment.kind, **state_for(db, assignment)}


def image_directory(case):
    return DATA_ROOT / "workspace" / "cases" / case.id


def create_assignment(db, case, user, kind="reader", source=None):
    existing = db.scalar(select(WorkspaceAssignment).where(WorkspaceAssignment.case_id == case.id,
                         WorkspaceAssignment.user_id == user.id, WorkspaceAssignment.kind == kind))
    if existing:
        return existing
    study_id = str(uuid4())
    patient = db.scalar(select(Patient).where(Patient.patient_id == case.patient_id))
    if not patient:
        patient = Patient(patient_id=case.patient_id, display_name=case.patient_id)
        db.add(patient)
        db.flush()
    folder = lung.store.study_dir(study_id)
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(image_directory(case) / "image.nii.gz", folder / "baseline.nii.gz")
    original_dicom = image_directory(case) / "original-dicom.zip"
    if original_dicom.exists():
        lung.store.dicom_archive_path(study_id).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original_dicom, lung.store.dicom_archive_path(study_id))
    lung.store.write_meta(study_id, {"patient_id": case.patient_id, "modality": case.modality,
                                   "description": case.source.get("sequence") or case.project_id,
                                   "source": case.source, "comparison_uploaded": False})
    study = StudyRecord(id=study_id, patient_pk=patient.id, accession_number=study_id,
                        modality=case.modality, description=case.source.get("sequence") or case.project_id,
                        body_part=case.project_id.split("-")[0], baseline_path=str(folder / "baseline.nii.gz"))
    db.add(study)
    db.flush()
    db.add(WorklistItem(study_id=study_id, status="new", assignee=user.name))
    assignment = WorkspaceAssignment(study_id=study_id, case_id=case.id, user_id=user.id, kind=kind,
        provenance={"source_assignment": source.study_id, "source_revision": state_for(db, source)["revision_id"]} if source else {})
    db.add(assignment)
    db.add(AnnotationHead(study_id=study_id, version=0))
    db.commit()
    if source:
        source_head = db.get(AnnotationHead, source.study_id)
        if source_head and source_head.revision_id:
            db.info["annotation_actor"] = {**user_payload(user), "role": "reviewer"}
            image = lung.image_for(study_id)
            lung.save_revision(db, db.get(AnnotationHead, study_id), image, lung.current_mask(source_head, image),
                "adjudication-copy", assignment.provenance, lung.nodules_for(db, source_head))
    return assignment


class BootstrapUser(BaseModel):
    username: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    name: str = Field(min_length=1, max_length=120)
    password_hash: str = Field(pattern=r"^[a-f0-9]{32}\$[a-f0-9]{64}$")
    role: str = Field(pattern=r"^(junior_annotator|senior_annotator|reviewer)$")


def seed_demo():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        # Restarted work is never falsely reported as finished.
        for job in db.scalars(select(WorkspaceFeatureJob).where(WorkspaceFeatureJob.status.in_(["running", "queued"]))):
            job.status, job.error = "failed", "Worker restarted. Run extraction again; no result was published."
        if not db.scalar(select(WorkspaceUser).limit(1)):
            password = os.getenv("ONCOMETRA_ADMIN_PASSWORD")
            if not password:
                if os.getenv("RENDER"):
                    raise RuntimeError("Set ONCOMETRA_ADMIN_PASSWORD in Render before starting the service")
                password = secrets.token_urlsafe(24)
                DATA_ROOT.mkdir(parents=True, exist_ok=True)
                secret_path = DATA_ROOT / "admin-password.txt"
                secret_path.write_text(password)
                secret_path.chmod(0o600)
            if len(password) < 12:
                raise RuntimeError("ONCOMETRA_ADMIN_PASSWORD must be at least 12 characters")
            db.add(WorkspaceUser(username="saad", name="Saad Slimani", password_hash=password_hash(password),
                                 role="admin", projects=PROJECT_IDS))
        for entry in json.loads(os.getenv("ONCOMETRA_BOOTSTRAP_USERS", "[]")):
            account = BootstrapUser.model_validate(entry)
            if not db.scalar(select(WorkspaceUser).where(WorkspaceUser.username == account.username)):
                db.add(WorkspaceUser(**account.model_dump(), projects=PROJECT_IDS))
        for manifest in sorted((ROOT / "demo-cases").glob("*/case.json")):
            info = json.loads(manifest.read_text())
            if db.scalar(select(WorkspaceCase).where(WorkspaceCase.project_id == info["project_id"], WorkspaceCase.patient_id == info["patient_id"])):
                continue
            case = WorkspaceCase(id=str(uuid4()), project_id=info["project_id"], patient_id=info["patient_id"], modality=info["modality"], source=info)
            destination = image_directory(case)
            destination.mkdir(parents=True, exist_ok=True)
            for name in ("image.nii.gz", "reference.nii.gz"):
                data = (manifest.parent / name).read_bytes()
                if hashlib.sha256(data).hexdigest() != info[name + "_sha256"]:
                    raise RuntimeError(f"Demo asset checksum mismatch: {manifest.parent.name}/{name}")
                (destination / name).write_bytes(data)
            db.add(case)
        db.commit()


@asynccontextmanager
async def lifespan(app):
    seed_demo()
    lung.recover_jobs()
    yield
    lung.stop_jobs()


app = FastAPI(title="Oncometra Annotation", version="0.2.1", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.dependency_overrides[annotation_db] = workspace_db


@app.middleware("http")
async def boundaries(request, call_next):
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > 270 * 1024 * 1024):
        return JSONResponse({"detail": "Request exceeds the 256-MB demo upload limit"}, status_code=413)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Cross-origin writes are not allowed"}, status_code=403)
    try:
        if request.url.path.startswith("/api/") and request.url.path != "/api/auth/login":
            current_user(request)
    except HTTPException as error:
        return JSONResponse({"detail": error.detail}, status_code=error.status_code)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz")
def health():
    return {"status": "ok", "application": "oncometra", "version": "0.2.1"}


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


@app.post("/api/auth/login")
def login(req: Login, request: Request, response: Response):
    key = (request.client.host if request.client else "unknown", req.username.lower())
    now = datetime.utcnow()
    with _login_lock:
        recent = [t for t in _login_attempts.get(key, []) if (now - t).total_seconds() < 300]
        if len(recent) >= 10:
            raise HTTPException(429, "Too many attempts. Try again in five minutes.")
        _login_attempts[key] = recent + [now]
        if len(_login_attempts) > 5000:
            _login_attempts.clear()
    with SessionLocal() as db:
        user = db.scalar(select(WorkspaceUser).where(WorkspaceUser.username == req.username.lower()))
        saved = user.password_hash if user else password_hash("invalid")
        if not hmac.compare_digest(password_hash(req.password, saved.split("$")[0]), saved) or not user:
            raise HTTPException(401, "Username or password is incorrect")
        token = secrets.token_urlsafe(32)
        db.add(WorkspaceSession(token_hash=digest(token), user_id=user.id, expires_at=now + timedelta(hours=12)))
        audit(db, user, "signed_in")
        db.commit()
        response.set_cookie(COOKIE, token, max_age=43200, httponly=True, secure=SECURE, samesite="strict")
        return user_payload(user)


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, user=Depends(current_user)):
    with SessionLocal() as db:
        session = db.get(WorkspaceSession, digest(request.cookies.get(COOKIE, "")))
        if session:
            db.delete(session)
            db.commit()
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return user_payload(user)


@app.get("/api/lung/identities")
def identity(user=Depends(current_user)):
    actor = user_payload(user)
    if user.role == "admin":
        actor["role"] = "reviewer"
    return {"current": actor, "identities": [actor], "notice": "Authenticated account. Final approval requires an independent reviewer."}


@app.get("/api/workspace")
def overview(user=Depends(current_user)):
    with SessionLocal() as db:
        cases = list(db.scalars(select(WorkspaceCase).where(WorkspaceCase.project_id.in_(user.projects))))
        assignments = list(db.scalars(select(WorkspaceAssignment).where(WorkspaceAssignment.case_id.in_([case.id for case in cases]))))
        visible = assignments if can_review(user) else [a for a in assignments if a.user_id == user.id]
        rows = [assignment_payload(db, assignment) for assignment in visible]
        projects = []
        for project in PROJECTS:
            if project["id"] not in user.projects:
                continue
            project_cases = [case for case in cases if case.project_id == project["id"]]
            project_rows = [row for row in rows if row["project_id"] == project["id"]]
            projects.append({**project, "cases": len(project_cases), "assignments": len(project_rows),
                "completed": len({r["case_id"] for r in project_rows if r["status"] in ("completed", "approved")}),
                "approved": len({r["case_id"] for r in project_rows if r["status"] == "approved"}),
                "lesion_observations": sum(r["lesions"] for r in project_rows),
                "in_progress": sum(r["status"] == "in_progress" for r in project_rows)})
        return {"projects": projects, "assignments": rows,
            "cases": [{"id": c.id, "project_id": c.project_id, "patient_id": c.patient_id,
                       "modality": c.modality, "source": c.source} for c in cases],
            "storage": "ephemeral" if os.getenv("DEMO_EPHEMERAL", "true") == "true" else "persistent",
            "notice": "Demonstration only. Public de-identified data. Export work before a free-service restart.",
            "counts_scope": "Visible assignments. Lesion counts are reader observations, not adjudicated unique tumors."}


@app.get("/api/worklist")
def viewer_worklist(user=Depends(current_user)):
    rows = overview(user)["assignments"]
    return {"items": [{**row, "study_id": row["id"], "patient": {"patient_id": row["patient_id"]},
                       "description": row["project_id"], "annotation_status": row["status"],
                       "worklist": {"status": row["status"]}} for row in rows]}


@app.get("/api/cases/{case_id}/preview")
def preview(case_id: UUID, user=Depends(current_user)):
    from PIL import Image
    with SessionLocal() as db:
        case = db.get(WorkspaceCase, str(case_id))
        if not case:
            raise HTTPException(404, "Case not found")
        require_project(user, case.project_id)
        directory = image_directory(case)
        path = directory / "preview.png"
        if not path.exists():
            volume = sitk.GetArrayFromImage(sitk.ReadImage(str(directory / "image.nii.gz")))
            mask_path = directory / "reference.nii.gz"
            mask = sitk.GetArrayFromImage(sitk.ReadImage(str(mask_path))) if mask_path.exists() else np.zeros_like(volume, dtype=np.uint8)
            z = int(np.argmax(np.count_nonzero(mask, axis=(1, 2)))) if mask.any() else volume.shape[0] // 2
            level, width = (-600, 1500) if case.project_id == "lung-ct" else (40, 400)
            if case.modality == "MR":
                lo, hi = np.percentile(volume, [1, 99])
                level, width = (lo + hi) / 2, max(1, hi - lo)
            values = (np.clip((volume[z] - level + width / 2) / width, 0, 1) * 255).astype(np.uint8)
            rgb = np.repeat(values[..., None], 3, axis=2)
            selected = mask[z] > 0
            rgb[selected] = (rgb[selected] * .55 + np.array([40, 155, 242]) * .45).astype(np.uint8)
            output = Image.fromarray(rgb)
            output.thumbnail((400, 240))
            output.save(path)
        return FileResponse(path)


@app.post("/api/import", status_code=201)
async def import_images(project_id: str = Form(...), patient_id: str = Form(...),
                        deidentified: bool = Form(False), files: list[UploadFile] = File(...), user=Depends(current_user)):
    from app.services.imaging import discover_dicom_series, read_dicom_series_from_dir
    from app.services.dicom_archive import stage_dicom_inputs, preserve_series
    require_project(user, project_id)
    if not deidentified or not 1 <= len(files) <= 3000 or not 1 <= len(patient_id.strip()) <= 80:
        raise HTTPException(422, "Provide a case ID and confirm de-identification; maximum 3,000 files")
    project = next(p for p in PROJECTS if p["id"] == project_id)
    with tempfile.TemporaryDirectory() as temporary, SessionLocal() as db:
        source, flat = Path(temporary) / "uploads", Path(temporary) / "dicom"
        source.mkdir()
        total = 0
        for index, upload in enumerate(files):
            name = Path((upload.filename or "image.dcm").replace("\\", "/")).name
            if name.startswith("."):
                continue
            suffix = ".nii.gz" if name.endswith(".nii.gz") else Path(name).suffix.lower()
            path = source / f"{index:05d}{suffix}"
            with path.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > 256 * 1024 * 1024:
                        raise HTTPException(413, "This demo accepts up to 256 MB per batch")
                    output.write(chunk)
        nifti = list(source.glob("*.nii*"))
        candidates = []
        try:
            if nifti:
                if len(files) != 1:
                    raise ValueError("Import one NIfTI separately from DICOM files")
                candidates = [(None, {"modality": project["modality"], "series_description": "NIfTI upload"}, nifti[0])]
            else:
                stage_dicom_inputs(source, flat)
                candidates = [(item["series_id"], item, None) for item in discover_dicom_series(flat)]
            if not candidates or len(candidates) > 20:
                raise ValueError("Select a batch with 1-20 CT/MRI series")
            imported, errors = [], []
            for index, (series_id, metadata, nifti_path) in enumerate(candidates):
                if metadata.get("modality") != project["modality"]:
                    errors.append("Series modality does not match the project")
                    continue
                case_id = patient_id.strip() + (f"-S{index + 1}" if len(candidates) > 1 else "")
                if db.scalar(select(WorkspaceCase).where(WorkspaceCase.project_id == project_id, WorkspaceCase.patient_id == case_id)):
                    errors.append(f"Case ID already exists: {case_id}")
                    continue
                # Check headers before materializing a NIfTI volume on a small instance.
                if nifti_path:
                    reader = sitk.ImageFileReader()
                    reader.SetFileName(str(nifti_path))
                    reader.ReadImageInformation()
                    if reader.GetDimension() != 3 or np.prod(reader.GetSize()) > 20_000_000:
                        raise ValueError("Demo volumes must be scalar 3D and at most 20 million voxels")
                    image = reader.Execute()
                else:
                    names = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(str(flat), series_id)
                    reader = sitk.ImageFileReader()
                    reader.SetFileName(names[0])
                    reader.ReadImageInformation()
                    if np.prod(reader.GetSize()) * len(names) > 20_000_000:
                        errors.append(f"Series {index + 1} exceeds the 20-million-voxel demo limit")
                        continue
                    image = read_dicom_series_from_dir(flat, series_id)
                lung.validate_image(image)
                if not np.isfinite(sitk.GetArrayViewFromImage(image)).all():
                    raise ValueError("Nonfinite image intensities are unsupported")
                image = sitk.DICOMOrient(image, "LPS")
                if not np.allclose(image.GetDirection(), np.eye(3).ravel(), atol=1e-4):
                    raise ValueError("Oblique acquisitions require a validated resampling step; not supported in this demo")
                case = WorkspaceCase(id=str(uuid4()), project_id=project_id, patient_id=case_id,
                    modality=project["modality"], source={"source": "User-supplied de-identified images", "license": "User-provided; rights attested",
                        "sequence": metadata.get("series_description"), "derived": bool(nifti_path),
                        "series_instance_uid": metadata.get("series_instance_uid"), "reference_voxels": 0})
                folder = image_directory(case)
                folder.mkdir(parents=True, exist_ok=True)
                sitk.WriteImage(image, str(folder / "image.nii.gz"), True)
                if series_id:
                    preserve_series(flat, series_id, folder / "original-dicom.zip")
                db.add(case)
                audit(db, user, "images_imported", case.id, {"project": project_id, "modality": case.modality})
                db.commit()
                imported.append(case.id)
            if not imported:
                raise HTTPException(422, "; ".join(errors) or "No supported series")
            return {"case_ids": imported, "warnings": errors}
        except (ValueError, RuntimeError) as error:
            raise HTTPException(422, str(error)) from error


@app.get("/api/studies/{study_id}/dicom/download")
def source_dicom(study_id: UUID, user=Depends(current_user)):
    with SessionLocal() as db:
        get_assignment(db, study_id, user)
        path = lung.store.dicom_archive_path(str(study_id))
        if not path.exists():
            raise HTTPException(404, "This volume has no original DICOM archive")
        audit(db, user, "dicom_exported", str(study_id))
        db.commit()
        return FileResponse(path, filename=f"dicom-{study_id}.zip")


class ReturnRequest(lung.VersionRequest):
    comment: str = Field(min_length=1, max_length=1000)


@app.post("/api/assignments/{study_id}/return")
def return_assignment(study_id: UUID, req: ReturnRequest, user=Depends(current_user)):
    if not can_review(user):
        raise HTTPException(403, "Reviewer access required")
    with SessionLocal() as db:
        assignment, _ = get_assignment(db, study_id, user)
        head = lung.study_head(db, study_id)
        lung.check_version(head, req.version)
        if state_for(db, assignment)["status"] not in ("completed", "approved"):
            raise HTTPException(409, "Only submitted work can be returned")
        db.info["annotation_actor"] = {**user_payload(user), "role": "reviewer", "comment": req.comment}
        audit(db, user, "changes_requested", str(study_id), {"comment": req.comment, "revision_id": head.revision_id})
        return lung.transition(db, head, "changes_requested", "in_progress")


class Comparison(BaseModel):
    a: UUID
    b: UUID


@app.post("/api/review/compare")
def compare(req: Comparison, user=Depends(current_user)):
    if not can_review(user):
        raise HTTPException(403, "Reviewer access required")
    with SessionLocal() as db:
        first, case = get_assignment(db, req.a, user)
        second, _ = get_assignment(db, req.b, user)
        if first.case_id != second.case_id or first.study_id == second.study_id:
            raise HTTPException(422, "Select two different readings of the same examination")
        if any(state_for(db, a)["status"] not in ("completed", "approved") for a in (first, second)):
            raise HTTPException(409, "Both readings must be submitted")
        image = lung.image_for(first.study_id)
        a = lung.current_mask(db.get(AnnotationHead, first.study_id), image) > 0
        b = lung.current_mask(db.get(AnnotationHead, second.study_id), image) > 0
        count_a, count_b = int(a.sum()), int(b.sum())
        if not count_a + count_b:
            raise HTTPException(422, "No lesion voxels to compare")
        return {"dice": 2 * int(np.count_nonzero(a & b)) / (count_a + count_b),
                "volume_difference_ml": float((count_a - count_b) * np.prod(image.GetSpacing()) / 1000),
                "notice": "Union-mask comparison only; not lesion matching, surface-distance benchmarking, or clinical validation. Practice reference imports may make agreement optimistic."}


class Assign(BaseModel):
    user_id: UUID | None = None


@app.post("/api/cases/{case_id}/assign")
def assign(case_id: UUID, req: Assign, user=Depends(current_user)):
    with SessionLocal() as db:
        case = db.get(WorkspaceCase, str(case_id))
        if not case:
            raise HTTPException(404, "Case not found")
        require_project(user, case.project_id)
        target = db.get(WorkspaceUser, str(req.user_id)) if req.user_id else user
        if not target:
            raise HTTPException(404, "Reader not found")
        if target.id != user.id and not can_review(user):
            raise HTTPException(403, "Only reviewers can allocate another reader")
        require_project(target, case.project_id)
        try:
            assignment = create_assignment(db, case, target)
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Assignment was created concurrently. Refresh the worklist.")
        audit(db, user, "assigned", assignment.study_id, {"reader": target.id})
        db.commit()
        return assignment_payload(db, assignment)


@app.post("/api/assignments/{study_id}/reference")
def use_reference(study_id: UUID, req: lung.VersionRequest, user=Depends(current_user)):
    with SessionLocal() as db:
        assignment, case = get_assignment(db, study_id, user)
        if assignment.user_id != user.id:
            raise HTTPException(403, "Only your own practice assignment can use the reference")
        if assignment.kind != "reader":
            raise HTTPException(409, "Reference import is not available for adjudication")
        reference = image_directory(case) / "reference.nii.gz"
        if not reference.exists():
            raise HTTPException(404, "No reference mask supplied")
        head = lung.study_head(db, study_id)
        lung.check_version(head, req.version)
        db.info["annotation_actor"] = user_payload(user)
        if head.revision_id:
            raise HTTPException(409, "Reference loading is available only on an empty new draft. Existing work is never replaced.")
        image = lung.image_for(str(study_id))
        mask = sitk.ReadImage(str(reference))
        lung.same_grid(mask, image)
        return lung.save_revision(db, head, image, sitk.GetArrayFromImage(mask), "reference-import",
                                  {"source": case.source, "practice_only": True})


@app.post("/api/assignments/{study_id}/adjudicate")
def adjudicate(study_id: UUID, user=Depends(current_user)):
    if not can_review(user):
        raise HTTPException(403, "Reviewer access required")
    with SessionLocal() as db:
        source, case = get_assignment(db, study_id, user)
        if state_for(db, source)["status"] not in ("completed", "approved"):
            raise HTTPException(409, "Only submitted work can be adjudicated")
        result = create_assignment(db, case, user, kind="adjudication", source=source)
        audit(db, user, "adjudication_started", result.study_id, {"source": source.study_id})
        db.commit()
        return assignment_payload(db, result)


class NewUser(BaseModel):
    username: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=200)
    role: str = Field(pattern=r"^(junior_annotator|senior_annotator|reviewer|admin)$")


@app.get("/api/users")
def users(user=Depends(current_user)):
    if not can_review(user):
        raise HTTPException(403, "Reviewer access required")
    with SessionLocal() as db:
        return [user_payload(row) for row in db.scalars(select(WorkspaceUser).order_by(WorkspaceUser.created_at))]


@app.post("/api/users", status_code=201)
def add_user(req: NewUser, user=Depends(current_user)):
    require_admin(user)
    with SessionLocal() as db:
        if db.scalar(select(WorkspaceUser).where(WorkspaceUser.username == req.username)):
            raise HTTPException(409, "Username already exists")
        created = WorkspaceUser(username=req.username, name=req.name, password_hash=password_hash(req.password), role=req.role, projects=user.projects)
        db.add(created)
        db.flush()
        audit(db, user, "user_created", created.id, {"role": created.role})
        db.commit()
        return user_payload(created)


@app.get("/api/audit")
def audit_log(user=Depends(current_user)):
    require_admin(user)
    with SessionLocal() as db:
        return [{"action": row.action, "subject_id": row.subject_id, "details": row.details,
                 "created_at": row.created_at.isoformat(), "user_id": row.user_id}
                for row in db.scalars(select(WorkspaceAudit).order_by(WorkspaceAudit.created_at.desc()).limit(100))]


def extract_features(job_id, slot):
    try:
        _extract_features(job_id)
    finally:
        slot.__exit__(None, None, None)


def _extract_features(job_id):
    with SessionLocal() as db:
        job = db.get(WorkspaceFeatureJob, job_id)
        try:
            job.status = "running"
            db.commit()
            from radiomics import featureextractor, __version__ as radiomics_version
            image_path = lung.store.image_path(job.study_id, "baseline")
            mask_path = lung.revision_path(job.study_id, job.revision_id)
            image, mask = sitk.ReadImage(str(image_path)), sitk.ReadImage(str(mask_path))
            lung.same_grid(mask, image)
            spacing = job.protocol["setting"].get("resampledPixelSpacing")
            if spacing and np.prod(np.asarray(image.GetSize()) * np.asarray(image.GetSpacing()) / spacing) > 20_000_000:
                raise ValueError("Requested resampling exceeds the demo memory limit. Use a coarser spacing or native grid.")
            extractor = featureextractor.RadiomicsFeatureExtractor({key: value for key, value in job.protocol.items() if key in ("setting", "imageType", "featureClass")})
            labels = np.unique(sitk.GetArrayViewFromImage(mask))
            rows = []
            for label in labels[labels > 0]:
                values = extractor.execute(image, mask, label=int(label))
                # PyRadiomics returns both Python scalars and zero-dimensional arrays.
                features = {key: float(value) for key, value in values.items()
                            if not key.startswith("diagnostics_") and np.asarray(value).ndim == 0
                            and np.isfinite(float(value))}
                diagnostics = {key: str(value) for key, value in values.items() if key.startswith("diagnostics_")}
                rows.append({"lesion_label": int(label), "features": features, "diagnostics": diagnostics})
            if not rows:
                raise ValueError("No nonempty lesion masks")
            job.result = {"rows": rows, "pyradiomics_version": radiomics_version,
                "image_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                "mask_sha256": hashlib.sha256(mask_path.read_bytes()).hexdigest(),
                "protocol_sha256": digest(json.dumps(job.protocol, sort_keys=True)),
                "revision_id": job.revision_id, "study_id": job.study_id,
                "disclaimer": "Demonstration features, not validated predictive biomarkers."}
            job.status = "succeeded"
        except Exception as error:
            job.status, job.error = "failed", f"{type(error).__name__}: {str(error)[:500]}"
        db.commit()


@app.post("/api/assignments/{study_id}/radiomics", status_code=202)
def run_features(study_id: UUID, req: FeatureRequest, user=Depends(current_user)):
    with _feature_slot, SessionLocal() as db:
        assignment, case = get_assignment(db, study_id, user)
        state = state_for(db, assignment)
        if state["status"] != "approved":
            raise HTTPException(409, "Independent approval is required before generating a feature file")
        head = lung.study_head(db, study_id)
        lung.check_version(head, req.version)
        if state["revision_id"] != str(req.revision_id):
            raise HTTPException(409, "The approved revision changed. Refresh before generating a file.")
        active = db.scalar(select(WorkspaceFeatureJob).where(WorkspaceFeatureJob.status.in_(["queued", "running"])))
        if active:
            raise HTTPException(409, "One extraction is already active. This demo runs one job at a time.")
        protocol = req.settings.protocol(case.modality)
        protocol["submission_status"] = state["status"]
        protocol["sequence"] = case.source.get("sequence")
        approval = latest_task_event(db, head)
        protocol["approval_event_id"] = approval.id
        protocol["approval_version"] = state["version"]
        protocol["reviewer"] = approval.actor
        job = WorkspaceFeatureJob(id=str(uuid4()), study_id=str(study_id), user_id=user.id,
                                  revision_id=state["revision_id"], protocol=protocol)
        slot = lung.worker_slot()
        slot.__enter__()
        try:
            lung.lock_head(db, head)
            db.add(job)
            audit(db, user, "feature_file_requested", str(study_id), {"revision_id": state["revision_id"], "protocol": protocol})
            db.commit()
            threading.Thread(target=extract_features, args=(job.id, slot), daemon=True).start()
        except Exception:
            slot.__exit__(None, None, None)
            raise
        return {"id": job.id, "status": job.status}


@app.get("/api/radiomics")
def feature_jobs(study_id: UUID | None = None, user=Depends(current_user)):
    with SessionLocal() as db:
        result = []
        query = select(WorkspaceFeatureJob).order_by(WorkspaceFeatureJob.created_at.desc())
        if study_id:
            get_assignment(db, study_id, user)
            query = query.where(WorkspaceFeatureJob.study_id == str(study_id))
        for job in db.scalars(query.limit(100)):
            try:
                assignment, case = get_assignment(db, job.study_id, user)
            except HTTPException:
                continue
            state = state_for(db, assignment)
            result.append({"id": job.id, "study_id": job.study_id, "patient_id": case.patient_id,
                "project_id": case.project_id, "status": job.status, "error": job.error,
                "revision_id": job.revision_id, "protocol": job.protocol,
                "stale": state["revision_id"] != job.revision_id or state["status"] != "approved"
                    or state["version"] != job.protocol.get("approval_version"),
                "result": job.result, "created_at": job.created_at.isoformat()})
        return result


@app.get("/api/radiomics/{job_id}/export")
def feature_export(job_id: UUID, format: str = "csv", user=Depends(current_user)):
    if format not in ("csv", "json", "zip"):
        raise HTTPException(422, "Supported formats are csv, json and zip")
    with SessionLocal() as db:
        job = db.get(WorkspaceFeatureJob, str(job_id))
        if not job:
            raise HTTPException(404, "Run not found")
        assignment, case = get_assignment(db, job.study_id, user)
        state = state_for(db, assignment)
        if state["status"] != "approved" or state["revision_id"] != job.revision_id or state["version"] != job.protocol.get("approval_version"):
            raise HTTPException(409, "This file's approval is no longer current. Generate a file from the current approved revision.")
        if job.status != "succeeded":
            raise HTTPException(409, "Extraction is not complete")
        audit(db, user, "radiomics_exported", job.id)
        db.commit()
        manifest = {**job.result, "protocol": job.protocol, "source": case.source}
        if format == "json":
            return JSONResponse(manifest, headers={"Content-Disposition": f'attachment; filename="features-{job.id}.json"'})
        rows = [{"patient_id": case.patient_id, "study_id": job.study_id, "revision_id": job.revision_id,
                 "protocol": job.protocol["id"], "lesion_label": row["lesion_label"], **row["features"]} for row in job.result["rows"]]
        fields = list(dict.fromkeys(key for row in rows for key in row))
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value
                          for key, value in row.items()} for row in rows)
        if format == "zip":
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
                package.writestr("features.csv", output.getvalue())
                package.writestr("manifest.json", json.dumps(manifest, indent=2))
                package.writestr("pyradiomics-settings.json", json.dumps({key: job.protocol[key] for key in ("setting", "imageType", "featureClass")}, indent=2))
            return Response(archive.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="features-{job.id}.zip"'})
        return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="features-{job.id}.csv"'})


@app.get("/")
def home():
    return FileResponse(ROOT / "app/static/oncometra.html")


@app.get("/viewer")
def viewer(user=Depends(current_user)):
    return FileResponse(ROOT / "app/static/annotate.html")


app.include_router(lung.router)
app.mount("/assets", StaticFiles(directory=ROOT / "app/static"), name="assets")


@app.get("/{filename}")
def viewer_asset(filename: str):
    if filename not in {"annotate.css", "annotate.js", "import-files.mjs", "viewer-controls.mjs", "annotation-workflow.mjs", "nodule-controls.mjs", "mask-editing.mjs"}:
        raise HTTPException(404, "Not found")
    return FileResponse(ROOT / "app/static" / filename)
