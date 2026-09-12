"""Stable, non-overlapping nodule labels within immutable case snapshots."""

from copy import deepcopy
from functools import lru_cache

import numpy as np
import SimpleITK as sitk

MAX_NODULE_LABEL = 255
COLORS = ((104, 237, 182), (116, 177, 255), (246, 156, 118), (225, 205, 105),
          (220, 156, 211), (99, 213, 221), (202, 223, 143), (246, 167, 186))


def new_nodule(label):
    return {"id": f"N{label}", "label": label, "name": f"Lesion {label}",
            "color": list(COLORS[(label - 1) % len(COLORS)]), "mask_revision_id": None,
            "voxel_count": 0, "volume_ml": 0., "centroid_xyz": None, "extent_xyz": None}


def summarize_nodules(mask, nodules, spacing):
    """Slice-wise accumulation avoids allocating full-volume int64 coordinate arrays."""
    counts = np.zeros(256, dtype=np.int64)
    sums = np.zeros((256, 3), dtype=np.float64)
    lower = np.full((256, 3), np.inf)
    upper = np.full((256, 3), -np.inf)
    for z, frame in enumerate(mask):
        ys, xs = np.nonzero(frame)
        if not len(xs):
            continue
        labels = frame[ys, xs]
        counts += np.bincount(labels, minlength=256)
        for axis, coordinates in enumerate((xs, ys, np.full(len(xs), z))):
            sums[:, axis] += np.bincount(labels, weights=coordinates, minlength=256)
            np.minimum.at(lower[:, axis], labels, coordinates)
            np.maximum.at(upper[:, axis], labels, coordinates)
    known = {nodule["label"] for nodule in nodules}
    if set(np.flatnonzero(counts)) - known:
        raise ValueError("Mask contains a label with no nodule identity")
    result = deepcopy(nodules)
    for nodule in result:
        label = nodule["label"]
        count = int(counts[label])
        nodule.update(voxel_count=count, volume_ml=float(count * np.prod(spacing) / 1000),
                      centroid_xyz=(sums[label] / count).tolist() if count else None,
                      extent_xyz=[int(value) for pair in zip(lower[label], upper[label]) for value in pair] if count else None)
    return result


@lru_cache(maxsize=128)
def legacy_nodule(path, revision_id):
    image = sitk.ReadImage(path)
    nodule = new_nodule(1)
    nodule["mask_revision_id"] = revision_id
    return summarize_nodules(sitk.GetArrayViewFromImage(image), [nodule], image.GetSpacing())


def snapshot_nodules(revision, path=None):
    if revision is None:
        return [new_nodule(1)]
    if "nodules" in revision.details:
        return deepcopy(revision.details["nodules"])
    # Preserve legacy disconnected regions as N1; never infer separate lesions.
    return deepcopy(legacy_nodule(str(path), revision.id))
