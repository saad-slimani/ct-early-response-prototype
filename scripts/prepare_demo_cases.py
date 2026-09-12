"""Fetch selected MSD members using HTTP ranges, never whole multi-GB archives."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile
import time
import urllib.request
import httpx

CLIENT = httpx.Client(timeout=60, follow_redirects=True)

TASKS = {
    "pancreas-ct": ("Task07_Pancreas", "pancreas", 2, "CT"),
    "liver-ct": ("Task03_Liver", "liver", 2, "CT"),
    "lung-ct": ("Task06_Lung", "lung", 1, "CT"),
    "brain-mr": ("Task01_BrainTumour", "BRATS", None, "MR"),
    "colon-ct": ("Task10_Colon", "colon", 1, "CT"),
}


def read_range(url, offset, length):
    if not 0 < length <= 256 * 1024 * 1024:
        raise ValueError("Invalid member size")
    for attempt in range(4):
        try:
            with CLIENT.stream("GET", url, headers={"Range": f"bytes={offset}-{offset+length-1}"}) as response:
                if response.status_code != 206 or not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                    raise ValueError("Server did not honor byte range; refusing full archive")
                data = response.read()
                if len(data) != length:
                    raise ValueError("Truncated archive member")
                return data
        except Exception:
            if attempt == 3:
                raise
            time.sleep(attempt + 1)


def index_archive(url, index_path):
    if index_path.exists():
        return json.loads(index_path.read_text())
    entries, offset = {}, 0
    for header in range(7000):
        block = read_range(url, offset, 512)
        if block == b"\0" * 512:
            break
        entry = tarfile.TarInfo.frombuf(block, "utf-8", "strict")
        if entry.isfile() and entry.name.endswith(".nii.gz") and "/._" not in entry.name:
            entries[entry.name] = [offset + 512, entry.size]
        offset += 512 + (entry.size + 511) // 512 * 512
        pairs = [name for name in entries if "/labelsTr/" in name and name.replace("/labelsTr/", "/imagesTr/") in entries]
        if len(pairs) >= 3:
            break
        if header and header % 100 == 0:
            print(f"  {header} headers indexed", flush=True)
    else:
        raise ValueError("Archive header limit exceeded")
    index_path.write_text(json.dumps(entries))
    return entries


def prepare(project, output):
    import numpy as np
    import SimpleITK as sitk

    task, _, tumor_label, modality = TASKS[project]
    directory = output / project
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "case.json").exists():
        return
    url = f"https://msd-for-monai.s3-us-west-2.amazonaws.com/{task}.tar"
    print(f"Indexing {task} (headers only)", flush=True)
    entries = index_archive(url, directory / "archive-index.json")
    candidates = sorted(name for name in entries if "/labelsTr/" in name)
    for mask_name in candidates[:12]:
        original_mask = directory / "source-mask.nii.gz"
        original_mask.write_bytes(read_range(url, *entries[mask_name]))
        mask_image = sitk.ReadImage(str(original_mask))
        mask = sitk.GetArrayFromImage(mask_image)
        target = mask > 0 if tumor_label is None else mask == tumor_label
        if not target.any():
            continue
        image_name = mask_name.replace("/labelsTr/", "/imagesTr/")
        if image_name not in entries:
            continue
        original = directory / "source-image.nii.gz"
        original.write_bytes(read_range(url, *entries[image_name]))
        image = sitk.ReadImage(str(original))
        sequence = None
        if image.GetDimension() == 4:
            # MSD BrainTumour channels: FLAIR, T1w, T1gd, T2w. Use T1gd.
            image = image[:, :, :, 2]
            sequence = "T1 post-contrast"
        if image.GetSize() != mask_image.GetSize():
            raise ValueError("Source image and mask sizes differ")
        if not np.allclose(image.GetDirection(), mask_image.GetDirection()) or not np.allclose(image.GetOrigin(), mask_image.GetOrigin()):
            raise ValueError("Source image and mask geometry mismatch")
        binary = sitk.GetImageFromArray(target.astype(np.uint8))
        binary.CopyInformation(mask_image)
        # Retain anatomical context, but cap browser/free-instance memory explicitly.
        factor = max(1, int(np.ceil(max(image.GetSize()) / 256)))
        if factor > 1:
            reference = sitk.Shrink(image, [factor] * 3)
            image = sitk.Resample(image, reference, sitk.Transform(), sitk.sitkLinear, 0, sitk.sitkFloat32)
            binary = sitk.Resample(binary, reference, sitk.Transform(), sitk.sitkNearestNeighbor, 0, sitk.sitkUInt8)
        if not sitk.GetArrayViewFromImage(binary).any():
            raise ValueError("Tumor lost during demo downsampling")
        # Axis permutation/flipping preserves samples, unlike unrecorded registration.
        image = sitk.DICOMOrient(image, "LPS")
        binary = sitk.DICOMOrient(binary, "LPS")
        sitk.WriteImage(image, str(directory / "image.nii.gz"), True)
        sitk.WriteImage(binary, str(directory / "reference.nii.gz"), True)
        manifest = {"project_id": project, "patient_id": Path(image_name).name.replace(".nii.gz", ""),
                    "modality": modality, "sequence": sequence, "source": "Medical Segmentation Decathlon",
                    "source_url": "https://medicaldecathlon.com/", "archive_url": url,
                    "image_member": image_name, "mask_member": mask_name,
                    "source_image_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
                    "source_mask_sha256": hashlib.sha256(original_mask.read_bytes()).hexdigest(),
                    "license": "CC BY-SA 4.0", "citation": "Simpson et al., A large annotated medical image dataset for the development and evaluation of segmentation algorithms. arXiv:1902.09063.",
                    "derived": True, "downsample_factor": factor, "tumor_label": tumor_label,
                    "note": "Derived public demonstration volume. Nonempty source tumor label and geometry checked; not independent clinical validation.",
                    "reference_voxels": int(np.count_nonzero(sitk.GetArrayViewFromImage(binary)))}
        for name in ("image.nii.gz", "reference.nii.gz"):
            manifest[name + "_sha256"] = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        (directory / "case.json").write_text(json.dumps(manifest, indent=2))
        original.unlink()
        original_mask.unlink()
        print(f"Prepared {project}: {manifest['patient_id']}, {manifest['reference_voxels']} tumor voxels", flush=True)
        return
    raise ValueError(f"No positive case found for {project}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", choices=[*TASKS, "all"], required=True)
    parser.add_argument("--output", type=Path, default=Path("demo-cases"))
    args = parser.parse_args()
    for project in (TASKS if args.project == "all" else [args.project]):
        try:
            prepare(project, args.output)
        except Exception as error:
            print(f"FAILED {project}: {error}", flush=True)
