from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict


def _data_root() -> Path:
    default_root = Path(__file__).resolve().parents[2] / "data"
    configured_root = os.getenv("APP_STORAGE_ROOT") or os.getenv("STORAGE_ROOT")
    return Path(configured_root).expanduser() if configured_root else default_root


DATA_ROOT = _data_root()
STUDIES_DIR = DATA_ROOT / "studies"
MODELS_DIR = DATA_ROOT / "models"


class StudyStore:
    def __init__(self) -> None:
        STUDIES_DIR.mkdir(parents=True, exist_ok=True)
        MODELS_DIR.mkdir(parents=True, exist_ok=True)

    def create_study(self) -> str:
        study_id = str(uuid.uuid4())
        self.study_dir(study_id).mkdir(parents=True, exist_ok=False)
        self.write_meta(study_id, {"study_id": study_id})
        return study_id

    def study_dir(self, study_id: str) -> Path:
        return STUDIES_DIR / study_id

    def image_path(self, study_id: str, timepoint: str) -> Path:
        return self.study_dir(study_id) / f"{timepoint}.nii.gz"

    def mask_path(self, study_id: str, timepoint: str) -> Path:
        return self.study_dir(study_id) / f"{timepoint}_mask.nii.gz"

    def meta_path(self, study_id: str) -> Path:
        return self.study_dir(study_id) / "meta.json"

    def write_meta(self, study_id: str, payload: Dict[str, Any]) -> None:
        meta = {}
        mp = self.meta_path(study_id)
        if mp.exists():
            meta = json.loads(mp.read_text())
        meta.update(payload)
        mp.write_text(json.dumps(meta, indent=2))

    def read_meta(self, study_id: str) -> Dict[str, Any]:
        mp = self.meta_path(study_id)
        if not mp.exists():
            raise FileNotFoundError(f"Unknown study_id: {study_id}")
        return json.loads(mp.read_text())

    def model_path(self, name: str = "radiomics_response.joblib") -> Path:
        return MODELS_DIR / name
