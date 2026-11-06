"""Build the B4 runner ZIP (offline). Contents: index.py (runner), m14_scenarios.py, the
production subsapi package and copies of its two config files — the code under test is exactly
what flight-subscriptions-api ships. boto3 comes from the Lambda runtime.

Usage: python3 aws/integration/package_runner.py [out_dir]
Prints JSON with the zip path, sha256 and CodeSha256 (base64) and writes <zip>.hex for transfer.
"""
import base64
import hashlib
import json
import sys
import zipfile
from pathlib import Path

AWS = Path(__file__).resolve().parents[1]
API = AWS / "lambdas" / "flight-subscriptions-api"
FIXED = (1980, 1, 1, 0, 0, 0)


def entries():
    files = {"index.py": (AWS / "integration" / "runner" / "index.py").read_bytes(),
             "m14_scenarios.py": (AWS / "integration" / "m14_scenarios.py").read_bytes()}
    for p in sorted((API / "subsapi").glob("*.py")):
        files[f"subsapi/{p.name}"] = p.read_bytes()
    for name in ("airports.json", "subscription-policy.json"):
        files[f"subsapi/config/{name}"] = (AWS / "config" / name).read_bytes()
    return dict(sorted(files.items()))


def build(out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "flight-m14-it-runner.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries().items():
            info = zipfile.ZipInfo(name, FIXED)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    data = path.read_bytes()
    (out_dir / "flight-m14-it-runner.zip.hex").write_text(data.hex())
    return {"zip": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "code_sha256": base64.b64encode(hashlib.sha256(data).digest()).decode()}


if __name__ == "__main__":
    print(json.dumps(build(Path(sys.argv[1]) if len(sys.argv) > 1 else AWS / "build"), indent=2))
