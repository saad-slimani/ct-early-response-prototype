"""Real-model technical checks using public reference-derived boxes, not a clinical benchmark."""

import json
from pathlib import Path
import tempfile
import time
import subprocess
import sys

import numpy as np
import SimpleITK as sitk

from app.services.lung_geometry import image_for_plane, same_grid
from app.services.medsam_jobs import stage_job, collect_result


def main():
    root = Path(__file__).resolve().parents[1]
    results = []
    for project in ("pancreas-ct", "brain-mr"):
        case_dir = next(path.parent for path in (root / 'demo-cases').glob('*/case.json')
                        if json.loads(path.read_text())['project_id'] == project)
        info = json.loads((case_dir / 'case.json').read_text())
        image = sitk.ReadImage(str(case_dir / 'image.nii.gz'))
        for plane in ("axial", "coronal", "sagittal"):
            truth = sitk.GetArrayFromImage(image_for_plane(sitk.ReadImage(str(case_dir / 'reference.nii.gz')), plane)) > 0
            frame = int(np.argmax(truth.sum(axis=(1, 2))))
            ys, xs = np.where(truth[frame])
            box = [max(0, int(xs.min()) - 5), max(0, int(ys.min()) - 5),
                   min(truth.shape[2] - 1, int(xs.max()) + 5), min(truth.shape[1] - 1, int(ys.max()) + 5)]
            with tempfile.TemporaryDirectory(prefix='medsam-smoke-') as directory:
                folder = Path(directory)
                request = {"image": str(case_dir / 'image.nii.gz'), "plane": plane, "modality": info['modality'],
                           "frame_index": frame, "box_xyxy": box, "slice_radius": 0, "window_level": 40, "window_width": 400}
                stage_job(folder, image, request)
                subprocess.run([sys.executable, '-m', 'app.services.medsam_worker', str(folder / 'request.json')], check=True)
                collect_result(folder, image)
                mask = sitk.ReadImage(str(folder / 'proposal.nii.gz'))
                same_grid(mask, image)
                prediction = sitk.GetArrayFromImage(image_for_plane(mask, plane)) > 0
                assert prediction.any(), (project, plane, 'empty prediction')
                assert not prediction[:frame].any() and not prediction[frame+1:].any()
                result = json.loads((folder / 'result.json').read_text())
                result.update(project=project, plane=plane, prompt_source='reference-derived smoke-test box',
                    dice_prompt_slice=2 * int((prediction[frame] & truth[frame]).sum()) / int(prediction[frame].sum() + truth[frame].sum()))
                results.append(result)
                print(json.dumps({key: result[key] for key in ('project', 'plane', 'elapsed_seconds', 'volume_ml', 'dice_prompt_slice')}), flush=True)
    target = root / 'docs/litemedsam-technical-smoke.json'
    target.write_text(json.dumps({"notice": "Reference-derived prompts; no independent accuracy claim", "results": results}, indent=2) + '\n')


if __name__ == '__main__':
    main()
