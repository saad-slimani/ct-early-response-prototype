"""Real hosted CT/MR inference checks. Proposals are never accepted or approved."""

import argparse
import json
from pathlib import Path
import shlex
import time

import httpx
import numpy as np
import SimpleITK as sitk


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--project", choices=("pancreas-ct", "brain-mr", "both"), default="both")
    args = parser.parse_args()
    config = {}
    for line in args.env_file.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and not key.startswith("#"):
            parsed = shlex.split(value)
            config[key] = parsed[0] if parsed else ""
    with httpx.Client(base_url=args.url, timeout=120) as client:
        assert client.get("/healthz").json()["version"] == "0.2.0"
        assert client.get("/api/workspace").status_code == 401
        client.post("/api/auth/login", json={"username": "saad", "password": config["ONCOMETRA_ADMIN_PASSWORD"]}).raise_for_status()
        model = client.get('/api/lung/model').json()
        assert model['available'] and model['id'] == 'litemedsam-onnx', model
        workspace = client.get("/api/workspace").json()
        assert len(workspace["projects"]) == 7 and len(workspace["cases"]) >= 5
        for project in (("pancreas-ct", "brain-mr") if args.project == "both" else (args.project,)):
            case = next(c for c in workspace["cases"] if c["project_id"] == project)
            response = client.post(f"/api/cases/{case['id']}/assign", json={})
            response.raise_for_status()
            study_id = response.json()["id"]
            base = f"/api/lung/studies/{study_id}"
            metadata = client.get(base).json()
            if metadata["revision_id"] is not None:
                raise RuntimeError("Existing annotation found; refusing to modify it")
            manifest = next(p for p in (Path(__file__).resolve().parents[1] / 'demo-cases').glob('*/case.json')
                            if json.loads(p.read_text())['project_id'] == project)
            reference = sitk.GetArrayFromImage(sitk.ReadImage(str(manifest.parent / 'reference.nii.gz'))) > 0
            frame = int(np.argmax(reference.sum(axis=(1, 2))))
            ys, xs = np.where(reference[frame])
            box = [max(0, int(xs.min()) - 5), max(0, int(ys.min()) - 5),
                   min(reference.shape[2] - 1, int(xs.max()) + 5), min(reference.shape[1] - 1, int(ys.max()) + 5)]
            response = client.post(base + '/jobs', json={'version': metadata['version'], 'plane': 'axial',
                'frame_index': frame, 'box_xyxy': box, 'slice_radius': 2, 'prompt_source': 'reference_derived_technical_test'})
            response.raise_for_status()
            job_id = response.json()['id']
            for _ in range(180):
                result = client.get(base + '/jobs/' + job_id)
                result.raise_for_status()
                job = result.json()
                if job['status'] not in ('queued', 'running'):
                    break
                time.sleep(2)
            assert job['status'] == 'succeeded', job
            assert not job['result']['empty_prediction'], job
            client.get(base + '/mask', params={'job_id': job_id}).raise_for_status()
            assert client.get(base).json()['revision_id'] is None
            print(json.dumps({'project': project, 'study_id': study_id, 'elapsed_seconds': job['result']['elapsed_seconds'],
                'slices': job['result']['slices_processed'], 'volume_ml': job['result']['volume_ml'],
                'source': 'Reference-derived technical test; proposal only, not an approved annotation'}), flush=True)
        client.post("/api/auth/logout", json={}).raise_for_status()


if __name__ == "__main__":
    main()
