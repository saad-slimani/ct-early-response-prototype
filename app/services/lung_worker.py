"""Isolated, pinned CPU inference. Invoked only by the local job manager."""

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import SimpleITK as sitk

from app.services.lung_geometry import crop_for_prompt, image_for_plane, mask_from_plane, plane_axes, restore_crop


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    started = time.monotonic()
    # This module imports no model code in the web process; ML dependencies stay optional.
    from benchmarks.run_medsam2 import CODE_REVISION, WEIGHT_REVISION, MODELS, infer
    source = Path(os.environ["LUNG_MODEL_SOURCE"]).resolve()
    checkpoint = Path(os.environ["LUNG_MODEL_WEIGHTS"]).resolve()
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True).strip()
    if revision != CODE_REVISION or dirty:
        raise ValueError("Model source must be the clean pinned MedSAM2 revision")
    digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    if digest != MODELS["efficient_tiny"][1]:
        raise ValueError("Model weights failed SHA-256 verification")
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    sys.path.insert(0, str(source))
    import torch
    from efficient_track_anything.build_efficienttam import build_efficienttam_video_predictor_npz
    torch.set_num_threads(2)
    torch.manual_seed(20260910)
    np.random.seed(20260910)
    torch.use_deterministic_algorithms(True)
    image = sitk.ReadImage(request["image"])
    plane = request.get("plane", "axial")
    crop, starts, prompt = crop_for_prompt(image, request["frame_index"], request["box_xyxy"], plane)
    array = sitk.GetArrayFromImage(image_for_plane(crop, plane))
    if not np.isfinite(array).all():
        raise ValueError("CT contains nonfinite intensities")
    predictor = build_efficienttam_video_predictor_npz(MODELS["efficient_tiny"][2], str(checkpoint), device="cpu")
    predictor.fill_hole_area = 0
    prediction = infer(predictor, array, prompt, torch)
    mask = mask_from_plane(prediction, crop, plane)
    restored = restore_crop(mask, starts, image)
    output_dir = args.request.parent
    partial = output_dir / "proposal.partial.nii.gz"
    sitk.WriteImage(restored, str(partial), useCompression=True)
    partial.replace(output_dir / "proposal.nii.gz")
    result = {
        "model_id": "medsam2-efficient-tiny", "code_revision": revision,
        "weights_sha256": digest, "weights_revision": WEIGHT_REVISION,
        "device": "cpu", "threads": 2, "window_hu": [-1000, 400],
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "crop_start_xyz": starts, "crop_size_xyz": list(crop.GetSize()),
        "touches_crop_boundary": bool(any(np.take(prediction, edge, axis=axis).any()
                                           for axis in range(3) for edge in (0, -1))),
        "empty_prediction": not bool(prediction.any()),
        "volume_ml": float(prediction.sum() * np.prod(image.GetSpacing()) / 1000),
        "prompt_source": f"user_drawn_{plane}_box", "prompt_plane": plane,
        "model_axes_xyz": list(plane_axes(plane)), "plane_accuracy_validated": False,
        "clinical_validation": False,
        "license_notice": "Research/education only; commercial rights unresolved",
        "seed": 20260910, "hole_filling": "disabled_no_CUDA_extension",
        "versions": {name: importlib.metadata.version(name) for name in ("torch", "torchvision", "numpy", "pillow", "SimpleITK")},
        "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (output_dir / "result.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
