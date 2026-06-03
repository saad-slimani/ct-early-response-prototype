# MVP Scaffold

This repo now scaffolds a small AI-native imaging workspace rather than only a segmentation demo.

## What Exists Now

### Database-Backed Workflow

- SQLAlchemy models for `Patient`, `StudyRecord`, `WorklistItem`, `Report`, `AiTask`, `AiArtifact`, and `TrainingRun`
- `DATABASE_URL` support for PostgreSQL in production
- SQLite fallback for local bootstrapping

### Imaging + Workflow APIs

- `POST /api/studies` uploads baseline and follow-up CT and creates a worklist/report workspace
- `GET /api/worklist` lists the reading queue
- `GET /api/studies/{id}/workspace` returns study, worklist, report, AI, and integration state
- `PATCH /api/studies/{id}/worklist` updates worklist status, priority, and assignee
- `GET|PUT /api/studies/{id}/report` reads and writes the draft report
- `GET|POST /api/studies/{id}/ai/tasks` lists and runs AI tasks
- `GET /api/models` returns the indexed model catalog
- `GET|POST /api/training-runs` tracks local fine-tune requests

### AI Task Types

- `segmentation`: runs the current segmentation adapter and records AI artifacts
- `early_response`: computes the current 0-100 score and can insert it into the report
- `report_draft`: generates a reviewable draft from available metadata and prior AI outputs

### Integration Scaffolding

- Orthanc / DICOMweb config surface
- OHIF launch surface
- Epic SMART on FHIR config surface

These are config-aware scaffolds, not full external integrations yet.

## Environment Variables

- `DATABASE_URL`
  - Production example: `postgresql+psycopg://user:pass@host:5432/dbname`
- `APP_STORAGE_ROOT`
  - Where studies, masks, models, and the default SQLite file live
- `ORTHANC_URL`
- `DICOMWEB_BASE_URL`
- `OHIF_BASE_URL`
- `SMART_FHIR_ISSUER`
- `SMART_CLIENT_ID`
- `EPIC_BASE_URL`
- `FHIRCAST_HUB_URL`

## What Is Still Scaffold-Level

- No real DICOM archive ingestion flow yet
- No DICOM SEG export yet
- No true report-generation foundation model yet
- No async queue or worker split yet
- No authentication or audit controls yet
- No live Epic launch handshake yet

## Best Next Build Steps

1. Replace local file upload as the primary ingest path with Orthanc-backed study creation.
2. Attach PostgreSQL and run this on a real deployment target.
3. Swap the internal slice viewer for an embedded OHIF viewer pane.
4. Convert segmentation artifacts into DICOM SEG exports.
5. Move AI tasks onto a background queue with worker processes.
6. Add authentication, tenancy, and audit logging before any real PHI use.
