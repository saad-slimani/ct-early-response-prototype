"""Bounded, reproducible parameters for approved-mask feature files."""

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

FAMILIES = ("firstorder", "shape", "glcm", "glrlm", "glszm", "gldm", "ngtdm")
Family = Literal["firstorder", "shape", "glcm", "glrlm", "glszm", "gldm", "ngtdm"]


class FeatureSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    families: list[Family] = Field(default_factory=lambda: list(FAMILIES), min_length=1, max_length=7)
    bin_width: float | None = Field(default=None, ge=1, le=1000, allow_inf_nan=False)
    normalize: bool | None = None
    normalize_scale: float = Field(default=100, ge=1, le=1000, allow_inf_nan=False)
    resample_mm: float | None = Field(default=None, ge=0.5, le=5, allow_inf_nan=False)

    def protocol(self, modality):
        settings = {"binWidth": self.bin_width or (20 if modality == "MR" else 25),
            "normalize": self.normalize if self.normalize is not None else modality == "MR",
            "normalizeScale": self.normalize_scale,
            "resampledPixelSpacing": [self.resample_mm] * 3 if self.resample_mm else None,
            "interpolator": "sitkBSpline", "additionalInfo": True, "preCrop": True}
        protocol = {"version": 2, "setting": settings, "imageType": {"Original": {}},
                    "featureClass": {name: [] for name in sorted(set(self.families))}}
        sha = hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()
        return {"id": f"{modality.lower()}-original-{sha[:12]}", **protocol}


class FeatureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    revision_id: UUID
    settings: FeatureSettings = Field(default_factory=FeatureSettings)
