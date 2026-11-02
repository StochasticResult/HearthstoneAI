"""Public executable build identity, frozen once for each match."""
import json
import re
from pathlib import Path

UNVERSIONED = 'unversioned'


def current_build(root=None):
    return read_build(Path(root or Path(__file__).parent)/'build-info.json')


def read_build(path):
    path=Path(path)
    if not path.exists():
        return {'version_id':UNVERSIONED,'identity_verified':False}
    if path.is_symlink() or path.stat().st_size>4096:
        raise ValueError('Invalid runtime build identity')
    value=json.loads(path.read_text())
    if value=={'version_id':UNVERSIONED,'identity_verified':False}:
        return value
    if (not isinstance(value,dict) or not re.fullmatch(r'[A-Za-z0-9._-]{1,80}',str(value.get('version_id','')))
            or not re.fullmatch(r'[0-9a-f]{64}',str(value.get('content_sha256','')))):
        raise ValueError('Invalid runtime build identity')
    return {k:value[k] for k in ('version_id','content_sha256','git_revision','native_version') if k in value} | {'identity_verified':True}
