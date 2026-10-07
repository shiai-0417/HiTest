#!/usr/bin/env python3
"""Deploy this working tree using the local platform's registered SSH credentials.

No driver test is executed. Files are staged, verified, then current is switched.
"""
import argparse
from datetime import datetime, timezone
import getpass
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import uuid

SOURCE = Path(__file__).resolve().parents[1]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def package(source, output):
    source = source.resolve()
    payload = {}
    for name in ('hitest', 'README.md', '.gitignore'):
        path = source / name
        if path.is_symlink() or not path.is_file(): raise ValueError('Invalid source: ' + name)
        payload[name] = path.read_bytes()
    for folder, suffix in (('scripts', '.sh'), ('tests', '.py'), ('deploy', '.py')):
        root = source / folder
        if root.is_symlink(): raise ValueError('Source directory is a symlink: ' + folder)
        for path in sorted(root.rglob('*')):
            if path.suffix != suffix: continue
            if path.is_symlink() or not path.is_file(): raise ValueError('Invalid source file: ' + str(path))
            payload[path.relative_to(source).as_posix()] = path.read_bytes()
    for required in ('scripts/load-unload.sh', 'scripts/nvidia-load-unload.sh'):
        if required not in payload: raise ValueError('Missing test script: ' + required)
    commit = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    dirty = subprocess.run(['git', '-C', str(source), 'status', '--porcelain'], capture_output=True, text=True)
    manifest = {'tool': 'HiTest', 'git_commit': commit.stdout.strip() if commit.returncode == 0 else '',
                'working_tree_dirty': bool(dirty.stdout.strip()),
                'files': {name: sha256(data) for name, data in sorted(payload.items())}}
    manifest['release_id'] = sha256(json.dumps(manifest, sort_keys=True).encode())[:24]
    payload['deployment.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / ('HiTest-' + manifest['release_id'] + '.tar.gz')
    with archive.open('wb') as handle, gzip.GzipFile(fileobj=handle, mode='wb', filename='', mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode='w|') as tar:
            for name, content in sorted(payload.items()):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o755 if name == 'hitest' or name.endswith('.sh') else 0o644
                tar.addfile(info, io.BytesIO(content))
    return archive, sha256(archive.read_bytes()), manifest


# Standard-library extraction supports the target's existing Python 3.10+.
# Reject links and path escapes instead of trusting tar.extractall defaults.
INSTALL = r'''
import hashlib,json,os,pathlib,shutil,subprocess,sys,tarfile,tempfile,uuid
archive=pathlib.Path(sys.argv[1]); base=pathlib.Path(sys.argv[2]); expected=sys.argv[3]
assert base.is_absolute() and '..' not in base.parts and base.name=='HiTest', 'Invalid target path'
assert base.resolve()==base, 'Deployment path contains a symlink'
assert hashlib.sha256(archive.read_bytes()).hexdigest()==expected, 'Archive checksum mismatch'
base.mkdir(parents=True,exist_ok=True)
releases=base/'releases'; releases.mkdir(exist_ok=True)
assert not releases.is_symlink(), 'Releases directory is a symlink'
with tarfile.open(archive,'r:gz') as tar:
    members=tar.getmembers()
    assert len(members)<=1000 and sum(m.size for m in members)<=20*1024*1024, 'Archive too large'
    names=[m.name for m in members]
    assert len(set(names))==len(names), 'Duplicate archive members'
    for m in members:
        path=pathlib.PurePosixPath(m.name)
        assert m.isfile() and not path.is_absolute() and '..' not in path.parts, 'Unsafe archive member'
    mf=tar.extractfile('deployment.json'); assert mf is not None, 'Missing manifest'
    manifest=json.loads(mf.read()); mf.close()
    files=manifest['files']; release_id=manifest['release_id']
    assert len(release_id)==24 and all(c in '0123456789abcdef' for c in release_id), 'Invalid release ID'
    assert set(names)==set(files)|{'deployment.json'}, 'Archive manifest mismatch'
    calculated=dict(manifest); del calculated['release_id']
    assert hashlib.sha256(json.dumps(calculated,sort_keys=True).encode()).hexdigest()[:24]==release_id, 'Manifest checksum mismatch'
    stage=pathlib.Path(tempfile.mkdtemp(prefix='.stage-',dir=releases))
    try:
        for m in members:
            content=tar.extractfile(m).read()
            if m.name!='deployment.json':
                assert hashlib.sha256(content).hexdigest()==files[m.name], 'File checksum mismatch: '+m.name
            destination=stage/m.name; destination.parent.mkdir(parents=True,exist_ok=True)
            with destination.open('xb') as handle: handle.write(content)
            destination.chmod(0o755 if m.name=='hitest' or m.name.endswith('.sh') else 0o644)
        scripts=[stage/'hitest',*sorted((stage/'scripts').glob('*.sh'))]
        for script in scripts:
            subprocess.run(['bash','-n',str(script)],check=True,timeout=15)
        listed=subprocess.run(['bash',str(stage/'hitest'),'list'],capture_output=True,text=True,check=True,timeout=15)
        assert {'load-unload','nvidia-load-unload'}<=set(listed.stdout.splitlines()), 'Missing commands'
        logs=base/'logs'; logs.mkdir(exist_ok=True)
        assert not logs.is_symlink(), 'Log directory is a symlink'
        (stage/'logs').symlink_to('../../logs',target_is_directory=True)
        final=releases/release_id
        assert not final.is_symlink(), 'Release path is a symlink'
        if final.exists():
            assert not final.is_symlink() and (final/'deployment.json').read_bytes()==(stage/'deployment.json').read_bytes(), 'Existing release differs'
            for name,digest in files.items():
                path=final/name
                assert not path.is_symlink() and path.resolve().is_relative_to(final) and hashlib.sha256(path.read_bytes()).hexdigest()==digest, 'Existing release was modified'
            assert (final/'logs').is_symlink() and (final/'logs').resolve()==logs, 'Existing log link differs'
            shutil.rmtree(stage)
        else: stage.rename(final)
        current=base/'current'
        assert not current.exists() or current.is_symlink(), 'current must be a symlink'
        link=base/('.current-'+uuid.uuid4().hex)
        link.symlink_to('releases/'+release_id,target_is_directory=True)
        try: os.replace(link,current)
        finally:
            if link.is_symlink(): link.unlink()
        print(json.dumps({'state':'deployed','release_id':release_id,'entrypoint':str(current/'hitest'),'commands':listed.stdout.splitlines()}))
    finally:
        if stage.exists(): shutil.rmtree(stage)
'''


def run_checked(transport, command, timeout=30):
    result = transport.run(command, timeout=timeout)
    if result.code != 0: raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'Remote command failed')
    return result.stdout


def deploy(app, environment, actor, archive, digest, manifest, transport_factory):
    if environment['username'] != 'root': raise ValueError('The target must use root login')
    remote_base = environment['remote_root'].rstrip('/') + '/tools/HiTest'
    remote_archive = environment['remote_root'].rstrip('/') + '/.hitest-uploads/' + uuid.uuid4().hex + '.tar.gz'
    lease = app.reservations.create({'environment_id': environment['id'], 'minutes': 15, 'reason': 'HiTest source deployment'}, actor)
    transport = None
    upload_started = False
    try:
        transport = transport_factory(environment, app.credentials)
        probe = json.loads(run_checked(transport, ['python3', '-c', 'import json,os,sys; print(json.dumps({"uid":os.geteuid(),"python_ok":sys.version_info >= (3,10)}))']))
        if probe.get('uid') != 0 or probe.get('python_ok') is not True: raise RuntimeError('Target requires root and Python 3.10+')
        run_checked(transport, ['python3', '-c', 'import pathlib,sys; p=pathlib.Path(sys.argv[1]); assert p.is_absolute() and ".." not in p.parts; p.parent.mkdir(parents=True,exist_ok=True)', remote_archive])
        upload_started = True
        transport.upload(archive, remote_archive)
        result = json.loads(run_checked(transport, ['python3', '-c', INSTALL, remote_archive, remote_base, digest], timeout=60))
        if result.get('state') != 'deployed' or result.get('release_id') != manifest['release_id']: raise RuntimeError('Remote deployment receipt does not match')
        with app.db.transaction(write=True) as conn:
            app.accounts.audit(conn, actor, 'tool.hitest.deploy', environment['id'], {'release_id': manifest['release_id'], 'archive_sha256': digest, 'entrypoint': result['entrypoint']})
        return result
    finally:
        try:
            if transport is not None and upload_started:
                run_checked(transport, ['python3', '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).unlink(missing_ok=True)', remote_archive])
        finally:
            app.reservations.release(lease['id'], actor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='192.168.0.107')
    parser.add_argument('--platform-dir', type=Path, default=SOURCE.parents[1] / 'hygon-dcu-test-platform')
    parser.add_argument('--actor', default=getpass.getuser(), help='Platform administrator account used for audit records')
    parser.add_argument('--package-only', action='store_true', help='Prepare the bundle without a remote connection')
    args = parser.parse_args()
    output = SOURCE / 'logs' / 'deploy' / (datetime.now().strftime('%Y-%m-%d_%H-%M-%S') + '-' + uuid.uuid4().hex[:8])
    archive, digest, manifest = package(SOURCE, output)
    record = {'hostname': args.host, 'package': str(archive), 'archive_sha256': digest, 'release_id': manifest['release_id'], 'state': 'packaged', 'started_at': datetime.now(timezone.utc).isoformat()}
    print('Package:', archive)
    app = None
    code = 0
    try:
        if not args.package_only:
            platform = args.platform_dir.resolve()
            sys.path.insert(0, str(platform / 'backend/src'))
            from dcu_platform.application import Application
            from dcu_platform.modules.execution.transport import SSHTransport
            os.chdir(platform)  # Settings use the platform's existing data/credentials.
            app = Application()
            with app.db.transaction() as conn:
                user = conn.one('SELECT * FROM users WHERE username=?', (args.actor,))
                if not user: raise RuntimeError('Platform administrator account not found')
                actor = app.accounts.public(user, conn)
            app.login_admin.authorize(actor)
            if not actor['active']: raise RuntimeError('Platform account disabled')
            environments = [env for env in app.environments.list() if env['hostname'] == args.host]
            if len(environments) != 1: raise RuntimeError('Expected exactly one registered target machine')
            record.update(deploy(app, environments[0], actor, archive, digest, manifest, SSHTransport))
    except Exception as error:
        code = 1
        record.update(state='failed', error=app.credentials.redact(str(error)) if app else str(error))
    finally:
        record['ended_at'] = datetime.now(timezone.utc).isoformat()
        (output / 'deployment-result.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__': sys.exit(main())
