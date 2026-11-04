"""Export a reproducible local release inventory. No device or service is opened."""
import argparse
import ast
import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import re
import sys

PROJECT=Path(__file__).resolve().parents[1]

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    paths=[]
    for folder in ('src/locator_demo','calibration'):
        paths.extend(p for p in (PROJECT/folder).rglob('*') if p.is_file()
                     and '__pycache__' not in p.parts and p.suffix in ('.py','.js','.css','.html','.json'))
    for name in ('models/silero_vad.onnx','models/scrfd.onnx','requirements.txt','locator_demo_settings.json','README.md',
                 'docs/CURRENT_RELEASE_CN.md','docs/CORE_YAW_GEOMETRY_CN.md'):
        p=PROJECT/name
        if p.exists():paths.append(p)
    source=ast.parse((PROJECT/'src/locator_demo/web/app.py').read_text(encoding='utf-8'))
    revisions=[v.value for node in ast.walk(source) if isinstance(node,ast.Dict)
               for k,v in zip(node.keys,node.values) if isinstance(k,ast.Constant)
               and k.value=='algorithm_revision' and isinstance(v,ast.Constant)]
    packages={}
    for line in (PROJECT/'requirements.txt').read_text(encoding='utf-8').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        match=re.match(r'([A-Za-z0-9_.-]+)',line)
        if not match:continue
        name=match.group(1)
        try:version=metadata.version(name)
        except metadata.PackageNotFoundError:version=None
        packages[name]={'declared':line,'installed_version':version}
    manifest={'source_revision':revisions[0] if revisions else None,
        'python_version':platform.python_version(),'platform':sys.platform,
        'files':{p.relative_to(PROJECT).as_posix():digest(p) for p in sorted(set(paths))},
        'declared_dependency_versions':packages,
        'runtime_revision':'not_queried_by_this_tool',
        'clean_install_verified':False,'hardware_io':False,
        'scope':'Inventory of the current local source, models, calibration and settings; not a tested lockfile or runtime load assertion.'}
    (args.output/'release-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    settings=PROJECT/'locator_demo_settings.json'
    if settings.exists():
        config=json.loads(settings.read_text(encoding='utf-8-sig'))
        (args.output/'settings-snapshot.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'revision':manifest['source_revision'],'files':len(manifest['files']),
        'dependency_packages':len(packages),'missing_packages':[n for n,v in packages.items() if v['installed_version'] is None],
        'clean_install_verified':False,'hardware_io':False}))

if __name__=='__main__':main()
