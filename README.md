# CT Early Response Web App (Prototype)

See [docs/ai-native-pacs-ris-blueprint.md](docs/ai-native-pacs-ris-blueprint.md) for the broader product direction that extends this prototype toward an AI-native PACS/RIS platform.

See [docs/mvp-scaffold.md](docs/mvp-scaffold.md) for the current MVP scaffold that adds a worklist, reporting, AI task tracking, and local fine-tune queue.

See [docs/render-deploy.md](docs/render-deploy.md) for the prepared Render deployment path.

This project provides a working prototype for early treatment response estimation in solid cancer CT:

- Upload baseline CT and early follow-up CT (2-6 weeks)
- Run semi-automatic lesion segmentation using open-source tooling adapters
- Review and edit segmentation slice-by-slice in a web viewer
- Produce an early response score from 0-100
- Train a radiomics regression model from labeled prior studies

## Stack

- Backend: FastAPI
- Imaging I/O: SimpleITK
- Segmentation adapters:
  - `TotalSegmentator` CLI adapter (if installed)
  - region-growing fallback (seeded ConnectedThreshold)
- Radiomics/modeling: handcrafted radiomics + scikit-learn RandomForest
- Frontend: HTML/CSS/vanilla JS canvas viewer/editor

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

- NIfTI: `.nii` or `.nii.gz`
- DICOM archive: `.zip` containing one CT series

## API overview

- `POST /api/studies` create a study workspace from baseline/follow-up uploads
- `GET /api/worklist` list reading queue items
- `GET /api/studies/{id}/workspace` retrieve study, worklist, report, AI, and integration context
- `PATCH /api/studies/{id}/worklist` update workflow status and assignment
- `GET /api/studies/{id}/report` read the current draft report
- `PUT /api/studies/{id}/report` save report draft or finalize it
- `GET /api/studies/{id}/slice` retrieve a CT + mask slice PNG
- `PUT /api/studies/{id}/mask/slice` persist edited slice mask
- `POST /api/studies/{id}/segment/auto` run semi-auto segmentation
- `POST /api/studies/{id}/ai/tasks` run scaffolded AI tasks
- `GET /api/models` inspect the indexed model catalog
- `POST /api/model/train` fit the radiomics model from labels
- `GET /api/training-runs` inspect fine-tune requests

## Notes for production hardening

- Add authentication, audit logging, and PHI controls
- Replace fallback segmentation with tumor-site-specific foundation models
- Add DICOM metadata QC and orientation normalization checks
- Version models and require external validation before clinical use
- Add uncertainty calibration and confidence intervals

## Important

This is a research/prototyping codebase and is **not** a regulated clinical decision system.
