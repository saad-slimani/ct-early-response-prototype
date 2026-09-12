"""Verified LiteMedSAM ONNX inference; no PyTorch in the web deployment."""

from functools import lru_cache
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import numpy as np

MODEL_FILES = {
    "encoder.onnx": "e56a2f885ab34950c269633f852f30c0c84509eb984c79fff7614055440e09b8",
    "decoder.onnx": "ad0359e1ed4a227bbc4f8c8fd116f88047c7f48d4b15152f43a6375fd822b11a",
}


def model_directory():
    return Path(os.getenv("MEDSAM_MODEL_DIR", str(Path(__file__).resolve().parents[2] / "model-assets/litemedsam")))


@lru_cache(maxsize=4)
def verified_manifest(directory, file_state):
    directory = Path(directory)
    for name, expected in MODEL_FILES.items():
        with (directory / name).open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != expected:
                raise ValueError(f"LiteMedSAM {name} checksum mismatch")
    manifest = json.loads((directory / "manifest.json").read_text())
    if any(manifest["files"].get(name) != sha for name, sha in MODEL_FILES.items()):
        raise ValueError("LiteMedSAM manifest does not match pinned artifacts")
    return manifest


def verify_model():
    directory = model_directory()
    state = tuple((name, (directory / name).stat().st_mtime_ns, (directory / name).stat().st_size)
                  for name in (*MODEL_FILES, "manifest.json"))
    return verified_manifest(str(directory), state)


def status():
    available, reason = False, ""
    try:
        verify_model()
        if not importlib.util.find_spec("onnxruntime") or not importlib.util.find_spec("cv2"):
            raise ValueError("Install the demo requirements to enable ONNX inference")
        available = True
    except (OSError, ValueError, KeyError) as error:
        reason = str(error)
    return {"id": "litemedsam-onnx", "name": "LiteMedSAM", "configured": available,
        "enabled": True, "available": available, "reason": reason,
        "modalities": ["CT", "MR"], "device": "CPU / ONNX", "max_slice_radius": 15,
        "scope": "Box-prompted 2D inference on the selected plane and slice range (up to 31 slices). Not whole-volume detection.",
        "license": "Apache-2.0 upstream repository. Check checkpoint and data terms for intended production use.",
        "validation": "Technical inference and conversion tests only; no clinical accuracy claim."}


def intensity_bounds(volume, modality, level=40, width=400):
    if modality == "CT":
        return float(level - width / 2), float(level + width / 2)
    if modality != "MR":
        raise ValueError("LiteMedSAM accepts CT or MR only")
    values = volume[volume > 0]
    owned = bool(values.size)
    if not values.size:
        values = volume.ravel()
    low, high = np.percentile(values, [0.5, 99.5], overwrite_input=owned)
    return float(low), float(max(high, low + 1e-8))


def prepare_slice(pixels, bounds):
    import cv2
    height, width = pixels.shape
    scale = 256 / max(height, width)
    new_height, new_width = int(height * scale + .5), int(width * scale + .5)
    # Match upstream CT/MR clipping and uint8 conversion before slice normalization.
    low, high = bounds
    clipped = np.clip(pixels.astype(np.float32), low, high)
    clipped = ((clipped - low) / max(high - low, 1e-8) * 255).astype(np.uint8)
    resized = cv2.resize(clipped, (new_width, new_height), interpolation=cv2.INTER_AREA).astype(np.float32)
    resized = (resized - resized.min()) / max(float(resized.max() - resized.min()), 1e-8)
    padded = np.pad(resized, ((0, 256 - new_height), (0, 256 - new_width)))
    tensor = np.repeat(padded[None, None], 3, axis=1)
    return tensor, (new_height, new_width), scale


def infer_volume(volume, frame, box, radius, modality, level, width, progress=None, bounds=None):
    import cv2
    import onnxruntime as ort
    manifest = verify_model()
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    sessions = [ort.InferenceSession(str(model_directory() / name), sess_options=options,
                                    providers=["CPUExecutionProvider"]) for name in MODEL_FILES]
    encoder, decoder = sessions
    start, stop = max(0, frame - radius), min(volume.shape[0], frame + radius + 1)
    bounds = bounds or intensity_bounds(volume, modality, level, width)
    prediction = np.zeros(volume.shape, dtype=np.uint8)
    for index in range(start, stop):
        tensor, new_size, scale = prepare_slice(volume[index], bounds)
        embedding = encoder.run(["embedding"], {"image": tensor})[0]
        logits = decoder.run(["logits"], {"embedding": embedding, "boxes": np.asarray([box], np.float32) * scale})[0][0, 0]
        restored = cv2.resize(logits[:new_size[0], :new_size[1]], (volume.shape[2], volume.shape[1]), interpolation=cv2.INTER_LINEAR)
        prediction[index] = restored > 0
        if progress:
            progress(index - start + 1, stop - start)
    return prediction, {"model_id": manifest["id"], "source_revision": manifest["source_revision"],
        "checkpoint_sha256": manifest["checkpoint_sha256"], "onnx_sha256": MODEL_FILES,
        "onnxruntime_version": ort.__version__, "device": "cpu", "precision": "float32",
        "modality": modality, "intensity_bounds": list(bounds), "slice_range_zero_based": [start, stop - 1],
        "slices_processed": stop - start, "propagation": "Independent slice inference with a fixed user box; not a 3D tracker",
        "clinical_validation": False, "license_notice": manifest["license"]}
