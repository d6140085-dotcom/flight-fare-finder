"""Offline DRY_RUN of the M1.4 Phase 2A fare check: the wrapper's in-process dry-run path (the
same code the deployed dry run uses), with fixture subscriptions and recorded Travelpayouts
responses. No AWS, no network, no Lambda, no SQS, no email.

Usage:
  python3 aws/tools/farecheck_dry_run.py [--api v1|v3] [--now 2026-11-06T13:00:00Z]
      [--subscriptions aws/tests/fixtures/travelpayouts/subscriptions.json]
      [--responses aws/tests/fixtures/travelpayouts]

Response fixtures are looked up as <api>_<origin>_<destination>_<month>_<currency>.json
(lower-case); a missing file behaves like an unreachable API (fare_check kind "error").
Output: one JSON document with the work items and the dry-run report (no addresses: the dry
run reads subscriptions without email).
"""
import argparse
import json
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

AWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AWS / "lambdas" / "flight-fare-check"))

from farecheck import config, effects, runner, travelpayouts  # noqa: E402


class FixtureTransport:
    def __init__(self, directory, api):
        self.directory, self.api, self.calls = Path(directory), api, []

    def __call__(self, url, headers, timeout):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        month = q.get("depart_date") or q.get("departure_at")
        name = f"{self.api}_{q['origin']}_{q['destination']}_{month}_{q['currency']}.json".lower()
        self.calls.append(name)
        path = self.directory / name
        if not path.exists():
            raise OSError(f"no fixture {name}")
        return json.loads(path.read_text(encoding="utf-8"))


def main(argv=None):
    fx = AWS / "tests" / "fixtures" / "travelpayouts"
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--api", choices=("v1", "v3"), default="v1")
    ap.add_argument("--now", default="2026-11-06T13:00:00Z")
    ap.add_argument("--subscriptions", default=str(fx / "subscriptions.json"))
    ap.add_argument("--responses", default=str(fx))
    a = ap.parse_args(argv)
    now = datetime.fromisoformat(a.now.replace("Z", "+00:00"))
    settings = config.Settings.from_env({"FARECHECK_MODE": "dry_run", "FARE_API": a.api})
    catalog = config.load_catalog()
    rows = json.loads(Path(a.subscriptions).read_text(encoding="utf-8"))
    transport, logs = FixtureTransport(a.responses, a.api), []
    client = travelpayouts.Client("offline-token", catalog, now, api=a.api, transport=transport, log=logs.append)
    evaluator = runner.DryRunEvaluator(settings=settings, catalog=catalog, client=client, as_of=now, log=logs.append)
    plan = runner.run_wrapper(settings=settings, catalog=catalog, reader=effects.ListReader(rows), now=now,
                              dry_evaluator=evaluator, log=logs.append)
    print(json.dumps({"dry_run": True, "api": a.api, "now": a.now, "result": plan,
                      "api_requests": transport.calls, "log": logs}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
