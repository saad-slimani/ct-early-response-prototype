from __future__ import annotations

import base64
import io
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import SimpleITK as sitk
from PIL import Image
from app.services.dicom_archive import preserve_series, stage_dicom_inputs


COMMON_DICOM_TAGS = {
    "0008|0020": "study_date",
    "0008|0030": "study_time",
    "0008|0050": "accession_number",
    "0008|0060": "modality",
    "0008|1030": "study_description",
    "0008|103e": "series_description",
    "0010|0010": "patient_name",
    "0010|0020": "patient_id",
    "0018|0015": "body_part",
    "0020|000d": "study_instance_uid",
    "0020|000e": "series_instance_uid",
    "0020|0011": "series_number",
}


def _flatten_dicom_directory(root: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for index, src in enumerate(path for path in root.rglob("*") if path.is_file()):
        suffix = src.suffix if len(src.suffix) <= 12 else ""
        shutil.copyfile(src, output_dir / f"dicom_{index:06d}{suffix}")


def _series_ids_from_dir(root: Path) -> List[str]:
    ids = sitk.ImageSeriesReader.GetGDCMSeriesIDs(str(root))
    return list(ids or [])


def _read_dicom_metadata(file_path: Path) -> Dict[str, str]:
    reader = sitk.ImageFileReader()
    reader.SetFileName(str(file_path))
    reader.LoadPrivateTagsOff()
    reader.ReadImageInformation()
    metadata: Dict[str, str] = {}
    for tag, name in COMMON_DICOM_TAGS.items():
        if reader.HasMetaDataKey(tag):
            metadata[name] = reader.GetMetaData(tag).strip()
    return metadata


def discover_dicom_series(root: Path) -> List[Dict[str, object]]:
    series_ids = _series_ids_from_dir(root)
    if not series_ids:
        return []

    discovered: List[Dict[str, object]] = []
    for series_id in series_ids:
        file_names = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(str(root), series_id)
        metadata = _read_dicom_metadata(Path(file_names[0])) if file_names else {}
        metadata["series_instance_uid"] = metadata.get("series_instance_uid") or series_id
        metadata["series_id"] = series_id
        metadata["instance_count"] = len(file_names)
        metadata["source_files"] = [Path(name).name for name in file_names[:6]]
        discovered.append(metadata)
    return discovered


def read_dicom_series_from_dir(root: Path, series_id: Optional[str] = None) -> sitk.Image:
    reader = sitk.ImageSeriesReader()
    series_ids = _series_ids_from_dir(root)
    if not series_ids:
        raise ValueError("No DICOM series found in archive")
    target_series = series_id if series_id in series_ids else series_ids[0]
    file_names = reader.GetGDCMSeriesFileNames(str(root), target_series)
    reader.SetFileNames(file_names)
    return ensure_3d_image(reader.Execute())


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


def load_scan_from_file(upload_path: Path, *, dicom_archive_path: Path | None = None) -> sitk.Image:
    suffixes = upload_path.suffixes
    if upload_path.suffix == ".zip":
        with tempfile.TemporaryDirectory() as td:
            source, flat = Path(td) / "source", Path(td) / "flat"
            source.mkdir()
            shutil.copyfile(upload_path, source / "scan.zip")
            stage_dicom_inputs(source, flat)
            series = discover_dicom_series(flat)
            if len(series) != 1:
                raise ValueError("Single-study ZIP requires exactly one DICOM series. Use bulk folder/ZIP import for multiple series.")
            image = read_dicom_series_from_dir(flat, str(series[0]["series_id"]))
            if dicom_archive_path:
                preserve_series(flat, str(series[0]["series_id"]), dicom_archive_path)
            return ensure_3d_image(image)

    if suffixes[-2:] == [".nii", ".gz"] or upload_path.suffix == ".nii":
        reader = sitk.ImageFileReader()
        reader.SetFileName(str(upload_path))
        reader.ReadImageInformation()
        if np.prod(reader.GetSize()) * reader.GetNumberOfComponents() > 128 * 1024 * 1024:
            raise ValueError("Image exceeds the 128-million-voxel local limit")
        return ensure_3d_image(reader.Execute())

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
