"""Publish generated files to an existing GitHub Pages branch checkout."""
import argparse
import fcntl
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path


DIRECTORIES = {'assets', 'archive', 'daily', 'downloads'}
ROOT_FILES = {'index.html', 'robots.txt', 'search.json', 'feed.xml'}
METADATA = {'site-manifest.json', 'site-version.txt', 'site-deployment.json'}


def generated(name):
    root, slash, _ = name.partition('/')
    return (root in DIRECTORIES if slash else root in ROOT_FILES | METADATA
            or bool(re.fullmatch(r'feed-[a-z0-9-]+\.xml', root)))


def sync(repo, web, branch, day=None):
    def git(*arguments):
        return subprocess.check_output(['git', *arguments], text=True, timeout=120).strip()

    if Path(git('rev-parse', '--show-toplevel')) != repo or git('branch', '--show-current') != branch:
        raise ValueError('Run in the root of the checked-out Pages branch')
    current = web / 'current'
    source = current.resolve(strict=True)
    if source.parent != (web / 'releases').resolve() or not re.fullmatch('[a-f0-9]{32}', source.name):
        raise ValueError('Expected an atomic Newspaper release')
    marker = Path(git('rev-parse', '--absolute-git-dir')) / 'newspaper-pushed-release'
    if marker.exists() and marker.read_text() == source.name + ' ' + git('rev-parse', 'HEAD'):
        return

    remote_ref = 'refs/remotes/origin/' + branch
    day_stamp = f'Newspaper-Date: {day}' if day else None

    def already_pushed():
        return day_stamp and git('for-each-ref', '--format=%(refname)', remote_ref) and git(
            'log', '-1', '--format=%H', '--grep=^' + day_stamp + '$', remote_ref)

    if already_pushed():
        return
    git('fetch', 'origin', branch)
    if already_pushed():
        return
    git('merge', '--ff-only', remote_ref)
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
        subject = ['-m', 'Publish Newspaper site ' + version[:12]]
        if day_stamp:
            subject += ['-m', day_stamp]
        git('commit', '--only', *subject, '--', *names)
    git('push', 'origin', 'HEAD:refs/heads/' + branch)
    marker.write_text(source.name + ' ' + git('rev-parse', 'HEAD'))
    print('Published', version, 'from', source.name)


def daily_sync(repo, branch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from newspaper.config import Settings
    from newspaper.retention import daily_cleanup_done
    from newspaper.schedule import jobs
    from newspaper.storage import Store

    settings = Settings.load()
    publication = settings.values.get('publication', {})
    if publication.get('mode') != 'pages_branch' or publication.get('ref', 'main') != branch:
        raise ValueError('Daily sync requires the configured Pages branch')
    if not settings.sections:
        return
    try:
        pipeline_lock = (settings.data / 'pipeline.lock').open('r')
    except FileNotFoundError:
        return
    with pipeline_lock:
        try:
            fcntl.flock(pipeline_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        today = datetime.now(settings.timezone).date()
        if not daily_cleanup_done(settings, today):
            return
        day = today.isoformat()
        end_of_day = datetime.combine(today, time.max, settings.timezone)
        scheduled = [(job, window) for job, window in jobs(settings, end_of_day) if window['date'] == day]
        if not scheduled or not (settings.data / 'newspaper.sqlite').is_file():
            return
        store = Store(settings, read_only=True)
        try:
            keys = [store.existing_publication(job, window) for job, window in scheduled]
            if not all(key and store.published(key) for key in keys):
                return
            if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='pending_translations'").fetchone():
                pending = {row[0] for row in store.db.execute('SELECT day FROM pending_translations')}
                if pending.intersection(keys):
                    return
        finally:
            store.close()
        sync(repo, settings.web, branch, day)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    web = parser.add_mutually_exclusive_group()
    web.add_argument('--web', type=Path)
    web.add_argument('--daily', action='store_true', help='Push once after all scheduled editions and translations are ready')
    parser.add_argument('--branch', default='main')
    args = parser.parse_args()
    if args.daily:
        daily_sync(Path.cwd(), args.branch)
    else:
        sync(Path.cwd(), args.web or Path('/srv/newspaper'), args.branch)


if __name__ == '__main__':
    main()
