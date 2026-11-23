"""Fetch one verified public site from the configured HTTPS origin on a Pages runner."""
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener


class SameOrigin(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if urlsplit(newurl)[:2] != urlsplit(request.full_url)[:2]:
            raise ValueError('Unexpected redirect away from the configured origin')
        return super().redirect_request(request, fp, code, message, headers, newurl)


def main():
    origin = os.environ['NEWSPAPER_ORIGIN'].rstrip('/')
    target = urlsplit(origin)
    if (target.scheme != 'https' or not target.hostname or target.username or target.password
            or target.query or target.fragment or target.path):
        raise ValueError('NEWSPAPER_ORIGIN must be an HTTPS site origin')
    version = os.environ['NEWSPAPER_VERSION']
    if not re.fullmatch('[0-9a-f]{64}', version):
        raise ValueError('Invalid release version')
    request_id = os.environ['NEWSPAPER_REQUEST_ID']
    if not re.fullmatch('[0-9a-f]{32}', request_id):
        raise ValueError('Invalid publication request')

    def fetch(path):
        return build_opener(SameOrigin()).open(f'{origin}/{path}?v={version}', timeout=30)

    with fetch('site-manifest.json') as response:
        manifest = response.read()
    if hashlib.sha256(manifest).hexdigest() != version:
        raise ValueError('A newer release is already active on the origin')
    files = json.loads(manifest)
    if 'index.html' not in files:
        raise ValueError('The release is missing its homepage')
    output = Path('_site')
    output.mkdir()

    def copy_file(item):
        name, digest = item
        parts = name.split('/')
        allowed = (name in {'index.html', 'search.json', 'robots.txt', 'feed.xml'}
                   or re.fullmatch(r'feed-[a-z0-9-]+\.xml', name)
                   or parts[0] in {'assets', 'archive', 'daily', 'downloads'} and len(parts) > 1)
        if not allowed or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', p) for p in parts):
            raise ValueError('Unexpected file in release manifest')
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        url_path = name.removesuffix('index.html') if parts[-1] == 'index.html' else name
        checksum = hashlib.sha256()
        with fetch(url_path) as source, path.open('xb') as dest:
            while chunk := source.read(65536):
                dest.write(chunk)
                checksum.update(chunk)
        if checksum.hexdigest() != digest:
            raise ValueError(f'Origin changed while downloading {name}')

    with ThreadPoolExecutor(max_workers=4) as pool:
        for _ in pool.map(copy_file, files.items()):
            pass
    with fetch('site-version.txt') as response:
        if response.read().strip() != version.encode():
            raise ValueError('Release superseded before upload')
    (output / 'site-version.txt').write_text(version + '\n')
    (output / 'site-manifest.json').write_bytes(manifest)
    (output / 'site-deployment.json').write_text(json.dumps({'version': version, 'request_id': request_id}))


if __name__ == '__main__':
    main()
