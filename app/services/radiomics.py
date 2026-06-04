from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List


@dataclass
class RadiomicsExample:
    features: Dict[str, float]
    target: float


class RadiomicsModel:
    def __init__(self, model_path: Path) -> None:
        self.model_path = model_path

    def extract_features(
        self,
        baseline_img_path: Path,
        followup_img_path: Path,
        baseline_mask_path: Path,
        followup_mask_path: Path,
    ) -> Dict[str, float]:
        import numpy as np
        import SimpleITK as sitk

        b_img = sitk.GetArrayFromImage(sitk.ReadImage(str(baseline_img_path))).astype(np.float32)
        f_img = sitk.GetArrayFromImage(sitk.ReadImage(str(followup_img_path))).astype(np.float32)
        b_msk = sitk.GetArrayFromImage(sitk.ReadImage(str(baseline_mask_path))).astype(bool)
        f_msk = sitk.GetArrayFromImage(sitk.ReadImage(str(followup_mask_path))).astype(bool)

        def lesion_stats(img: np.ndarray, msk: np.ndarray, prefix: str) -> Dict[str, float]:
            vox = img[msk]
            if vox.size == 0:
                return {
                    f"{prefix}_volume_vox": 0.0,
                    f"{prefix}_mean_hu": 0.0,
                    f"{prefix}_std_hu": 0.0,
                    f"{prefix}_p90_hu": 0.0,
                }
            return {
                f"{prefix}_volume_vox": float(msk.sum()),
                f"{prefix}_mean_hu": float(vox.mean()),
                f"{prefix}_std_hu": float(vox.std()),
                f"{prefix}_p90_hu": float(np.percentile(vox, 90)),
            }

        feat = {}
        feat.update(lesion_stats(b_img, b_msk, "baseline"))
        feat.update(lesion_stats(f_img, f_msk, "followup"))

        bvol = feat["baseline_volume_vox"]
        fvol = feat["followup_volume_vox"]
        feat["delta_volume_pct"] = float(((fvol - bvol) / bvol) * 100.0) if bvol > 0 else 0.0
        feat["delta_mean_hu"] = feat["followup_mean_hu"] - feat["baseline_mean_hu"]
        feat["delta_std_hu"] = feat["followup_std_hu"] - feat["baseline_std_hu"]
        return feat

    def train(self, examples: Iterable[RadiomicsExample]) -> Dict[str, float]:
        import joblib
        import numpy as np

        rows = list(examples)
        if len(rows) < 5:
            raise ValueError("Need at least 5 labeled studies to train a stable model")
        keys = sorted(rows[0].features.keys())
        X = np.array([[r.features[k] for k in keys] for r in rows], dtype=np.float32)
        y = np.array([r.target for r in rows], dtype=np.float32)

        mean = X.mean(axis=0, keepdims=True)
        std = X.std(axis=0, keepdims=True)
        std = np.where(std < 1e-6, 1.0, std)
        X_scaled = (X - mean) / std
        ones = np.ones((X_scaled.shape[0], 1), dtype=np.float32)
        design = np.concatenate([ones, X_scaled], axis=1)
        coeffs, *_ = np.linalg.lstsq(design, y, rcond=None)
        pred = design @ coeffs
        mae = float(np.mean(np.abs(pred - y)))
        r2_num = float(np.sum((pred - y.mean()) ** 2))
        r2_den = float(np.sum((y - y.mean()) ** 2) + 1e-8)
        r2 = max(0.0, min(1.0, r2_num / r2_den))

        payload = {
            "kind": "linear-regression",
            "feature_keys": keys,
            "mean": mean.astype(np.float32).tolist()[0],
            "std": std.astype(np.float32).tolist()[0],
            "coefficients": coeffs.astype(np.float32).tolist(),
        }
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(payload, self.model_path)
        return {"train_mae": mae, "train_r2_proxy": r2, "n_samples": float(len(rows))}

    def predict(self, features: Dict[str, float]) -> float:
        import joblib
        import numpy as np

        if self.model_path.exists():
            payload = joblib.load(self.model_path)
            keys = payload["feature_keys"]
            x = np.array([[features.get(k, 0.0) for k in keys]], dtype=np.float32)
            mean = np.array([payload["mean"]], dtype=np.float32)
            std = np.array([payload["std"]], dtype=np.float32)
            coeffs = np.array(payload["coefficients"], dtype=np.float32)
            x_scaled = (x - mean) / std
            design = np.concatenate([np.ones((1, 1), dtype=np.float32), x_scaled], axis=1)
            y = float((design @ coeffs.reshape(-1, 1))[0, 0])
            return float(np.clip(y, 0.0, 100.0))

        # Heuristic fallback if no trained model exists yet.
        delta_volume_pct = features.get("delta_volume_pct", 0.0)
        score = 60.0 - 0.7 * delta_volume_pct
        return float(np.clip(score, 0.0, 100.0))
