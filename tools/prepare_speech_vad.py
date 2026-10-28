"""Verify or acquire the pinned, MIT-licensed Silero VAD. Runtime never downloads."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
COMMIT = '066409f3d9db45a30d21dfcbf9147826ffdd7052'
BASE = f'https://raw.githubusercontent.com/snakers4/silero-vad/{COMMIT}/'
SHA256 = '1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true', help='Explicitly download from the official pinned source')
    args = parser.parse_args()
    model = ROOT / 'models/silero_vad.onnx'
    url = BASE + 'src/silero_vad/data/silero_vad.onnx'
    if args.download:
        data = urllib.request.urlopen(url, timeout=60).read()
        if hashlib.sha256(data).hexdigest() != SHA256:
            raise RuntimeError('Model digest mismatch; existing model was not changed')
        license_data = urllib.request.urlopen(BASE + 'LICENSE', timeout=30).read()
        model.parent.mkdir(parents=True, exist_ok=True)
        temporary = model.with_suffix('.download')
        temporary.write_bytes(data)
        temporary.replace(model)
        (model.parent / 'silero_vad.LICENSE.txt').write_bytes(license_data)
    if not model.is_file() or hashlib.sha256(model.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError('Pinned model missing or changed; run with --download to acquire it')
    result = dict(repository='https://github.com/snakers4/silero-vad', commit=COMMIT,
                  source_url=url, sha256=SHA256, bytes=model.stat().st_size,
                  license='MIT', runtime_downloads=False, trained_here=False)
    (model.parent / 'silero_vad.source.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
