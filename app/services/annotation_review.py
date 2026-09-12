"""Revision-bound research review state, separate from RIS/report sign-off."""

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AnnotationReviewEvent, AnnotationTaskEvent


# Self-selected test personas, deliberately not represented as authentication.
TEST_IDENTITIES = {
    "reviewer": {"id": "reviewer", "name": "Saad Slimani", "role": "reviewer"},
    "junior": {"id": "junior", "name": "Test junior", "role": "junior_annotator"},
    "senior": {"id": "senior", "name": "Test senior", "role": "senior_annotator"},
}


def annotation_db(db: Session = Depends(get_db), x_annotation_actor: str = Header(default="reviewer")):
    if x_annotation_actor not in TEST_IDENTITIES:
        raise HTTPException(400, "Unknown test identity")
    db.info["annotation_actor"] = dict(TEST_IDENTITIES[x_annotation_actor])
    return db


def actor_for(db):
    return dict(db.info.get("annotation_actor", TEST_IDENTITIES["reviewer"]))


def require_reviewer(db):
    if actor_for(db)["role"] != "reviewer":
        raise HTTPException(403, "Only the reviewer can approve a task or reopen an approved task")


def latest_task_event(db, head):
    return db.execute(select(AnnotationTaskEvent).where(
        AnnotationTaskEvent.study_id == head.study_id,
    ).order_by(AnnotationTaskEvent.version.desc()).limit(1)).scalar_one_or_none()


def task_status(head, event=None, legacy_confirmation=None):
    if event:
        if event.version == head.version and event.revision_id == head.revision_id:
            return event.status
        return "in_progress"
    if legacy_confirmation:
        # Old local confirmations did not have reviewer attribution. Never promote to approved.
        return "completed"
    return "in_progress" if head and head.revision_id else "new"


def task_permissions(db, status):
    reviewer = actor_for(db)["role"] == "reviewer"
    editable = not db.info.get("read_only_annotation", False)
    independent = db.info.get("independent_approval", True)
    return {"annotate": editable and status in ("new", "in_progress"),
            "complete": editable and status in ("new", "in_progress"),
            "approve": reviewer and independent and status == "completed",
            "reopen": status == "completed" or reviewer and status == "approved",
            "undo_approval": reviewer and status == "approved"}


def active_confirmation(db, head):
    event = latest_task_event(db, head)
    if event:
        return event if task_status(head, event) in ("completed", "approved") else None
    return db.execute(select(AnnotationReviewEvent).where(
        AnnotationReviewEvent.study_id == head.study_id,
        AnnotationReviewEvent.version == head.version,
        AnnotationReviewEvent.revision_id == head.revision_id,
        AnnotationReviewEvent.action == "confirmed",
    )).scalar_one_or_none()


def public_review(event):
    return {"id": event.id, "action": event.action, "revision_id": event.revision_id,
            "version": event.version, "created_at": event.created_at.isoformat() + "Z",
            "status": getattr(event, "status", "completed" if event.action == "confirmed" else "in_progress"),
            "actor": getattr(event, "actor", None), "identity_mode": (getattr(event, "actor", None) or {}).get("identity_mode", "self_selected_test")}
