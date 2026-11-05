"""Check aws/config/airports.json against OpenTravelData (offline, no AWS or fare API calls).

Download the reference file once (public, ~13 MB):
  https://raw.githubusercontent.com/opentraveldata/opentraveldata/master/opentraveldata/optd_por_public.csv
Run:
  python3 aws/tools/verify_airports.py path/to/optd_por_public.csv

For every origin and destination it checks that the code is a current IATA location in the stated
country; that type=airport codes are airports; and that type=city codes are city codes whose
listed airports all belong to that city. Exit status 1 if anything fails.
"""
import collections
import csv
import json
import sys
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "config" / "airports.json"


def load(path):
    csv.field_size_limit(10**8)
    rows = collections.defaultdict(list)
    with open(path, encoding="utf-8") as f:
        for x in csv.DictReader(f, delimiter="^"):
            if not x["date_until"] and not x["envelope_id"]:  # current records only
                rows[x["iata_code"]].append(x)
    return rows


def check(entry, rows):
    code, cc = entry["code"], entry["country"]
    xs = rows.get(code, [])
    if not xs:
        return "unknown IATA code"
    if cc not in {x["country_code"] for x in xs}:
        return f"country {cc} doesn't match {sorted({x['country_code'] for x in xs})}"
    if entry["type"] == "airport":
        return None if any("A" in x["location_type"] for x in xs) else "not an airport"
    if not any("C" in x["location_type"] for x in xs):
        return "not a city code"
    for ap in entry.get("airports", []):
        cities = {c for x in rows.get(ap, []) if "A" in x["location_type"] for c in x["city_code_list"].split(",")}
        if code not in cities:
            return f"airport {ap} is not in city {code} (it belongs to {sorted(cities)})"
    return None


def main(path):
    rows = load(path)
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    failures = 0
    for entry in cfg["origins"] + cfg["destinations"]:
        problem = check(entry, rows)
        failures += problem is not None
        print(f"{'FAIL' if problem else 'ok  '} {entry['code']} {entry['type']:7} {entry['name_en']}"
              + (f": {problem}" if problem else ""))
    print(f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
