"""One isolated, cancellable LiteMedSAM job. Writes a proposal, never a saved mask."""

import argparse
import json
import hashlib
from pathlib import Path
import resource
import sys
import time

import numpy as np
from app.services.medsam_runtime import infer_volume


def run(request_path):
    request_path = Path(request_path)
    request = json.loads(request_path.read_text())
    folder = request_path.parent
    started = time.monotonic()
    plane = request.get("plane", "axial")
    volume = np.load(request["input_pixels"], allow_pickle=False, mmap_mode="r")
    if not np.isfinite(volume).all():
        raise ValueError("Image contains nonfinite intensities")

    def progress(completed, total, phase):
        partial = folder / "progress.partial.json"
        partial.write_text(json.dumps({"completed": completed, "total": total, "phase": phase}))
        partial.replace(folder / "progress.json")

    prediction, result = infer_volume(volume, request["input_frame_index"], request["box_xyxy"],
        request.get("slice_radius", 4), request["modality"], request.get("window_level", 40),
        request.get("window_width", 400), progress, request["intensity_bounds"], folder)
    np.save(folder / "proposal.npy", prediction, allow_pickle=False)
    lo, hi = result["slice_range_zero_based"]
    result.update(elapsed_seconds=round(time.monotonic() - started, 3),
        volume_ml=float(prediction.sum() * request["voxel_volume_ml"]),
        empty_prediction=not bool(prediction.any()), prompt_plane=plane,
        box_xyxy=request["box_xyxy"], prompt_source=request.get("prompt_source", "user_drawn_box"),
        touches_crop_boundary=bool(prediction[lo].any() or prediction[hi].any()),
        worker_peak_rss_mb=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**20 if sys.platform == "darwin" else 1024), 1),
        adapter_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    result["slice_range_zero_based"] = [lo + request["slice_start"], hi + request["slice_start"]]
    (folder / "result.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request")
    run(parser.parse_args().request)
