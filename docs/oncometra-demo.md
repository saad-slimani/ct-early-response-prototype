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
   Select Full screen in the viewer toolbar, or press F, to hide the workspace
   chrome and expand the annotation area. Esc or Exit full screen returns without
   resetting the slice, focused plane, zoom or mask. Inspector toggles the lesion
   and model panel. Unsupported browsers use an escapable expanded-window view.
4. Complete the case. Another reviewer can inspect, approve, return with comments,
   compare submitted union masks, or create a separate adjudicated copy.
5. After independent approval, choose Feature file on the case or review row.
   Configure feature families, bin width, normalization and optional resampling,
   then generate a ZIP containing CSV features, reusable settings and provenance.
   Image/mask exports remain available on completed or approved submissions.

Readers see only their own assigned masks. Reviewers can inspect other readers'
submissions, but cannot edit them in place or approve their own work. Administrators
also act as reviewers for this demo. The count of submitted examinations means
at least one completed reading, not that every required reader has finished.
Lesion observations are counted per reading, not as adjudicated unique tumors.

## Approved Feature Files

PyRadiomics 3.0.1 extracts seven Original-image feature families per lesion:
shape, first-order, GLCM, GLRLM, GLSZM, GLDM and NGTDM. CT uses a native-grid,
25-HU-bin protocol. MRI uses explicit whole-volume z-score normalization with
scale 100 and bin width 20. Neither is a validated clinical biomarker protocol.
The previous Radiomics workspace tab has been removed. Features are generated
per approved assignment, not before approval. CSV, JSON and ZIP retain the mask
revision, approval event, reviewer, settings, package version and hashes.
Reopened or modified submissions mark prior jobs as superseded. Interrupted jobs
are marked failed and can be retried. Superseded approvals cannot be downloaded
as current feature files. Extraction and segmentation share a single compute lock.
Native-grid extraction is the default; very large resampling requests are rejected.

## MedSAM

LiteMedSAM runs on CPU using ONNX Runtime, without PyTorch in the deployed app.
The checked-in encoder and decoder derive from the official LiteMedSAM checkpoint;
their hashes, source revision, license and conversion parity results are stored in
`model-assets/litemedsam/manifest.json`. Build and inference verify artifact hashes.
The upstream model is https://github.com/bowang-lab/MedSAM/tree/LiteMedSAM.

Draw one box on axial, coronal or sagittal images. Select 1, 5, 9, 17 or 31 slices
centered on that box's slice (clipped at volume boundaries). CT uses the current
display window; MR uses 0.5/99.5 percentile clipping of positive image values and
per-slice normalization. Every selected slice receives the same box independently.
This is **2D MedSAM with a bounded slice range**, not MedSAM2, volumetric tracking,
automatic tumor detection or tumor-specific clinical validation. Inspect the
proposal, accept it explicitly, edit, complete and request independent review.
Cancel terminates the isolated worker. Accepted proposals can be undone.

Only the selected pixel slab is staged into the worker, not the full imaging
runtime. Processes run one job at a time and exit after inference. Real CT/MR
technical checks in all three planes are in `docs/litemedsam-technical-smoke.json`;
their boxes are reference-derived, not evidence of independent clinical accuracy.

For collaborators who must survive free-host restarts, set the private Render
environment variable `ONCOMETRA_BOOTSTRAP_USERS` to a JSON array containing
`username`, `name`, `role` and a PBKDF2 password hash generated by
`scripts/demo_admin.py provision`. Never commit credential records or this secret.
Only missing accounts are seeded; existing accounts are never reset. Bootstrap
configuration can explicitly provision administrators; restrict access to this
Render secret. Role changes use the administrator-only, audited
`PATCH /api/users/{user_id}/role` endpoint and invalidate the affected user's
sessions without changing their password or assignments. Synchronize the private
bootstrap record after an intentional role change on disposable hosting.

## Scope Boundaries

- Brain CT and rectum MRI are empty until rights-cleared positive cases are verified.
- MedSAM2 and tumor-specific models remain unconfigured; LiteMedSAM is the running adapter.
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
python -m pytest tests/test_oncometra.py tests/test_medsam_runtime.py -q
python -m scripts.smoke_medsam
node --test tests/viewer-fullscreen.test.mjs
```

Tests cover authentication, reader isolation, independent approval, completion
locking, adjudication, floating-point MRI transfers, image/mask exports, upload
validation, real feature extraction and stale-input tracking.

Public dataset provenance and data-license terms are in
[demo-cases/README.md](../demo-cases/README.md).
