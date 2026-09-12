#!/usr/bin/env bash
set -euo pipefail
python -m pip install --no-cache-dir numpy==1.26.4 setuptools==69.5.1 wheel
python -m pip install --no-cache-dir --no-build-isolation -r requirements-demo.txt
python -c 'from app.services.medsam_runtime import verify_model; print("Verified model:", verify_model()["name"])'
