"""Private deployment snapshots and idempotent collaborator provisioning."""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shlex
import zipfile

import httpx
import numpy as np
import SimpleITK as sitk


def private_file(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        output.write(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "provision"))
    parser.add_argument("--url", default="https://oncometra-demo.onrender.com")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--username", default="laurent")
    parser.add_argument("--name", default="Laurent")
    parser.add_argument("--role", choices=("junior_annotator", "senior_annotator", "reviewer"), default="junior_annotator")
    args = parser.parse_args()
    config = {}
    for line in args.env_file.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and not key.startswith("#"):
            config[key] = shlex.split(value)[0] if value else ""
    args.private_dir.mkdir(parents=True, exist_ok=True)
    args.private_dir.chmod(0o700)
    with httpx.Client(base_url=args.url, timeout=180) as client:
        client.post('/api/auth/login', json={'username': 'saad', 'password': config['ONCOMETRA_ADMIN_PASSWORD']}).raise_for_status()

        def get(path):
            response = client.get(path)
            response.raise_for_status()
            return response

        if args.action == 'snapshot':
            workspace = get('/api/workspace').json()
            target = args.private_dir / ('snapshot-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.zip')
            with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name, data in (('workspace', workspace), ('users', get('/api/users').json()), ('feature-jobs', get('/api/radiomics').json())):
                    archive.writestr(name + '.json', json.dumps(data, indent=2))
                for row in workspace['assignments']:
                    base = '/api/lung/studies/' + row['id']
                    meta = get(base).json()
                    archive.writestr(row['id'] + '/metadata.json', json.dumps(meta, indent=2))
                    archive.writestr(row['id'] + '/volume.bin', get(base + '/volume').content)
                    for revision in meta['history']:
                        archive.writestr(row['id'] + '/' + revision['id'] + '.nii.gz', get(base + '/export/' + revision['id']).content)
            with zipfile.ZipFile(target) as archive:
                if archive.testzip() is not None:
                    raise RuntimeError('Snapshot integrity check failed')
            print(f'Saved {len(workspace["assignments"])} assignments with metadata, image pixels, mask revisions and feature results: {target}')
        else:
            path = args.private_dir / (args.username + '-account.json')
            if path.exists():
                account = json.loads(path.read_text())
                if account['username'] != args.username or account['role'] != args.role:
                    raise RuntimeError('Existing credential record differs; refusing to overwrite it')
            else:
                account = {'username': args.username, 'name': args.name, 'password': secrets.token_urlsafe(24), 'role': args.role}
                private_file(path, json.dumps({'url': args.url, **account}, indent=2) + '\n')
            existing = next((user for user in get('/api/users').json() if user['username'] == args.username), None)
            if not existing:
                client.post('/api/users', json={key: account[key] for key in ('username', 'name', 'password', 'role')}).raise_for_status()
            elif existing['role'] != account['role']:
                raise RuntimeError('Existing live account has another role; refusing to change it silently')
            with httpx.Client(base_url=args.url, timeout=120) as collaborator:
                result = collaborator.post('/api/auth/login', json={key: account[key] for key in ('username', 'password')})
                result.raise_for_status()
                collaborator.post('/api/auth/logout').raise_for_status()
            bootstrap = args.private_dir / (args.username + '-bootstrap.txt')
            if not bootstrap.exists():
                salt = secrets.token_hex(16)
                hashed = hashlib.pbkdf2_hmac('sha256', account['password'].encode(), bytes.fromhex(salt), 600000).hex()
                value = [{key: account[key] for key in ('username', 'name', 'role')} | {'password_hash': salt + '$' + hashed}]
                private_file(bootstrap, 'ONCOMETRA_BOOTSTRAP_USERS=' + json.dumps(value, separators=(',', ':')) + '\n')
            print(f'Account verified: {args.username} / {account["role"]}. Credentials: {path}. Restart bootstrap: {bootstrap}')
        client.post('/api/auth/logout').raise_for_status()


if __name__ == '__main__':
    main()
