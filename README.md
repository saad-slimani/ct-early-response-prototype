# CT Early Response Web App (Prototype)

See [docs/ai-native-pacs-ris-blueprint.md](docs/ai-native-pacs-ris-blueprint.md) for the broader product direction that extends this prototype toward an AI-native PACS/RIS platform.

See [docs/mvp-scaffold.md](docs/mvp-scaffold.md) for the current MVP scaffold that adds a worklist, reporting, AI task tracking, and local fine-tune queue.

See [docs/render-deploy.md](docs/render-deploy.md) for the prepared Render deployment path.

This project provides a working prototype for an AI-native PACS/RIS workstation with an early treatment response CT workflow:

- Bulk upload DICOM folders with metadata extraction, or upload DICOM zip/NIfTI studies across CT, MRI, ultrasound, mammography, and X-ray-style modalities
- Review studies in an embedded PACS viewer with single-series or comparison layouts, active-series selection, modality presets, mouse window/level, inversion, zoom, cine stack playback, axial/coronal/sagittal MPR, measurements, and segmentation mask editing
- Manage a RIS worklist with scheduling and billing charge capture
- Draft reports in a browser word processor and insert AI outputs or saved measurements
- Dictate reports through hosted browser speech recognition or a local/open-source speech-to-text adapter such as Whisper.cpp
- Upload baseline CT and early follow-up CT (2-6 weeks) for the early-response workflow
- Run semi-automatic lesion segmentation using open-source tooling adapters
- Review and edit segmentation slice-by-slice in a web viewer
- Produce an early response score from 0-100
- Train a radiomics regression model from labeled prior studies

## Stack

- Backend: FastAPI
- Imaging I/O: SimpleITK
- Segmentation adapters:
  - `TotalSegmentator` CLI adapter (if installed)
  - region-growing fallback (bounded seeded grower)
- Radiomics/modeling: handcrafted radiomics + lightweight linear regression
- Speech-to-text: optional open-source Whisper.cpp adapter
- Hosted dictation fallback: browser `SpeechRecognition` when the backend model is not configured
- Frontend: HTML/CSS/vanilla JS PACS/RIS workstation

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

Configuration starts from [.env.example](.env.example).

## Data format

Upload either:

- DICOM folder: select a local folder from the bulk import panel
- NIfTI: `.nii` or `.nii.gz`
- DICOM archive: `.zip` containing one imaging series

For single-frame X-ray, mammography, or ultrasound studies, upload the primary series and leave the comparison upload empty. Folder imports and single-frame studies store a one-slice volume internally so the same viewer controls still work.

## API overview

- `POST /api/studies` create a study workspace from baseline/follow-up uploads
- `POST /api/dicom/bulk` create one worklist-ready study per DICOM series in a folder upload
- `GET /api/worklist` list reading queue items
- `GET /api/studies/{id}/workspace` retrieve study, worklist, report, AI, and integration context
- `PATCH /api/studies/{id}/worklist` update workflow status and assignment
- `GET /api/studies/{id}/report` read the current draft report
- `PUT /api/studies/{id}/report` save report draft or finalize it
- `GET /api/studies/{id}/slice` retrieve an image + mask plane PNG with window/MPR options
- `PUT /api/studies/{id}/mask/slice` persist edited slice mask
- `GET /api/studies/{id}/measurements` list viewer measurements
- `POST /api/studies/{id}/measurements` save a viewer measurement
- `POST /api/studies/{id}/segment/auto` run semi-auto segmentation
- `POST /api/studies/{id}/ai/tasks` run scaffolded AI tasks
- `GET /api/schedule` and `POST /api/schedule` manage RIS appointments
- `GET /api/billing` and `POST /api/billing` manage billing items
- `GET /api/speech/status` inspect the local speech engine
- `POST /api/speech/transcribe` transcribe browser microphone audio when Whisper.cpp is configured
- `GET /api/models` inspect the indexed model catalog
- `POST /api/model/train` fit the radiomics model from labels
- `GET /api/training-runs` inspect fine-tune requests

## Dictation

The hosted free Render deployment uses browser speech recognition when available, so the report dictation button can work without shipping a large model. To enable local/open-source backend dictation, install Whisper.cpp and ffmpeg, download a Whisper model, then set:

```bash
STT_PROVIDER=whisper_cpp
WHISPER_CPP_BINARY=/path/to/whisper-cli
WHISPER_CPP_MODEL=/path/to/ggml-base.en.bin
FFMPEG_BINARY=ffmpeg
```

## Notes for production hardening

- Add authentication, audit logging, and PHI controls
- Replace fallback segmentation with tumor-site-specific foundation models
- Add DICOM metadata QC and orientation normalization checks
- Version models and require external validation before clinical use
- Add uncertainty calibration and confidence intervals

## Important

This is a research/prototyping codebase and is **not** a regulated clinical decision system.
