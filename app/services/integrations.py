from __future__ import annotations

import os
from typing import Any, Dict, Optional


class IntegrationService:
    def summary(self) -> Dict[str, Any]:
        dicomweb_base_url = os.getenv("DICOMWEB_BASE_URL")
        orthanc_url = os.getenv("ORTHANC_URL")
        ohif_base_url = os.getenv("OHIF_BASE_URL")
        smart_fhir_issuer = os.getenv("SMART_FHIR_ISSUER")
        smart_client_id = os.getenv("SMART_CLIENT_ID")
        epic_base_url = os.getenv("EPIC_BASE_URL")
        fhircast_hub_url = os.getenv("FHIRCAST_HUB_URL")

        return {
            "archive": {
                "provider": "orthanc-scaffold",
                "configured": bool(orthanc_url or dicomweb_base_url),
                "orthanc_url": orthanc_url,
                "dicomweb_base_url": dicomweb_base_url,
            },
            "viewer": {
                "provider": "ohif-scaffold",
                "configured": bool(ohif_base_url),
                "base_url": ohif_base_url,
            },
            "ehr": {
                "provider": "epic-smart-on-fhir-scaffold",
                "configured": bool(smart_fhir_issuer and smart_client_id),
                "issuer": smart_fhir_issuer,
                "client_id_configured": bool(smart_client_id),
                "epic_base_url": epic_base_url,
                "fhircast_enabled": bool(fhircast_hub_url),
            },
            "deployment": {
                "mode": os.getenv("DEPLOYMENT_MODE", "local-or-cloud"),
                "local_storage_root": os.getenv("APP_STORAGE_ROOT") or os.getenv("STORAGE_ROOT") or "data/",
            },
        }

    def viewer_launch_context(self, study_id: str, dicom_study_uid: Optional[str]) -> Dict[str, Optional[str]]:
        ohif_base_url = os.getenv("OHIF_BASE_URL")
        ohif_launch_url = None
        if ohif_base_url and dicom_study_uid:
            base = ohif_base_url.rstrip("/")
            ohif_launch_url = f"{base}/viewer?StudyInstanceUIDs={dicom_study_uid}"
        return {
            "internal_workspace_path": f"/?study_id={study_id}",
            "ohif_launch_url": ohif_launch_url,
        }
