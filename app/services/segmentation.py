from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import SimpleITK as sitk


@dataclass
class SeedPoint:
    z: int
    y: int
    x: int


class Segmenter:
    def run(
        self,
        image_path: Path,
        output_mask_path: Path,
        seed: Optional[SeedPoint] = None,
    ) -> Tuple[str, Path]:
        # Adapter order: external open-source models first, robust fallback second.
        if self._totalsegmentator_available():
            try:
                self._run_totalsegmentator(image_path, output_mask_path)
                return "totalsegmentator", output_mask_path
            except Exception:
                pass

        self._run_region_growing_fallback(image_path, output_mask_path, seed)
        return "region-growing-fallback", output_mask_path

    def _totalsegmentator_available(self) -> bool:
        return shutil.which("TotalSegmentator") is not None

    def _run_totalsegmentator(self, image_path: Path, output_mask_path: Path) -> None:
        # Note: TotalSegmentator does not provide a universal "solid tumor" label.
        # We run it and merge likely lesion-relevant masks if available.
        task = os.getenv("TUMOR_SEG_TASK", "total")
        with tempfile.TemporaryDirectory() as td:
            outdir = Path(td) / "ts_out"
            cmd = [
                "TotalSegmentator",
                "-i",
                str(image_path),
                "-o",
                str(outdir),
                "--task",
                task,
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            candidates = list(outdir.glob("*tumor*.nii.gz")) + list(outdir.glob("*lesion*.nii.gz"))
            if not candidates:
                raise RuntimeError("No tumor/lesion output from TotalSegmentator")

            ref = sitk.ReadImage(str(image_path))
            merged = np.zeros_like(sitk.GetArrayFromImage(ref), dtype=np.uint8)
            for c in candidates:
                arr = sitk.GetArrayFromImage(sitk.ReadImage(str(c)))
                merged = np.logical_or(merged, arr > 0)
            out = sitk.GetImageFromArray(merged.astype(np.uint8))
            out.CopyInformation(ref)
            sitk.WriteImage(out, str(output_mask_path), useCompression=True)

    def _run_region_growing_fallback(
        self,
        image_path: Path,
        output_mask_path: Path,
        seed: Optional[SeedPoint],
    ) -> None:
        image = sitk.ReadImage(str(image_path))
        arr = sitk.GetArrayFromImage(image)

        if seed is None:
            # Heuristic seed at max intensity in central torso cube.
            z0, y0, x0 = [max(0, s // 4) for s in arr.shape]
            z1, y1, x1 = [min(s, 3 * s // 4) for s in arr.shape]
            core = arr[z0:z1, y0:y1, x0:x1]
            rz, ry, rx = np.unravel_index(np.argmax(core), core.shape)
            seed = SeedPoint(z=z0 + int(rz), y=y0 + int(ry), x=x0 + int(rx))

        lower = float(np.percentile(arr, 80))
        upper = float(np.percentile(arr, 99.7))
        seed_value = float(arr[int(seed.z), int(seed.y), int(seed.x)])
        lower = min(lower, seed_value - 25.0)
        upper = max(upper, seed_value + 25.0)

        # Restrict the grow to a bounded crop so fallback remains responsive on free-tier demo deployments.
        crop_radius = (12, 32, 32)
        z0 = max(0, int(seed.z) - crop_radius[0])
        z1 = min(arr.shape[0], int(seed.z) + crop_radius[0] + 1)
        y0 = max(0, int(seed.y) - crop_radius[1])
        y1 = min(arr.shape[1], int(seed.y) + crop_radius[1] + 1)
        x0 = max(0, int(seed.x) - crop_radius[2])
        x1 = min(arr.shape[2], int(seed.x) + crop_radius[2] + 1)

        crop = arr[z0:z1, y0:y1, x0:x1]
        candidate = np.logical_and(crop >= lower, crop <= upper)

        local_seed = (int(seed.z) - z0, int(seed.y) - y0, int(seed.x) - x0)
        grown = np.zeros_like(candidate, dtype=np.uint8)
        if candidate[local_seed]:
            queue: deque[Tuple[int, int, int]] = deque([local_seed])
            grown[local_seed] = 1
            neighbors = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))

            while queue:
                cz, cy, cx = queue.popleft()
                for dz, dy, dx in neighbors:
                    nz, ny, nx = cz + dz, cy + dy, cx + dx
                    if not (0 <= nz < candidate.shape[0] and 0 <= ny < candidate.shape[1] and 0 <= nx < candidate.shape[2]):
                        continue
                    if grown[nz, ny, nx] or not candidate[nz, ny, nx]:
                        continue
                    grown[nz, ny, nx] = 1
                    queue.append((nz, ny, nx))

        if not grown.any():
            grown[max(0, local_seed[0] - 1) : local_seed[0] + 2, max(0, local_seed[1] - 3) : local_seed[1] + 4, max(0, local_seed[2] - 3) : local_seed[2] + 4] = 1

        mask = np.zeros_like(arr, dtype=np.uint8)
        mask[z0:z1, y0:y1, x0:x1] = grown
        out = sitk.GetImageFromArray(mask.astype(np.uint8))
        out.CopyInformation(image)
        sitk.WriteImage(out, str(output_mask_path), useCompression=True)
