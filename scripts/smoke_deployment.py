"""Exercise a disposable deployment; never overwrites an existing annotation."""

import argparse
from pathlib import Path
import shlex
import time

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    config = {}
    for line in args.env_file.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and not key.startswith("#"):
            parsed = shlex.split(value)
            config[key] = parsed[0] if parsed else ""
    with httpx.Client(base_url=args.url, timeout=120) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.get("/api/workspace").status_code == 401
        login = client.post("/api/auth/login", json={"username": "saad", "password": config["ONCOMETRA_ADMIN_PASSWORD"]})
        login.raise_for_status()
        workspace = client.get("/api/workspace").json()
        assert len(workspace["projects"]) == 7 and len(workspace["cases"]) >= 5
        print("Authenticated; seven projects and five public cases available", flush=True)
        case = next(c for c in workspace["cases"] if c["project_id"] == "pancreas-ct")
        response = client.post(f"/api/cases/{case['id']}/assign", json={})
        response.raise_for_status()
        study_id = response.json()["id"]
        base = f"/api/lung/studies/{study_id}"
        metadata = client.get(base).json()
        if metadata["revision_id"] is not None:
            raise RuntimeError("Existing annotation found; refusing to modify it")
        client.post(base + "/open", json={"version": metadata["version"]}).raise_for_status()
        metadata = client.get(base).json()
        client.post(f"/api/assignments/{study_id}/reference", json={"version": metadata["version"]}).raise_for_status()
        metadata = client.get(base).json()
        client.post(base + "/complete", json={"version": metadata["version"], "revision_id": metadata["revision_id"], "reviewed": True}).raise_for_status()
        print("Public reference practice submission completed; no independent approval claimed", flush=True)
        response = client.post(f"/api/assignments/{study_id}/radiomics", json={})
        response.raise_for_status()
        job_id = response.json()["id"]
        for _ in range(120):
            jobs = client.get("/api/radiomics").json()
            job = next(j for j in jobs if j["id"] == job_id)
            if job["status"] in ("succeeded", "failed"):
                break
            time.sleep(2)
        assert job["status"] == "succeeded", job.get("error") or job["status"]
        count = len(job["result"]["rows"][0]["features"])
        assert count > 90
        exported = client.get(f"/api/radiomics/{job_id}/export")
        exported.raise_for_status()
        assert "original_shape_MeshVolume" in exported.text
        print(f"PyRadiomics succeeded: {count} features, versioned CSV verified", flush=True)
        client.post("/api/auth/logout", json={}).raise_for_status()


if __name__ == "__main__":
    main()
