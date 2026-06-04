from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Patient(Base):
    __tablename__ = "patients"

    id = Column(String(36), primary_key=True, default=_uuid)
    patient_id = Column(String(64), unique=True, index=True, nullable=False)
    display_name = Column(String(120), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    studies = relationship("StudyRecord", back_populates="patient", cascade="all, delete-orphan")


class StudyRecord(Base):
    __tablename__ = "studies"

    id = Column(String(36), primary_key=True)
    patient_pk = Column(String(36), ForeignKey("patients.id"), nullable=False, index=True)
    accession_number = Column(String(64), unique=True, index=True, nullable=False)
    modality = Column(String(16), nullable=False, default="CT")
    body_part = Column(String(64), nullable=False, default="Chest")
    description = Column(String(200), nullable=False, default="Baseline and follow-up CT")
    archive_source = Column(String(32), nullable=False, default="local-upload")
    dicom_study_uid = Column(String(128), nullable=True)
    baseline_path = Column(String(255), nullable=True)
    followup_path = Column(String(255), nullable=True)
    status = Column(String(32), nullable=False, default="new")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    patient = relationship("Patient", back_populates="studies")
    worklist_item = relationship(
        "WorklistItem",
        back_populates="study",
        uselist=False,
        cascade="all, delete-orphan",
    )
    report = relationship(
        "Report",
        back_populates="study",
        uselist=False,
        cascade="all, delete-orphan",
    )
    ai_tasks = relationship("AiTask", back_populates="study", cascade="all, delete-orphan")
    ai_artifacts = relationship("AiArtifact", back_populates="study", cascade="all, delete-orphan")
    appointments = relationship("Appointment", back_populates="study", cascade="all, delete-orphan")
    billing_items = relationship("BillingItem", back_populates="study", cascade="all, delete-orphan")
    measurements = relationship("ViewerMeasurement", back_populates="study", cascade="all, delete-orphan")


class WorklistItem(Base):
    __tablename__ = "worklist_items"

    id = Column(String(36), primary_key=True, default=_uuid)
    study_id = Column(String(36), ForeignKey("studies.id"), unique=True, nullable=False, index=True)
    status = Column(String(32), nullable=False, default="ready_to_read")
    priority = Column(String(16), nullable=False, default="routine")
    assignee = Column(String(120), nullable=True)
    report_state = Column(String(32), nullable=False, default="empty")
    ai_state = Column(String(32), nullable=False, default="not_requested")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    study = relationship("StudyRecord", back_populates="worklist_item")


class Report(Base):
    __tablename__ = "reports"

    id = Column(String(36), primary_key=True, default=_uuid)
    study_id = Column(String(36), ForeignKey("studies.id"), unique=True, nullable=False, index=True)
    status = Column(String(32), nullable=False, default="draft")
    template_name = Column(String(64), nullable=False, default="general-ct")
    indication = Column(Text, nullable=True)
    findings = Column(Text, nullable=True)
    impression = Column(Text, nullable=True)
    signed_by = Column(String(120), nullable=True)
    signed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    study = relationship("StudyRecord", back_populates="report")


class AiTask(Base):
    __tablename__ = "ai_tasks"

    id = Column(String(36), primary_key=True, default=_uuid)
    study_id = Column(String(36), ForeignKey("studies.id"), nullable=False, index=True)
    task_type = Column(String(32), nullable=False)
    model_id = Column(String(120), nullable=False)
    prompt = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="requested")
    output_summary = Column(Text, nullable=True)
    result_payload = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    study = relationship("StudyRecord", back_populates="ai_tasks")
    artifacts = relationship("AiArtifact", back_populates="task", cascade="all, delete-orphan")


class AiArtifact(Base):
    __tablename__ = "ai_artifacts"

    id = Column(String(36), primary_key=True, default=_uuid)
    study_id = Column(String(36), ForeignKey("studies.id"), nullable=False, index=True)
    ai_task_id = Column(String(36), ForeignKey("ai_tasks.id"), nullable=True, index=True)
    artifact_type = Column(String(32), nullable=False)
    label = Column(String(120), nullable=False)
    storage_ref = Column(String(255), nullable=True)
    dicom_reference = Column(String(255), nullable=True)
    report_snippet = Column(Text, nullable=True)
    accepted = Column(Boolean, nullable=False, default=False)
    accepted_by = Column(String(120), nullable=True)
    provenance = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    study = relationship("StudyRecord", back_populates="ai_artifacts")
    task = relationship("AiTask", back_populates="artifacts")


class TrainingRun(Base):
    __tablename__ = "training_runs"

    id = Column(String(36), primary_key=True, default=_uuid)
    base_model_id = Column(String(120), nullable=False)
    dataset_name = Column(String(120), nullable=False)
    dataset_source = Column(String(120), nullable=False, default="accepted-segmentations")
    execution_target = Column(String(64), nullable=False, default="local-gpu")
    status = Column(String(32), nullable=False, default="planned")
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(String(36), primary_key=True, default=_uuid)
    study_id = Column(String(36), ForeignKey("studies.id"), nullable=True, index=True)
    patient_id = Column(String(64), nullable=False, index=True)
    patient_name = Column(String(120), nullable=True)
    modality = Column(String(16), nullable=False, default="CT")
    procedure = Column(String(160), nullable=False, default="Imaging study")
    scheduled_for = Column(DateTime, nullable=True, index=True)
    room = Column(String(80), nullable=True)
    ordering_provider = Column(String(120), nullable=True)
    status = Column(String(32), nullable=False, default="scheduled")
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    study = relationship("StudyRecord", back_populates="appointments")


class BillingItem(Base):
    __tablename__ = "billing_items"

    id = Column(String(36), primary_key=True, default=_uuid)
    study_id = Column(String(36), ForeignKey("studies.id"), nullable=True, index=True)
    patient_id = Column(String(64), nullable=False, index=True)
    accession_number = Column(String(64), nullable=True, index=True)
    cpt_code = Column(String(32), nullable=False, default="IMG")
    description = Column(String(200), nullable=False, default="Imaging interpretation")
    payer = Column(String(120), nullable=True)
    amount_cents = Column(Integer, nullable=False, default=0)
    status = Column(String(32), nullable=False, default="draft")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    study = relationship("StudyRecord", back_populates="billing_items")


class ViewerMeasurement(Base):
    __tablename__ = "viewer_measurements"

    id = Column(String(36), primary_key=True, default=_uuid)
    study_id = Column(String(36), ForeignKey("studies.id"), nullable=False, index=True)
    timepoint = Column(String(32), nullable=False, default="baseline")
    plane = Column(String(16), nullable=False, default="axial")
    slice_index = Column(Integer, nullable=False, default=0)
    measurement_type = Column(String(32), nullable=False, default="length")
    label = Column(String(120), nullable=False, default="Measurement")
    points = Column(JSON, nullable=False)
    value_mm = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    study = relationship("StudyRecord", back_populates="measurements")
