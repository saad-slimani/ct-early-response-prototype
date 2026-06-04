from __future__ import annotations

import base64
import io
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import SimpleITK as sitk
from PIL import Image


def _read_dicom_series_from_dir(root: Path) -> sitk.Image:
    reader = sitk.ImageSeriesReader()
    series_ids = reader.GetGDCMSeriesIDs(str(root))
    if not series_ids:
        raise ValueError("No DICOM series found in archive")
    file_names = reader.GetGDCMSeriesFileNames(str(root), series_ids[0])
    reader.SetFileNames(file_names)
    return reader.Execute()


def ensure_3d_image(image: sitk.Image) -> sitk.Image:
    if image.GetDimension() == 3:
        return image
    if image.GetDimension() != 2:
        raise ValueError(f"Unsupported image dimension: {image.GetDimension()}")
    image3d = sitk.JoinSeries(image)
    spacing = list(image.GetSpacing()) + [1.0]
    origin = list(image.GetOrigin()) + [0.0]
    image3d.SetSpacing(spacing)
    image3d.SetOrigin(origin)
    return image3d


def load_scan_from_file(upload_path: Path) -> sitk.Image:
    suffixes = upload_path.suffixes
    if upload_path.suffix == ".zip":
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            with zipfile.ZipFile(upload_path, "r") as zf:
                zf.extractall(tdir)
            return ensure_3d_image(_read_dicom_series_from_dir(tdir))

    if suffixes[-2:] == [".nii", ".gz"] or upload_path.suffix == ".nii":
        return ensure_3d_image(sitk.ReadImage(str(upload_path)))

    raise ValueError("Unsupported format. Use .nii, .nii.gz, or DICOM .zip")


def save_nifti(image: sitk.Image, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sitk.WriteImage(image, str(output_path), useCompression=True)


def normalize_image_slice(
    arr2d: np.ndarray,
    *,
    window_center: float | None = None,
    window_width: float | None = None,
    invert: bool = False,
) -> np.ndarray:
    if window_center is None or window_width is None or window_width <= 0:
        low, high = float(np.percentile(arr2d, 1)), float(np.percentile(arr2d, 99))
        if low == high:
            low, high = float(arr2d.min()), float(arr2d.max() or arr2d.min() + 1)
    else:
        low = float(window_center) - float(window_width) / 2.0
        high = float(window_center) + float(window_width) / 2.0

    if low == high:
        high = low + 1.0
    clipped = np.clip(arr2d, low, high)
    scaled = ((clipped - low) / (high - low) * 255.0).astype(np.uint8)
    if invert:
        scaled = 255 - scaled
    return scaled


def normalize_ct_slice(arr2d: np.ndarray) -> np.ndarray:
    return normalize_image_slice(arr2d, window_center=50.0, window_width=500.0)


def make_png_base64(arr2d: np.ndarray) -> str:
    img = Image.fromarray(arr2d)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def decode_mask_png_b64(mask_png_b64: str, expected_hw: Tuple[int, int]) -> np.ndarray:
    raw = base64.b64decode(mask_png_b64)
    img = Image.open(io.BytesIO(raw)).convert("L")
    if img.size != (expected_hw[1], expected_hw[0]):
        img = img.resize((expected_hw[1], expected_hw[0]))
    arr = np.array(img)
    return (arr > 127).astype(np.uint8)


def image_to_numpy_zyx(image: sitk.Image) -> np.ndarray:
    arr = sitk.GetArrayFromImage(image)
    if arr.ndim == 2:
        return arr[np.newaxis, :, :]
    return arr


def numpy_to_image_zyx(arr: np.ndarray, reference: sitk.Image) -> sitk.Image:
    out = sitk.GetImageFromArray(arr.astype(np.uint8))
    out.CopyInformation(reference)
    return out


def create_empty_mask(reference: sitk.Image) -> sitk.Image:
    arr = sitk.GetArrayFromImage(reference)
    mask = np.zeros_like(arr, dtype=np.uint8)
    out = sitk.GetImageFromArray(mask)
    out.CopyInformation(reference)
    return out


def copy_upload_to_tmp(src: Path) -> Path:
    fd, dst = tempfile.mkstemp(prefix="scan_upload_", suffix=src.suffix)
    Path(dst).unlink(missing_ok=True)
    shutil.copy(src, dst)
    return Path(dst)


def scan_stats(image: sitk.Image) -> Dict[str, object]:
    arr = sitk.GetArrayFromImage(image)
    return {
        "shape_zyx": list(arr.shape),
        "spacing_xyz": list(image.GetSpacing()),
        "origin_xyz": list(image.GetOrigin()),
        "direction_len": len(image.GetDirection()),
        "slice_min_hu": float(arr.min()),
        "slice_max_hu": float(arr.max()),
    }
