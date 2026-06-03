from __future__ import annotations

from typing import Any, Dict, List


MODEL_CATALOG: List[Dict[str, Any]] = [
    {
        "id": "totalsegmentator-ct",
        "name": "TotalSegmentator CT",
        "task_type": "segmentation",
        "modality": "CT",
        "body_region": "whole-body",
        "execution_mode": "sync-if-installed",
        "output_type": "segmentation-mask",
        "open_source": True,
        "local_tunable": False,
        "status": "available",
        "description": "Open-source CT segmentation adapter wired into the current MVP.",
    },
    {
        "id": "radiomics-early-response-v1",
        "name": "Radiomics Early Response",
        "task_type": "early_response",
        "modality": "CT",
        "body_region": "oncology",
        "execution_mode": "sync",
        "output_type": "score-json",
        "open_source": True,
        "local_tunable": True,
        "status": "available",
        "description": "Computes a 0-100 early response score from baseline and follow-up masks.",
    },
    {
        "id": "report-assist-template-v1",
        "name": "Report Assist Template",
        "task_type": "report_draft",
        "modality": "CT",
        "body_region": "general",
        "execution_mode": "sync",
        "output_type": "report-text",
        "open_source": False,
        "local_tunable": True,
        "status": "available",
        "description": "Generates a reviewable report draft from study metadata and available AI artifacts.",
    },
    {
        "id": "tenant-finetune-slot",
        "name": "Tenant Fine-Tune Slot",
        "task_type": "fine_tune",
        "modality": "CT",
        "body_region": "site-specific",
        "execution_mode": "planned",
        "output_type": "training-run",
        "open_source": True,
        "local_tunable": True,
        "status": "planned",
        "description": "Scaffold for local fine-tuning jobs on accepted segmentations and site-owned labels.",
    },
]
