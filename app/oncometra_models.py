"""Workspace metadata separated from the existing image/annotation engine."""

from datetime import datetime
from uuid import uuid4
from sqlalchemy import Column, String, DateTime, JSON, ForeignKey, UniqueConstraint, Text
from app.db import Base


def uid():
    return str(uuid4())


class WorkspaceUser(Base):
    __tablename__ = "workspace_users"
    id = Column(String(36), primary_key=True, default=uid)
    username = Column(String(80), unique=True, nullable=False)
    name = Column(String(120), nullable=False)
    password_hash = Column(Text, nullable=False)
    role = Column(String(30), nullable=False)
    projects = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class WorkspaceSession(Base):
    __tablename__ = "workspace_sessions"
    token_hash = Column(String(64), primary_key=True)
    user_id = Column(String(36), ForeignKey("workspace_users.id"), nullable=False)
    expires_at = Column(DateTime, nullable=False)


class WorkspaceCase(Base):
    __tablename__ = "workspace_cases"
    id = Column(String(36), primary_key=True, default=uid)
    project_id = Column(String(40), index=True, nullable=False)
    patient_id = Column(String(100), nullable=False)
    modality = Column(String(8), nullable=False)
    source = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("project_id", "patient_id"),)


class WorkspaceAssignment(Base):
    __tablename__ = "workspace_assignments"
    study_id = Column(String(36), ForeignKey("studies.id"), primary_key=True)
    case_id = Column(String(36), ForeignKey("workspace_cases.id"), index=True, nullable=False)
    user_id = Column(String(36), ForeignKey("workspace_users.id"), index=True, nullable=False)
    kind = Column(String(20), default="reader", nullable=False)
    provenance = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("case_id", "user_id", "kind"),)


class WorkspaceFeatureJob(Base):
    __tablename__ = "workspace_feature_jobs"
    id = Column(String(36), primary_key=True, default=uid)
    study_id = Column(String(36), ForeignKey("studies.id"), index=True, nullable=False)
    user_id = Column(String(36), nullable=False)
    revision_id = Column(String(36), nullable=False)
    status = Column(String(20), default="queued", nullable=False)
    protocol = Column(JSON, nullable=False)
    result = Column(JSON)
    error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class WorkspaceAudit(Base):
    __tablename__ = "workspace_audit"
    id = Column(String(36), primary_key=True, default=uid)
    user_id = Column(String(36), nullable=False)
    action = Column(String(60), nullable=False)
    subject_id = Column(String(36))
    details = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
