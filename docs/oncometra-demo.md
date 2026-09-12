# Oncometra Demonstration

## Run Locally

Use Python 3.11. Create a virtual environment, activate it, and run:

```bash
bash scripts/render_build.sh
APP_STORAGE_ROOT="$HOME/.local/share/oncometra-demo" uvicorn app.oncometra:app --host 127.0.0.1 --port 8020
```

Open http://127.0.0.1:8020. The initial username is `saad`. Set
`ONCOMETRA_ADMIN_PASSWORD` to a private password of at least 12 characters before
the first start, or read the generated local `admin-password.txt` in the storage
directory. Passwords are hashed; the file is created with owner-only permissions.
Environment changes do not reset an account in an existing database.

## Render

Deploy `render.yaml` from branch `codex/oncometra-demo`. It defines one free web
service, no paid databases, no workers, and no persistent disks. Its commands are:

```text
Build: bash scripts/render_build.sh
Start: uvicorn app.oncometra:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips '*'
Health: /healthz
```

The Blueprint generates `ONCOMETRA_ADMIN_PASSWORD` as a Render environment secret.
Read it from the service's Environment page and sign in as `saad`. Create separate
accounts for collaborators under Administration; do not share the administrator
password. There is no public registration or password-reset email service.

The service seeds only the five committed public example images and reference
masks. No local private studies, local user accounts, or local annotations are
uploaded. Source checksum validation happens at startup.

**Free storage is temporary, including SQLite, uploaded files, accounts and
results.** Export work before leaving the demo. Render can spin down after 15
minutes without inbound traffic, and can restart at any time. Reopening a spun
down service can take about a minute. See [Render Free limitations](https://render.com/docs/free).
Keep one process and one instance. Do not attach a persistent database without
also implementing durable image, mask, revision and artifact storage.

## Demonstration Workflow

1. Open a tumor project, then select Begin annotation.
2. Draw an independent mask, or explicitly load the released reference as a
   practice draft. Reference-assisted work is not independent validation data.
3. Inspect axial, coronal and sagittal planes. Edit individual lesion labels with
   2D or 3D brushes, erase or delete a lesion, and undo edits.
4. Complete the case. Another reviewer can inspect, approve, return with comments,
   compare submitted union masks, or create a separate adjudicated copy.
5. Export image/mask data or run PyRadiomics against the submitted revision.

Readers see only their own assigned masks. Reviewers can inspect other readers'
submissions, but cannot edit them in place or approve their own work. Administrators
also act as reviewers for this demo. The count of submitted examinations means
at least one completed reading, not that every required reader has finished.
Lesion observations are counted per reading, not as adjudicated unique tumors.

## Radiomics

PyRadiomics 3.0.1 extracts seven Original-image feature families per lesion:
shape, first-order, GLCM, GLRLM, GLSZM, GLDM and NGTDM. CT uses a native-grid,
25-HU-bin protocol. MRI uses explicit whole-volume z-score normalization with
scale 100 and bin width 20. Neither is a validated clinical biomarker protocol.
CSV and JSON retain the mask revision, settings, package version and hashes.
Reopened or modified submissions mark prior jobs as superseded. Interrupted jobs
are marked failed and can be retried. Extraction is single-instance and CPU-only.

## Scope Boundaries

- Brain CT and rectum MRI are empty until rights-cleared positive cases are verified.
- MedSAM and tumor-specific models are candidates, not running inference services.
- Demo images are derived, downsampled public NIfTI volumes, not original DICOM.
- DICOM SEG, longitudinal response prediction, DICOM networking, EHR integration,
  SSO, audit retention guarantees, clinical/FDA validation and billing are absent.
- The application has basic independent-reader review, not a validated multi-reader
  benchmark with lesion matching, surface-distance metrics or statistical inference.
- Thousands-of-scans production use needs paginated APIs, PostgreSQL migrations,
  object storage, a durable job queue, isolated inference workers, account lifecycle
  controls, project administration, backups and tested recovery, quotas and load tests.

## Verification

```bash
python -m pytest tests/test_oncometra.py -q
```

Tests cover authentication, reader isolation, independent approval, completion
locking, adjudication, floating-point MRI transfers, image/mask exports, upload
validation, real feature extraction and stale-input tracking.

Public dataset provenance and data-license terms are in
[demo-cases/README.md](../demo-cases/README.md).
