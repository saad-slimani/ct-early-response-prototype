from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    AiArtifact,
    AiTask,
    Appointment,
    BillingItem,
    Patient,
    Report,
    StudyRecord,
    TrainingRun,
    ViewerMeasurement,
    WorklistItem,
    AnnotationHead,
    AnnotationReviewEvent,
    AnnotationTaskEvent,
)
from app.services.annotation_review import task_status, public_review


class WorkflowService:
    def _make_accession(self, study_id: str) -> str:
        stamp = datetime.utcnow().strftime("%Y%m%d")
        return f"ACC-{stamp}-{study_id[:8].upper()}"

    def bootstrap_study(
        self,
        db: Session,
        *,
        study_id: str,
        patient_identifier: str,
        patient_name: Optional[str],
        accession_number: Optional[str],
        modality: str,
        body_part: str,
        description: Optional[str],
        dicom_study_uid: Optional[str] = None,
        archive_source: str = "local-upload",
        priority: str,
        indication: Optional[str],
        assignee: Optional[str],
        baseline_path: str,
        followup_path: str,
    ) -> StudyRecord:
        patient = db.execute(select(Patient).where(Patient.patient_id == patient_identifier)).scalar_one_or_none()
        if patient is None:
            patient = Patient(patient_id=patient_identifier, display_name=patient_name or None)
            db.add(patient)
            db.flush()
        elif patient_name:
            patient.display_name = patient_name

        study = StudyRecord(
            id=study_id,
            patient=patient,
            accession_number=accession_number or self._make_accession(study_id),
            modality=modality or "CT",
            body_part=body_part or "Chest",
            description=description or f"{modality or 'Imaging'} study",
            dicom_study_uid=dicom_study_uid or None,
            archive_source=archive_source or "local-upload",
            baseline_path=baseline_path,
            followup_path=followup_path,
            status="ready_to_read",
            updated_at=datetime.utcnow(),
        )
        db.add(study)
        db.flush()

        db.add(
            WorklistItem(
                study=study,
                status="ready_to_read",
                priority=priority,
                assignee=assignee or None,
                report_state="empty",
                ai_state="not_requested",
                updated_at=datetime.utcnow(),
            )
        )
        db.add(
            Report(
                study=study,
                status="draft",
                indication=indication or "",
                findings="",
                impression="",
                updated_at=datetime.utcnow(),
            )
        )
        db.commit()
        return self.get_study(db, study_id)

    def get_study(self, db: Session, study_id: str) -> StudyRecord:
        stmt = (
            select(StudyRecord)
            .where(StudyRecord.id == study_id)
            .options(
                selectinload(StudyRecord.patient),
                selectinload(StudyRecord.worklist_item),
                selectinload(StudyRecord.report),
                selectinload(StudyRecord.ai_tasks).selectinload(AiTask.artifacts),
                selectinload(StudyRecord.ai_artifacts),
                selectinload(StudyRecord.appointments),
                selectinload(StudyRecord.billing_items),
                selectinload(StudyRecord.measurements),
            )
        )
        study = db.execute(stmt).scalar_one_or_none()
        if study is None:
            raise FileNotFoundError(f"Unknown study_id: {study_id}")
        return study

    def list_worklist(self, db: Session, status: Optional[str] = None, assignee: Optional[str] = None) -> List[Dict[str, Any]]:
        stmt = (
            select(WorklistItem)
            .options(
                selectinload(WorklistItem.study).selectinload(StudyRecord.patient),
                selectinload(WorklistItem.study).selectinload(StudyRecord.report),
            )
            .order_by(WorklistItem.updated_at.desc())
        )
        if status:
            stmt = stmt.where(WorklistItem.status == status)
        if assignee:
            stmt = stmt.where(WorklistItem.assignee == assignee)
        rows = db.execute(stmt).scalars().all()
        # Annotation task state stays independent of RIS/report signing.
        heads = {head.study_id: head for head in db.execute(select(AnnotationHead)).scalars()}
        confirmed = set(db.execute(select(AnnotationHead.study_id).join(AnnotationReviewEvent,
            (AnnotationReviewEvent.study_id == AnnotationHead.study_id) &
            (AnnotationReviewEvent.version == AnnotationHead.version) &
            (AnnotationReviewEvent.revision_id == AnnotationHead.revision_id)
        ).where(AnnotationReviewEvent.action == "confirmed")).scalars())
        events = {}
        for event in db.execute(select(AnnotationTaskEvent).order_by(AnnotationTaskEvent.version.desc())).scalars():
            events.setdefault(event.study_id, event)
        items = []
        for row in rows:
            item = self.serialize_worklist_item(row)
            head = heads.get(row.study_id)
            event = events.get(row.study_id)
            item["annotation_status"] = task_status(head, event, row.study_id in confirmed)
            item["annotation_event"] = public_review(event) if event else None
            item["annotation_revision_id"] = head.revision_id if head else None
            items.append(item)
        return items

    def update_worklist(
        self,
        db: Session,
        study_id: str,
        *,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        assignee: Optional[str] = None,
        report_state: Optional[str] = None,
        ai_state: Optional[str] = None,
    ) -> WorklistItem:
        study = self.get_study(db, study_id)
        item = study.worklist_item
        if item is None:
            item = WorklistItem(study=study, updated_at=datetime.utcnow())
            db.add(item)
        if status is not None:
            item.status = status
            study.status = status
        if priority is not None:
            item.priority = priority
        if assignee is not None:
            item.assignee = assignee or None
        if report_state is not None:
            item.report_state = report_state
        if ai_state is not None:
            item.ai_state = ai_state
        item.updated_at = datetime.utcnow()
        study.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(item)
        return item

    def get_or_create_report(self, db: Session, study_id: str) -> Report:
        study = self.get_study(db, study_id)
        report = study.report
        if report is None:
            report = Report(study=study, status="draft", indication="", findings="", impression="", updated_at=datetime.utcnow())
            db.add(report)
            db.commit()
            db.refresh(report)
        return report

    def upsert_report(
        self,
        db: Session,
        study_id: str,
        *,
        indication: Optional[str] = None,
        findings: Optional[str] = None,
        impression: Optional[str] = None,
        status: Optional[str] = None,
        signed_by: Optional[str] = None,
    ) -> Report:
        report = self.get_or_create_report(db, study_id)
        if indication is not None:
            report.indication = indication
        if findings is not None:
            report.findings = findings
        if impression is not None:
            report.impression = impression
        if status is not None:
            report.status = status
        if signed_by is not None:
            report.signed_by = signed_by or None
        if report.status == "final":
            report.signed_at = datetime.utcnow()
            self.update_worklist(db, study_id, status="signed", report_state="final")
        else:
            worklist_state = "draft" if (report.findings or report.impression or report.indication) else "empty"
            self.update_worklist(db, study_id, status="in_progress", report_state=worklist_state)
        report.updated_at = datetime.utcnow()
        db.add(report)
        db.commit()
        db.refresh(report)
        return report

    def append_ai_result_to_report(
        self,
        db: Session,
        study_id: str,
        *,
        findings_snippet: Optional[str] = None,
        impression_snippet: Optional[str] = None,
    ) -> Report:
        report = self.get_or_create_report(db, study_id)
        if findings_snippet:
            report.findings = self._append_block(report.findings, findings_snippet)
        if impression_snippet:
            report.impression = self._append_block(report.impression, impression_snippet)
        report.updated_at = datetime.utcnow()
        db.add(report)
        self.update_worklist(db, study_id, status="awaiting_ai_review", report_state="draft")
        db.commit()
        db.refresh(report)
        return report

    def create_ai_task(
        self,
        db: Session,
        study_id: str,
        *,
        task_type: str,
        model_id: str,
        prompt: Optional[str],
        status: str = "requested",
    ) -> AiTask:
        self.get_study(db, study_id)
        task = AiTask(
            study_id=study_id,
            task_type=task_type,
            model_id=model_id,
            prompt=prompt or "",
            status=status,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(task)
        self.update_worklist(db, study_id, ai_state="running")
        db.commit()
        db.refresh(task)
        return task

    def complete_ai_task(
        self,
        db: Session,
        task: AiTask,
        *,
        output_summary: str,
        result_payload: Dict[str, Any],
        artifacts: List[Dict[str, Any]],
        ai_state: str = "ready_for_review",
    ) -> AiTask:
        task.status = "completed"
        task.output_summary = output_summary
        task.result_payload = result_payload
        task.updated_at = datetime.utcnow()
        db.add(task)
        for artifact in artifacts:
            db.add(
                AiArtifact(
                    study_id=task.study_id,
                    task=task,
                    artifact_type=artifact["artifact_type"],
                    label=artifact["label"],
                    storage_ref=artifact.get("storage_ref"),
                    dicom_reference=artifact.get("dicom_reference"),
                    report_snippet=artifact.get("report_snippet"),
                    accepted=artifact.get("accepted", False),
                    accepted_by=artifact.get("accepted_by"),
                    provenance=artifact.get("provenance"),
                )
            )
        self.update_worklist(db, task.study_id, status="awaiting_ai_review", ai_state=ai_state)
        db.commit()
        db.refresh(task)
        return task

    def fail_ai_task(self, db: Session, task: AiTask, detail: str) -> AiTask:
        task.status = "failed"
        task.output_summary = detail
        task.updated_at = datetime.utcnow()
        db.add(task)
        self.update_worklist(db, task.study_id, ai_state="not_requested")
        db.commit()
        db.refresh(task)
        return task

    def list_ai_tasks(self, db: Session, study_id: str) -> List[Dict[str, Any]]:
        study = self.get_study(db, study_id)
        ordered = sorted(study.ai_tasks, key=lambda row: row.created_at, reverse=True)
        return [self.serialize_ai_task(task) for task in ordered]

    def latest_result_by_type(self, db: Session, study_id: str, artifact_type: str) -> Optional[AiArtifact]:
        stmt = (
            select(AiArtifact)
            .where(AiArtifact.study_id == study_id, AiArtifact.artifact_type == artifact_type)
            .order_by(AiArtifact.created_at.desc())
        )
        return db.execute(stmt).scalars().first()

    def create_training_run(
        self,
        db: Session,
        *,
        base_model_id: str,
        dataset_name: str,
        dataset_source: str,
        execution_target: str,
        notes: Optional[str],
    ) -> TrainingRun:
        run = TrainingRun(
            base_model_id=base_model_id,
            dataset_name=dataset_name,
            dataset_source=dataset_source,
            execution_target=execution_target,
            status="planned",
            notes=notes or "",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    def list_training_runs(self, db: Session) -> List[Dict[str, Any]]:
        stmt = select(TrainingRun).order_by(TrainingRun.created_at.desc())
        rows = db.execute(stmt).scalars().all()
        return [self.serialize_training_run(run) for run in rows]

    def create_appointment(
        self,
        db: Session,
        *,
        study_id: Optional[str],
        patient_id: str,
        patient_name: Optional[str],
        modality: str,
        procedure: str,
        scheduled_for: Optional[datetime],
        room: Optional[str],
        ordering_provider: Optional[str],
        status: str,
        notes: Optional[str],
    ) -> Appointment:
        if study_id:
            self.get_study(db, study_id)
        row = Appointment(
            study_id=study_id or None,
            patient_id=patient_id,
            patient_name=patient_name or None,
            modality=modality or "CT",
            procedure=procedure or "Imaging study",
            scheduled_for=scheduled_for,
            room=room or None,
            ordering_provider=ordering_provider or None,
            status=status or "scheduled",
            notes=notes or "",
            updated_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def list_appointments(self, db: Session) -> List[Dict[str, Any]]:
        stmt = select(Appointment).order_by(Appointment.scheduled_for.asc(), Appointment.created_at.desc())
        rows = db.execute(stmt).scalars().all()
        ordered = sorted(rows, key=lambda row: (row.scheduled_for is None, row.scheduled_for or datetime.max, row.created_at), reverse=False)
        return [self.serialize_appointment(row) for row in ordered]

    def create_billing_item(
        self,
        db: Session,
        *,
        study_id: Optional[str],
        patient_id: str,
        accession_number: Optional[str],
        cpt_code: str,
        description: str,
        payer: Optional[str],
        amount_cents: int,
        status: str,
    ) -> BillingItem:
        if study_id:
            self.get_study(db, study_id)
        row = BillingItem(
            study_id=study_id or None,
            patient_id=patient_id,
            accession_number=accession_number or None,
            cpt_code=cpt_code or "IMG",
            description=description or "Imaging interpretation",
            payer=payer or None,
            amount_cents=max(0, int(amount_cents or 0)),
            status=status or "draft",
            updated_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def list_billing_items(self, db: Session) -> List[Dict[str, Any]]:
        stmt = select(BillingItem).order_by(BillingItem.created_at.desc())
        rows = db.execute(stmt).scalars().all()
        return [self.serialize_billing_item(row) for row in rows]

    def create_measurement(
        self,
        db: Session,
        *,
        study_id: str,
        timepoint: str,
        plane: str,
        slice_index: int,
        measurement_type: str,
        label: str,
        points: List[Dict[str, float]],
        value_mm: Optional[float],
    ) -> ViewerMeasurement:
        self.get_study(db, study_id)
        row = ViewerMeasurement(
            study_id=study_id,
            timepoint=timepoint,
            plane=plane,
            slice_index=slice_index,
            measurement_type=measurement_type or "length",
            label=label or "Measurement",
            points=points,
            value_mm=value_mm,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def list_measurements(self, db: Session, study_id: str) -> List[Dict[str, Any]]:
        self.get_study(db, study_id)
        stmt = (
            select(ViewerMeasurement)
            .where(ViewerMeasurement.study_id == study_id)
            .order_by(ViewerMeasurement.created_at.desc())
        )
        rows = db.execute(stmt).scalars().all()
        return [self.serialize_measurement(row) for row in rows]

    def serialize_workspace(
        self,
        study: StudyRecord,
        *,
        file_meta: Dict[str, Any],
        integrations: Dict[str, Any],
        viewer_context: Dict[str, Optional[str]],
    ) -> Dict[str, Any]:
        ai_tasks = sorted(study.ai_tasks, key=lambda row: row.created_at, reverse=True)
        artifacts = sorted(study.ai_artifacts, key=lambda row: row.created_at, reverse=True)
        return {
            "study": self.serialize_study(study, file_meta=file_meta),
            "worklist": self.serialize_worklist_item(study.worklist_item),
            "report": self.serialize_report(study.report),
            "ai_tasks": [self.serialize_ai_task(task) for task in ai_tasks],
            "ai_artifacts": [self.serialize_ai_artifact(artifact) for artifact in artifacts],
            "appointments": [self.serialize_appointment(row) for row in sorted(study.appointments, key=lambda row: row.created_at, reverse=True)],
            "billing_items": [self.serialize_billing_item(row) for row in sorted(study.billing_items, key=lambda row: row.created_at, reverse=True)],
            "measurements": [self.serialize_measurement(row) for row in sorted(study.measurements, key=lambda row: row.created_at, reverse=True)],
            "integrations": integrations,
            "viewer_context": viewer_context,
        }

    def serialize_study(self, study: StudyRecord, *, file_meta: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "study_id": study.id,
            "patient_id": study.patient.patient_id if study.patient else None,
            "patient_name": study.patient.display_name if study.patient else None,
            "accession_number": study.accession_number,
            "modality": study.modality,
            "body_part": study.body_part,
            "description": study.description,
            "status": study.status,
            "archive_source": study.archive_source,
            "dicom_study_uid": study.dicom_study_uid,
            "file_meta": file_meta,
            "created_at": study.created_at.isoformat(),
            "updated_at": study.updated_at.isoformat(),
        }

    def serialize_worklist_item(self, item: Optional[WorklistItem]) -> Optional[Dict[str, Any]]:
        if item is None:
            return None
        study = item.study
        patient = study.patient if study else None
        report = study.report if study else None
        return {
            "study_id": item.study_id,
            "status": item.status,
            "priority": item.priority,
            "assignee": item.assignee,
            "report_state": item.report_state,
            "ai_state": item.ai_state,
            "patient_id": patient.patient_id if patient else None,
            "patient_name": patient.display_name if patient else None,
            "accession_number": study.accession_number if study else None,
            "body_part": study.body_part if study else None,
            "modality": study.modality if study else None,
            "description": study.description if study else None,
            "report_status": report.status if report else "draft",
            "updated_at": item.updated_at.isoformat(),
        }

    def serialize_report(self, report: Optional[Report]) -> Optional[Dict[str, Any]]:
        if report is None:
            return None
        return {
            "study_id": report.study_id,
            "status": report.status,
            "template_name": report.template_name,
            "indication": report.indication or "",
            "findings": report.findings or "",
            "impression": report.impression or "",
            "signed_by": report.signed_by,
            "signed_at": report.signed_at.isoformat() if report.signed_at else None,
            "updated_at": report.updated_at.isoformat(),
        }

    def serialize_ai_task(self, task: AiTask) -> Dict[str, Any]:
        ordered = sorted(task.artifacts, key=lambda row: row.created_at, reverse=True)
        return {
            "id": task.id,
            "study_id": task.study_id,
            "task_type": task.task_type,
            "model_id": task.model_id,
            "prompt": task.prompt or "",
            "status": task.status,
            "output_summary": task.output_summary,
            "result_payload": task.result_payload,
            "artifacts": [self.serialize_ai_artifact(artifact) for artifact in ordered],
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
        }

    def serialize_ai_artifact(self, artifact: AiArtifact) -> Dict[str, Any]:
        return {
            "id": artifact.id,
            "study_id": artifact.study_id,
            "ai_task_id": artifact.ai_task_id,
            "artifact_type": artifact.artifact_type,
            "label": artifact.label,
            "storage_ref": artifact.storage_ref,
            "dicom_reference": artifact.dicom_reference,
            "report_snippet": artifact.report_snippet,
            "accepted": artifact.accepted,
            "accepted_by": artifact.accepted_by,
            "provenance": artifact.provenance,
            "created_at": artifact.created_at.isoformat(),
        }

    def serialize_training_run(self, run: TrainingRun) -> Dict[str, Any]:
        return {
            "id": run.id,
            "base_model_id": run.base_model_id,
            "dataset_name": run.dataset_name,
            "dataset_source": run.dataset_source,
            "execution_target": run.execution_target,
            "status": run.status,
            "notes": run.notes or "",
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
        }

    def serialize_appointment(self, row: Appointment) -> Dict[str, Any]:
        return {
            "id": row.id,
            "study_id": row.study_id,
            "patient_id": row.patient_id,
            "patient_name": row.patient_name,
            "modality": row.modality,
            "procedure": row.procedure,
            "scheduled_for": row.scheduled_for.isoformat() if row.scheduled_for else None,
            "room": row.room,
            "ordering_provider": row.ordering_provider,
            "status": row.status,
            "notes": row.notes or "",
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    def serialize_billing_item(self, row: BillingItem) -> Dict[str, Any]:
        return {
            "id": row.id,
            "study_id": row.study_id,
            "patient_id": row.patient_id,
            "accession_number": row.accession_number,
            "cpt_code": row.cpt_code,
            "description": row.description,
            "payer": row.payer,
            "amount_cents": row.amount_cents,
            "status": row.status,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    def serialize_measurement(self, row: ViewerMeasurement) -> Dict[str, Any]:
        return {
            "id": row.id,
            "study_id": row.study_id,
            "timepoint": row.timepoint,
            "plane": row.plane,
            "slice_index": row.slice_index,
            "measurement_type": row.measurement_type,
            "label": row.label,
            "points": row.points,
            "value_mm": row.value_mm,
            "created_at": row.created_at.isoformat(),
        }

    def _append_block(self, existing: Optional[str], snippet: str) -> str:
        base = (existing or "").strip()
        return f"{base}\n\n{snippet}".strip() if base else snippet.strip()
