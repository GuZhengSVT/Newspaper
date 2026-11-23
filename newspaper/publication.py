"""Content-addressed public releases and GitHub Pages delivery receipts."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

import httpx

from .storage import atomic_json


def _file_hashes(directory: Path) -> dict[str, str]:
    files = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('Public releases must contain regular files')
        if path.is_file():
            name = path.relative_to(directory).as_posix()
            if name not in {'site-version.txt', 'site-manifest.json'}:
                files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def write_manifest(directory: Path) -> str:
    files = _file_hashes(directory)
    payload = json.dumps(files, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    version = hashlib.sha256(payload).hexdigest()
    (directory / 'site-manifest.json').write_bytes(payload)
    (directory / 'site-version.txt').write_text(version + '\n')
    return version


def matches_current(settings, version):
    if local_version(settings) != version:
        return False
    current = settings.web / 'current'
    try:
        manifest = (current / 'site-manifest.json').read_bytes()
        return hashlib.sha256(manifest).hexdigest() == version and json.loads(manifest) == _file_hashes(current)
    except (FileNotFoundError, ValueError):
        return False


def local_version(settings):
    try:
        version = (settings.web / 'current/site-version.txt').read_text().strip()
    except FileNotFoundError:
        return None
    return version if re.fullmatch('[a-f0-9]{64}', version) else None


def _receipt(settings):
    config = settings.values.get('publication', {})
    target = [settings.base_url, config.get('repository'), config.get('ref', 'main')]
    if config.get('mode') == 'pages_branch':
        target.append('pages_branch')
    try:
        state = json.loads((settings.data / 'pages-state.json').read_text())
    except (ValueError, FileNotFoundError):
        state = {}
    return state if isinstance(state, dict) and state.get('target') == target else {'target': target}


def status(settings):
    mode = settings.values.get('publication', {}).get('mode', 'local')
    version = local_version(settings)
    receipt = _receipt(settings) if mode != 'local' else {}
    public = version if mode == 'local' else receipt.get('confirmed')
    live = version == public and not receipt.get('requested')
    if mode == 'pages_branch':
        live = live and receipt.get('release') == (settings.web / 'current').resolve().name
    return {'mode': mode, 'local_version': version, 'public_version': public,
            'state': 'unbuilt' if not version else 'live' if live else 'awaiting_publication'}


def delivery_ready(settings, store, keys):
    """Confirm that the documents we would send match the public revision."""
    pages = settings.values.get('publication', {}).get('mode') in {'pages', 'pages_branch'}
    deferred_translation = settings.values.get('translation', {}).get('publish') == 'chinese_first'
    if not pages and not deferred_translation and 'publication' not in settings.values:
        # Direct hosting publishes by atomic symlink swap in the generation transaction.
        return True
    if status(settings)['state'] != 'live':
        return False
    try:
        manifest = json.loads((settings.web / 'current/site-manifest.json').read_text())
    except (ValueError, FileNotFoundError):
        return False
    for key in keys:
        payload = json.dumps(store.document(key), ensure_ascii=False, indent=2).encode()
        if manifest.get(f'downloads/{key}.json') != hashlib.sha256(payload).hexdigest():
            return False
    return True


def sync_pages(settings):
    """Read back the public version before recording success; never run the editor."""
    mode = settings.values.get('publication', {}).get('mode')
    if mode not in {'pages', 'pages_branch'}:
        return status(settings)
    settings.data.mkdir(parents=True, exist_ok=True)
    with (settings.data / 'pages.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'state': 'busy'}
        state = _receipt(settings)
        version = local_version(settings)
        if mode == 'pages_branch':
            current = settings.web / 'current'
            release = current.resolve().name
            if not version or status(settings)['state'] == 'live':
                return status(settings)
            response = httpx.get(settings.base_url + '/site-deployment.json', params={'id': release},
                                 headers={'Cache-Control': 'no-cache'}, timeout=10)
            if response.status_code == 200 and response.json() == {'version': version, 'request_id': release}:
                if current.resolve().name == release and local_version(settings) == version:
                    atomic_json(settings.data / 'pages-state.json', {
                        'target': state['target'], 'confirmed': version, 'release': release})
            return status(settings)
        if not version or (version == state.get('confirmed') and not state.get('requested')):
            return status(settings)
        path = settings.data / 'pages-state.json'
        config = settings.values['publication']
        with httpx.Client(timeout=10) as client:
            now = time.time()
            if state.get('requested') == version and state.get('request_id'):
                # A -> B -> A must wait for the new A deployment to pass the queued B.
                request_id = state['request_id']
                try:
                    check = client.get(settings.base_url + '/site-deployment.json', params={'id': request_id},
                                       headers={'Cache-Control': 'no-cache'})
                    visible = check.status_code == 200 and check.json() == {'version': version, 'request_id': request_id}
                except (httpx.RequestError, ValueError):
                    visible = False
                if local_version(settings) != version:
                    return {'state': 'superseded'}
                if visible:
                    atomic_json(path, {'target': state['target'], 'confirmed': version})
                    return status(settings)
                if now - state.get('requested_at', 0) < 900:
                    return status(settings)
            else:
                request_id = uuid.uuid4().hex
            token = os.environ.get('NEWSPAPER_PAGES_TOKEN')
            if not token:
                raise ValueError('Missing NEWSPAPER_PAGES_TOKEN')
            api = f"https://api.github.com/repos/{config['repository']}/actions/workflows/publish-pages.yml/dispatches"
            # Persist the intent before network I/O to recover an ambiguous dispatch.
            state.update(requested=version, requested_at=now, request_id=request_id)
            atomic_json(path, state)
            response = client.post(api, headers={'Authorization': 'Bearer ' + token,
                'Accept': 'application/vnd.github+json'}, json={
                'ref': config.get('ref', 'main'), 'inputs': {'version': version, 'request_id': request_id}})
            response.raise_for_status()
        return status(settings)
