"""Deployment smoke tests, isolated from the user's local annotation database."""

import io
import json
import os
from pathlib import Path
import tempfile
import time
import zipfile
from uuid import uuid4

import numpy as np
import pytest
import SimpleITK as sitk

TEMP = tempfile.TemporaryDirectory(prefix="oncometra-test-")
os.environ["APP_STORAGE_ROOT"] = TEMP.name
os.environ["ONCOMETRA_ADMIN_PASSWORD"] = "test-admin-password-1234"
os.environ.pop("DATABASE_URL", None)

from fastapi.testclient import TestClient
from app.oncometra import app, image_directory, SessionLocal, WorkspaceCase, WorkspaceFeatureJob, lung
from app.db import Base, engine


@pytest.fixture
def clients():
    Base.metadata.drop_all(engine)
    lung.image_for.cache_clear()
    with TestClient(app) as admin:
        assert admin.post("/api/auth/login", json={"username": "saad", "password": os.environ["ONCOMETRA_ADMIN_PASSWORD"]}).status_code == 200
        for username, role in [("reader", "junior_annotator"), ("reviewer", "reviewer")]:
            assert admin.post("/api/users", json={"username": username, "name": username.title(), "role": role, "password": "test-reader-password-1234"}).status_code == 201
        with TestClient(app, raise_server_exceptions=True) as reader, TestClient(app) as reviewer:
            for client, username in ((reader,"reader"),(reviewer,"reviewer")):
                assert client.post("/api/auth/login", json={"username":username,"password":"test-reader-password-1234"}).status_code == 200
            yield admin, reader, reviewer


def fixture_case(modality="CT"):
    # Synthetic volumes are test fixtures only, never demo assets.
    array = np.arange(24**3, dtype=np.float32).reshape(24,24,24) / 17.3 - 400
    image = sitk.GetImageFromArray(array)
    image.SetSpacing((1.1,1.2,1.3))
    mask = sitk.GetImageFromArray(np.pad(np.ones((8,8,8),np.uint8),8))
    mask.CopyInformation(image)
    case = WorkspaceCase(id=str(uuid4()), project_id="brain-mr" if modality=="MR" else "lung-ct", patient_id="TEST-"+str(uuid4())[:8], modality=modality,
                         source={"source":"Synthetic unit-test fixture", "reference_voxels":512})
    directory = image_directory(case)
    directory.mkdir(parents=True)
    sitk.WriteImage(image,str(directory/"image.nii.gz"))
    sitk.WriteImage(mask,str(directory/"reference.nii.gz"))
    with SessionLocal() as db:
        db.add(case)
        db.commit()
    return case


def assigned(client, case):
    result=client.post(f"/api/cases/{case.id}/assign",json={})
    assert result.status_code==200,result.text
    return result.json()["id"]


def complete(client, study_id):
    base=f"/api/lung/studies/{study_id}"
    state=client.get(base).json()
    result=client.post(f"/api/assignments/{study_id}/reference",json={"version":state["version"]})
    assert result.status_code==200,result.text
    state=client.get(base).json()
    result=client.post(base+"/complete",json={"version":state["version"],"revision_id":state["revision_id"],"reviewed":True})
    assert result.status_code==200,result.text
    return client.get(base).json()


def test_authentication_and_csrf(clients):
    admin, reader, reviewer=clients
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/workspace').status_code==401
        assert anonymous.get('/api/lung/identities',headers={'X-Annotation-Actor':'reviewer'}).status_code==401
        assert anonymous.get('/healthz').status_code==200
    assert reader.post('/api/users',json={'username':'hacker','name':'Unauthorized','role':'admin','password':'test-invalid-account'}).status_code==403
    assert admin.post('/api/auth/logout',json={},headers={'Origin':'https://untrusted.example'}).status_code==403
    assert reader.get('/api/lung/identities',headers={'X-Annotation-Actor':'reviewer'}).json()['current']['role']=='junior_annotator'


def test_reader_isolation_and_independent_approval(clients):
    admin, reader, reviewer=clients
    case=fixture_case()
    mine=assigned(reader,case)
    other=assigned(reviewer,case)
    assert mine!=other
    assert reader.get(f'/api/lung/studies/{other}/mask').status_code==403
    assert reader.get(f'/api/lung/studies/{other}/volume').status_code==403
    state=complete(reader,mine)
    assert reviewer.get(f'/api/lung/studies/{other}').json()['revision_id'] is None
    request={'version':state['version'],'revision_id':state['revision_id'],'reviewed':True}
    assert reader.post(f'/api/lung/studies/{mine}/approve',json=request).status_code==403
    assert reviewer.post(f'/api/lung/studies/{mine}/approve',json=request).status_code==200
    assert reader.post(f'/api/lung/studies/{mine}/strokes',json={'version':state['version'],'plane':'axial','slice_index':10,'points':[[10,10]],'radius_mm':2}).status_code==409
    approved=reviewer.get(f'/api/lung/studies/{mine}').json()
    returned=reviewer.post(f'/api/assignments/{mine}/return',json={'version':approved['version'],'comment':'Review the superior boundary'})
    assert returned.status_code==200
    assert reader.get(f'/api/lung/studies/{mine}').json()['annotation_status']=='in_progress'
    own=complete(reviewer,other)
    assert reviewer.post(f'/api/lung/studies/{other}/approve',json={'version':own['version'],'revision_id':own['revision_id'],'reviewed':True}).status_code==403


def test_float_mri_and_export_provenance(clients):
    admin, reader, reviewer=clients
    case=fixture_case('MR')
    study_id=assigned(reader,case)
    base=f'/api/lung/studies/{study_id}'
    state=reader.get(base).json()
    assert state['modality']=='MR' and state['transfer_dtype']=='float32-le'
    pixels=np.frombuffer(reader.get(base+'/volume').content,dtype='<f4')
    assert pixels[1]==pytest.approx(1/17.3-400)
    state=complete(reader,study_id)
    result=reader.get(base+f"/exports/{state['revision_id']}",params={'version':state['version'],'format':'bundle'})
    assert result.status_code==200,result.text
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        assert 'image.nii.gz' in archive.namelist()
        manifest=json.loads(archive.read('manifest.json'))
        assert manifest['identity_mode']=='authenticated'
        assert manifest['modality']=='MR'
        assert manifest['revision']['details']['practice_only'] is True
        assert manifest['review']['actor']['username']=='reader'


def test_adjudication_preserves_source_and_comparison(clients):
    admin, reader, reviewer=clients
    case=fixture_case()
    original=assigned(reader,case)
    first=complete(reader,original)
    copy=reviewer.post(f'/api/assignments/{original}/adjudicate',json={})
    assert copy.status_code==200,copy.text
    assert copy.json()['kind']=='adjudication'
    assert copy.json()['id']!=original
    assert reader.get(f'/api/lung/studies/{original}').json()['revision_id']==first['revision_id']
    assert reader.get(f"/api/lung/studies/{copy.json()['id']}/mask").status_code==403
    another=assigned(reviewer,case)
    complete(reviewer,another)
    compared=reviewer.post('/api/review/compare',json={'a':original,'b':another})
    assert compared.status_code==200,compared.text
    assert compared.json()['dice']==1


def test_pyradiomics_is_real_versioned_and_invalidated(clients):
    admin, reader, reviewer=clients
    study_id=assigned(reader,fixture_case())
    completed=complete(reader,study_id)
    result=reader.post(f'/api/assignments/{study_id}/radiomics',json={})
    assert result.status_code==202,result.text
    job_id=result.json()['id']
    for _ in range(200):
        jobs=reader.get('/api/radiomics').json()
        job=next(row for row in jobs if row['id']==job_id)
        if job['status'] in ('succeeded','failed'):
            break
        time.sleep(.05)
    assert job['status']=='succeeded',job
    assert job['result']['pyradiomics_version']=='v3.0.1'
    assert len(job['result']['rows'][0]['features'])>90
    assert 'original_shape_MeshVolume' in job['result']['rows'][0]['features']
    assert not job['stale']
    csv_export=reader.get(f'/api/radiomics/{job_id}/export')
    assert csv_export.status_code==200 and 'original_shape_MeshVolume' in csv_export.text
    assert reader.post(f'/api/lung/studies/{study_id}/reopen',json={'version':completed['version']}).status_code==200
    assert reader.get('/api/radiomics').json()[0]['stale']


def test_import_limits_and_valid_mri(clients):
    admin, reader, reviewer=clients
    case=fixture_case('MR')
    payload=(image_directory(case)/'image.nii.gz').read_bytes()
    result=reader.post('/api/import',data={'project_id':'brain-mr','patient_id':'UPLOADED-MR','deidentified':'true'},files={'files':('image.nii.gz',payload)})
    assert result.status_code==201,result.text
    assert reader.post('/api/import',data={'project_id':'brain-mr','patient_id':'UPLOADED-MR','deidentified':'false'},files={'files':('image.nii.gz',payload)}).status_code==422
    result=reader.post('/api/import',data={'project_id':'brain-mr','patient_id':'UPLOADED-MR','deidentified':'true'},files={'files':('image.nii.gz',payload)})
    assert result.status_code==422
