"""Build the M1.4 Phase 2B-1 notification ZIP (offline; uploads nothing).

Deterministic: the same commit always gives the same bytes and CodeSha256. Contains handler.py,
the notify2 package and, under notify2/data/, the search-link configuration and airport catalog
(validated here, so an unsafe search-platforms.json fails the build: spec L12); boto3/botocore come from the Lambda runtime.

Usage: python3 aws/scripts/package_notification_v2.py [out_dir]   (default: aws/build)
Prints JSON: {"zip": path, "sha256": hex, "code_sha256": base64, "bytes": n, "files": [...]}
"""
import base64
import hashlib
import json
import sys
import zipfile
from pathlib import Path

AWS = Path(__file__).resolve().parents[1]
SRC = AWS / "lambdas" / "flight-fare-notification-v2"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def entries():
    files = {"handler.py": (SRC / "handler.py").read_bytes()}
    for p in sorted((SRC / "notify2").glob("*.py")):
        files[f"notify2/{p.name}"] = p.read_bytes()
    for name in ("search-platforms.json", "airports.json"):
        files[f"notify2/data/{name}"] = (AWS / "config" / name).read_bytes()
    return dict(sorted(files.items()))


def validate():
    sys.path.insert(0, str(SRC))
    from notify2 import searchlinks          # noqa: E402 - the Lambda's own validator
    searchlinks.validate_config(json.loads((AWS / "config" / "search-platforms.json").read_text(encoding="utf-8")))
    searchlinks.load_catalog(AWS / "config" / "airports.json")


def build(out_dir):
    validate()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "flight-fare-notification-v2.zip"
    files = entries()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    data = path.read_bytes()
    return {"zip": str(path), "sha256": hashlib.sha256(data).hexdigest(),
            "code_sha256": base64.b64encode(hashlib.sha256(data).digest()).decode(), "bytes": len(data),
            "files": list(files)}


if __name__ == "__main__":
    print(json.dumps(build(Path(sys.argv[1]) if len(sys.argv) > 1 else AWS / "build"), indent=2))
