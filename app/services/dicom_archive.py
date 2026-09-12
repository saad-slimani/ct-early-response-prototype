"""Retain original DICOM bytes separately from converted volumes and annotation masks."""

import hashlib
import json
from pathlib import Path
import shutil
import zipfile

import SimpleITK as sitk

MAX_FILES = 3000
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024


def stage_dicom_inputs(source: Path, target: Path):
    """Flatten raw files and bounded ZIPs into a GDCM-readable temporary directory."""
    target.mkdir(parents=True, exist_ok=True)
    count, total = 0, 0

    def copy(stream, size):
        nonlocal count, total
        count += 1
        total += size
        if count > MAX_FILES or total > MAX_EXPANDED_BYTES:
            raise ValueError("DICOM batch exceeds 3,000 files or 1 GB expanded size")
        with (target / f"instance-{count:06d}.dcm").open("wb") as output:
            shutil.copyfileobj(stream, output, length=1024 * 1024)

    for path in sorted(source.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                if len(archive.infolist()) > MAX_FILES:
                    raise ValueError("DICOM ZIP exceeds 3,000 entries")
                for member in archive.infolist():
                    parts = Path(member.filename.replace("\\", "/")).parts
                    if member.filename.startswith(("/", "\\")) or ".." in parts or ":" in member.filename:
                        raise ValueError("DICOM ZIP contains an unsafe archive entry")
                    if ((member.external_attr >> 16) & 0o170000) == 0o120000:
                        raise ValueError("DICOM ZIP contains a symbolic link")
                    if member.is_dir() or any(part.startswith(".") or part == "__MACOSX" for part in parts):
                        continue
                    with archive.open(member) as stream:
                        copy(stream, member.file_size)
        elif not path.name.startswith("."):
            with path.open("rb") as stream:
                copy(stream, path.stat().st_size)
    return count


def preserve_series(root: Path, series_id: str, destination: Path):
    files = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(str(root), series_id)
    if not files:
        raise ValueError("Cannot archive an empty DICOM series")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".partial.zip")
    instances = []
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for index, name in enumerate(files):
                relative = f"DICOM/{index + 1:06d}.dcm"
                digest = hashlib.sha256()
                with Path(name).open("rb") as source, archive.open(relative, "w") as output:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        output.write(chunk)
                instances.append({"path": relative, "sha256": digest.hexdigest()})
            archive.writestr("manifest.json", json.dumps({"schema_version": 1, "series_instance_uid": series_id,
                "content": "Original uploaded DICOM bytes. Filenames normalized; DICOM tags and pixels are unchanged.",
                "privacy": "Not automatically de-identified. Includes original metadata. Not a DICOM SEG export.",
                "instances": instances}, indent=2))
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    return {"instance_count": len(instances), "archive_bytes": destination.stat().st_size}
