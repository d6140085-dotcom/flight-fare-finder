"""Build the flight-subscriptions-api deployment ZIP (offline; uploads nothing).

The ZIP is deterministic (sorted entries, fixed timestamps and permissions), so the same commit
always produces the same bytes and the same Lambda CodeSha256, which the deploy step verifies.
It contains index.py, the subsapi package and copies of aws/config/airports.json and
subscription-policy.json (the API validates against the same catalog the front end ships).
boto3/botocore are provided by the Lambda Python runtime and are not bundled.

Usage: python3 aws/scripts/package_subscriptions_api.py [out_dir]   (default: aws/build)
Prints JSON: {"zip": path, "sha256": hex, "code_sha256": base64, "bytes": n}
"""
import base64
import hashlib
import json
import sys
import zipfile
from pathlib import Path

AWS = Path(__file__).resolve().parents[1]
SRC = AWS / "lambdas" / "flight-subscriptions-api"
CONFIG_FILES = ("airports.json", "subscription-policy.json")
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def entries():
    files = {"index.py": (SRC / "index.py").read_bytes()}
    for p in sorted((SRC / "subsapi").glob("*.py")):
        files[f"subsapi/{p.name}"] = p.read_bytes()
    for name in CONFIG_FILES:
        files[f"subsapi/config/{name}"] = (AWS / "config" / name).read_bytes()
    return dict(sorted(files.items()))


def build(out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "flight-subscriptions-api.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries().items():
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    data = path.read_bytes()
    return {"zip": str(path), "sha256": hashlib.sha256(data).hexdigest(),
            "code_sha256": base64.b64encode(hashlib.sha256(data).digest()).decode(), "bytes": len(data)}


if __name__ == "__main__":
    print(json.dumps(build(Path(sys.argv[1]) if len(sys.argv) > 1 else AWS / "build"), indent=2))
