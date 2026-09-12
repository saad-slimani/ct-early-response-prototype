"""Native-grid CT crops and physical brush edits shared by the worker and API."""

import numpy as np
import SimpleITK as sitk

MAX_VOXELS = 128 * 1024 * 1024
PLANE_AXES = {"axial": (0, 1, 2), "coronal": (0, 2, 1), "sagittal": (1, 2, 0)}


def plane_axes(plane):
    if plane not in PLANE_AXES:
        raise ValueError("Prompt plane must be axial, coronal, or sagittal")
    return PLANE_AXES[plane]


def validate_image(image):
    if image.GetDimension() != 3 or image.GetNumberOfComponentsPerPixel() != 1:
        raise ValueError("A scalar 3D CT series is required")
    if any(n < 2 for n in image.GetSize()) or np.prod(image.GetSize()) > MAX_VOXELS:
        raise ValueError("CT must have at least two voxels per axis and at most 128 million voxels")
    if not np.isfinite(image.GetSpacing()).all() or min(image.GetSpacing()) <= 0:
        raise ValueError("Invalid voxel spacing")
    direction = np.asarray(image.GetDirection()).reshape(3, 3)
    if not np.isfinite(image.GetOrigin()).all() or not np.allclose(direction.T @ direction, np.eye(3), atol=1e-4):
        raise ValueError("Invalid image geometry")


def crop_for_prompt(image, frame, box, plane="axial"):
    validate_image(image)
    a, b, normal = plane_axes(plane)
    sx, sy, sz = [image.GetSize()[axis] for axis in (a, b, normal)]
    x0, y0, x1, y1 = box
    if not np.isfinite(box).all() or not (0 <= x0 < x1 < sx and 0 <= y0 < y1 < sy and 0 <= frame < sz):
        raise ValueError(f"Draw a nonempty box fully inside the {plane} image")
    if x1 - x0 > 224 or y1 - y0 > 224:
        raise ValueError("Local inference supports boxes up to 224 pixels per side; select one lesion")
    plane_sizes = [min(sx, max(128, int(np.ceil(x1 - x0)) + 32)),
                   min(sy, max(128, int(np.ceil(y1 - y0)) + 32)), min(sz, 64)]
    sizes, centers = [0] * 3, [0] * 3
    for axis, size, center in zip((a, b, normal), plane_sizes, [(x0 + x1) / 2, (y0 + y1) / 2, frame]):
        sizes[axis], centers[axis] = size, center
    starts = [max(0, min(n - size, int(center) - size // 2))
              for n, size, center in zip(image.GetSize(), sizes, centers)]
    prompt = {"frame_index": frame - starts[normal],
              "box_xyxy": [x0 - starts[a], y0 - starts[b], x1 - starts[a], y1 - starts[b]]}
    return sitk.RegionOfInterest(image, sizes, starts), starts, prompt


def image_for_plane(image, plane="axial"):
    """Reorder native axes to model (u, v, slice); no intensity interpolation."""
    return sitk.PermuteAxes(image, list(plane_axes(plane)))


def mask_from_plane(prediction, native_crop, plane="axial"):
    oriented = image_for_plane(native_crop, plane)
    if tuple(prediction.shape) != oriented.GetSize()[::-1]:
        raise ValueError("Model mask dimensions do not match the prompt-plane crop")
    mask = sitk.GetImageFromArray(np.asarray(prediction, dtype=np.uint8))
    mask.CopyInformation(oriented)
    restored = sitk.PermuteAxes(mask, [int(axis) for axis in np.argsort(plane_axes(plane))])
    same_grid(restored, native_crop)
    return restored


def restore_crop(mask, starts, reference):
    output = sitk.Image(reference.GetSize(), sitk.sitkUInt8)
    output.CopyInformation(reference)
    return sitk.Paste(output, sitk.Cast(mask, sitk.sitkUInt8), mask.GetSize(), [0, 0, 0], starts)


def same_grid(image, reference):
    if image.GetSize() != reference.GetSize() or any(
        not np.allclose(a, b, rtol=0, atol=1e-5) for a, b in (
            (image.GetSpacing(), reference.GetSpacing()), (image.GetOrigin(), reference.GetOrigin()),
            (image.GetDirection(), reference.GetDirection()))
    ):
        raise ValueError("Mask geometry does not match the CT")


def paint_stroke(mask, spacing_xyz, plane, index, points, radius_mm, erase=False, spherical=False, label=1, feedback=None):
    if not 1 <= label <= 255:
        raise ValueError("Invalid nodule label")
    axis = {"axial": 0, "coronal": 1, "sagittal": 2}[plane]
    if not 0 <= index < mask.shape[axis]:
        raise ValueError("Slice outside volume")
    spacing = np.asarray(spacing_xyz[::-1])
    centers = []
    for u, v in points:
        center = np.array([index, v, u] if axis == 0 else ([v, index, u] if axis == 1 else [v, u, index]))
        if not np.isfinite(center).all() or (center < 0).any() or (center > np.array(mask.shape) - 1).any():
            raise ValueError("Brush point outside volume")
        if centers:
            previous = centers[-1]
            steps = int(np.ceil(np.linalg.norm((center - previous) * spacing) / max(radius_mm / 2, 0.5)))
            if steps > 4096:
                raise ValueError("Stroke is too long")
            if len(centers) + max(1, steps) > 10000:
                raise ValueError("Stroke is too complex; use shorter strokes")
            centers.extend(np.linspace(previous, center, max(2, steps + 1))[1:])
        else:
            centers.append(center)
    if len(centers) > 10000:
        raise ValueError("Stroke is too complex")
    work_voxels = 0
    for center in centers:
        radii = radius_mm / spacing
        if not spherical:
            radii[axis] = 0
        lo = np.maximum(0, np.floor(center - radii).astype(int))
        hi = np.minimum(mask.shape, np.ceil(center + radii).astype(int) + 1)
        work_voxels += int(np.prod(hi - lo))
        if work_voxels > 32 * 1024 * 1024:
            raise ValueError("Stroke exceeds the local compute limit; use a smaller radius or shorter stroke")
        zz, yy, xx = np.ogrid[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]]
        distance = sum(((coords - value) * step) ** 2 for coords, value, step in zip((zz, yy, xx), center, spacing))
        region = mask[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]]
        footprint = distance <= radius_mm ** 2
        if feedback is not None and np.any(footprint & (region != 0) & (region != label)):
            feedback["protected_other_nodules"] = True
        editable = region == label if erase else (region == 0) | (region == label)
        region[footprint & editable] = 0 if erase else label
    return mask
