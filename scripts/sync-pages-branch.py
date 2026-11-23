"""Publish generated files to an existing GitHub Pages branch checkout."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


DIRECTORIES = {'assets', 'archive', 'daily', 'downloads'}
ROOT_FILES = {'index.html', 'robots.txt', 'search.json', 'feed.xml'}
METADATA = {'site-manifest.json', 'site-version.txt', 'site-deployment.json'}


def generated(name):
    root, slash, _ = name.partition('/')
    return (root in DIRECTORIES if slash else root in ROOT_FILES | METADATA
            or bool(re.fullmatch(r'feed-[a-z0-9-]+\.xml', root)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--web', type=Path, default=Path('/srv/newspaper'))
    parser.add_argument('--branch', default='main')
    args = parser.parse_args()
    repo = Path.cwd()

    def git(*arguments):
        return subprocess.check_output(['git', *arguments], text=True, timeout=120).strip()

    if Path(git('rev-parse', '--show-toplevel')) != repo or git('branch', '--show-current') != args.branch:
        raise ValueError('Run in the root of the checked-out Pages branch')
    current = args.web / 'current'
    source = current.resolve(strict=True)
    if source.parent != (args.web / 'releases').resolve() or not re.fullmatch('[a-f0-9]{32}', source.name):
        raise ValueError('Expected an atomic Newspaper release')
    marker = Path(git('rev-parse', '--absolute-git-dir')) / 'newspaper-pushed-release'
    if marker.exists() and marker.read_text() == source.name + ' ' + git('rev-parse', 'HEAD'):
        return

    git('fetch', 'origin', args.branch)
    git('merge', '--ff-only', 'refs/remotes/origin/' + args.branch)
    manifest = (source / 'site-manifest.json').read_bytes()
    version = (source / 'site-version.txt').read_text().strip()
    files = json.loads(manifest)
    if hashlib.sha256(manifest).hexdigest() != version or 'index.html' not in files:
        raise ValueError('Invalid public release manifest')
    for name in files:
        if (not generated(name) or name in METADATA
                or any(not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]*', part) for part in name.split('/'))):
            raise ValueError('Unexpected path in public release')
    actual = set()
    for path in source.rglob('*'):
        if path.is_symlink():
            raise ValueError('Public release contains a symlink')
        if path.is_file():
            actual.add(path.relative_to(source).as_posix())
    if actual != set(files) | {'site-version.txt', 'site-manifest.json'}:
        raise ValueError('Public files differ from the release manifest')

    # Keep repository-owned metadata and any unrelated tracked or staged changes.
    managed = {name.split('/')[0] for name in git('ls-files', '-z').split('\0') if generated(name)}
    managed.update(name.split('/')[0] for name in actual)
    managed.add('site-deployment.json')
    if any((repo / name).is_symlink() for name in managed):
        raise ValueError('Unexpected symlink in the Pages checkout')
    includes = ['--include=/' + name + '/***' for name in sorted(DIRECTORIES)]
    includes += ['--include=/' + name for name in sorted(ROOT_FILES | (METADATA - {'site-deployment.json'}))]
    subprocess.run(['rsync', '-rtc', '--delete', *includes, '--include=/feed-*.xml', '--exclude=*',
                    str(source) + '/', str(repo) + '/'], check=True)
    if (repo / 'site-manifest.json').read_bytes() != manifest or (repo / 'site-version.txt').read_text().strip() != version:
        raise ValueError('Mirrored release metadata changed during sync')
    for name, expected in files.items():
        if hashlib.sha256((repo / name).read_bytes()).hexdigest() != expected:
            raise ValueError('Mirrored file differs from the release manifest')
    if current.resolve() != source:
        raise RuntimeError('A newer release is ready')
    (repo / 'site-deployment.json').write_text(json.dumps({'version': version, 'request_id': source.name}) + '\n')
    names = sorted(managed)
    git('add', '-A', '--', *names)
    if git('diff', '--cached', '--name-only', '--', *names):
        git('commit', '--only', '-m', 'Publish Newspaper site ' + version[:12], '--', *names)
    git('push', 'origin', 'HEAD:refs/heads/' + args.branch)
    marker.write_text(source.name + ' ' + git('rev-parse', 'HEAD'))
    print('Published', version, 'from', source.name)


if __name__ == '__main__':
    main()
