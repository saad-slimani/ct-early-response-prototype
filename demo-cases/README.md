# Public Demonstration Data

These are derived subsets of the **Medical Segmentation Decathlon (MSD)**,
attributed to the contributing investigators and institutions described by
Simpson et al., *A large annotated medical image dataset for the development and
evaluation of segmentation algorithms*, [arXiv:1902.09063](https://arxiv.org/abs/1902.09063).
Dataset source: https://medicaldecathlon.com/.

The included images, masks and their adaptations are distributed under
[Creative Commons Attribution-ShareAlike 4.0 International](https://creativecommons.org/licenses/by-sa/4.0/).
The [full license](https://creativecommons.org/licenses/by-sa/4.0/legalcode) applies.
Keep attribution, indicate further changes, and apply the same license to shared
adaptations of this data. This data license does not purport to relicense application code.
No endorsement by the original investigators is implied. Data is supplied without
warranties and is not independently clinically validated by this application.

Included tasks: pancreas CT (Task07), liver CT (Task03), lung CT (Task06), brain MRI
(Task01), and colon CT (Task10). Each `case.json` identifies the exact source archive,
members, target labels, derivation, hashes and nonzero reference-mask voxel count.
Pancreas/liver use tumor label 2; lung/colon use tumor label 1. Brain uses the union
of tumor-associated labels, not an assertion that edema is viable tumor.

Changes: canonical LPS orientation, float32 images, nearest-neighbor target-label
conversion, and isotropic integer downsampling where needed to keep the largest
dimension at most 256 voxels. Brain MRI uses the T1 post-contrast channel only.
The released reference is explicitly marked practice-only when copied into a reading.

`scripts/prepare_demo_cases.py` reproduces these assets using HTTP byte-range
requests against the MONAI-hosted original MSD tar archives. It does not download
the complete multi-gigabyte collections. Do not substitute unrelated ULS-derived
archives without reviewing their different license restrictions.
