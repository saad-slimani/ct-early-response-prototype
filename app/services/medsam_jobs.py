"""Stage a bounded pixel slab so the inference process does not load SimpleITK."""

import json
from pathlib import Path

import numpy as np
import SimpleITK as sitk

from app.services.lung_geometry import plane_axes, same_grid
from app.services.medsam_runtime import intensity_bounds


def stage_job(directory, image, request):
    a, b, normal = plane_axes(request["plane"])
    native = sitk.GetArrayViewFromImage(image)
    view = np.transpose(native, (2 - normal, 2 - b, 2 - a))
    frame, radius = request["frame_index"], request["slice_radius"]
    start, stop = max(0, frame - radius), min(view.shape[0], frame + radius + 1)
    np.save(directory / "pixels.npy", view[start:stop], allow_pickle=False)
    request.update(input_pixels=str((directory / "pixels.npy").resolve()),
        slice_start=start, input_frame_index=frame - start,
        intensity_bounds=intensity_bounds(native, request["modality"], request["window_level"], request["window_width"]),
        voxel_volume_ml=float(np.prod(image.GetSpacing()) / 1000))
    (directory / "request.json").write_text(json.dumps(request))


def collect_result(directory, image):
    request = json.loads((directory / "request.json").read_text())
    result = json.loads((directory / "result.json").read_text())
    prediction = np.load(directory / "proposal.npy", allow_pickle=False)
    a, b, normal = plane_axes(request["plane"])
    start = request["slice_start"]
    stop = min(image.GetSize()[normal], request["frame_index"] + request["slice_radius"] + 1)
    expected = (stop - start, image.GetSize()[b], image.GetSize()[a])
    if prediction.shape != expected or not np.isin(prediction, [0, 1]).all():
        raise ValueError("Model output shape or label values are invalid")
    native = np.zeros(image.GetSize()[::-1], dtype=np.uint8)
    view = np.transpose(native, (2 - normal, 2 - b, 2 - a))
    view[start:stop] = prediction
    mask = sitk.GetImageFromArray(native)
    mask.CopyInformation(image)
    same_grid(mask, image)
    partial = directory / "proposal.partial.nii.gz"
    sitk.WriteImage(mask, str(partial), True)
    partial.replace(directory / "proposal.nii.gz")
    (directory / "pixels.npy").unlink(missing_ok=True)
    (directory / "proposal.npy").unlink(missing_ok=True)
    return result
