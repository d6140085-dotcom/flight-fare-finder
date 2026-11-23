"""B6-1: the local manual-verification checklist generator (aws/tools/b6_link_checklist.py).

Offline: the generator must never touch the network, never print a marker (real or test), never
read TP_MARKER/TP_TOKEN, mark verified vs unverified codes correctly and never claim attribution.
Run: python3 -m unittest aws/tests/test_b6_link_checklist.py
"""
import io
import os
import re
import socket
import sys
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
import b6_link_checklist as tool  # noqa: E402

TODAY = date(2026, 11, 23)


def generate(**kw):
    args = dict(origins=["TPE", "KHH", "RMQ"], destinations=["TYO", "OKA", "LON"], month_offsets=[1, 6, 12])
    args.update(kw)
    return tool.build_checklist(TODAY, **args)


def no_network(*a, **k):
    raise AssertionError("the checklist generator must not use the network")


class ChecklistTests(unittest.TestCase):
    def test_no_network_and_no_secrets_read(self):
        env = {"TP_MARKER": "987654321", "TP_TOKEN": "tok-should-never-appear"}
        with mock.patch.object(socket, "socket", no_network), \
                mock.patch.object(socket, "create_connection", no_network), \
                mock.patch.object(urllib.request, "urlopen", no_network), \
                mock.patch.dict(os.environ, env):
            text = generate()
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(tool.main(["--today", "2026-11-23", "--months", "1"]), 0)
        for out in (text, buf.getvalue()):
            self.assertNotIn("987654321", out)
            self.assertNotIn("tok-should-never-appear", out)
            self.assertNotIn("marker=", out)         # not even the 000000 test value
            self.assertNotIn("000000", out)

    def test_attribution_is_never_called_verified(self):
        text = generate()
        self.assertIn("Affiliate attribution: NOT VERIFIED", text)
        for line in text.splitlines():
            if "attribution" in line.lower() and "VERIFIED" in line.upper():
                self.assertIn("NOT VERIFIED", line.upper(), line)   # never "attribution ... verified"
            if "affiliate" in line.lower() or "marker" in line.lower():
                self.assertNotRegex(line, r"(?<!NOT )VERIFIED(?! codes)", line)

    def test_verified_and_unverified_routes_are_distinguished(self):
        text = generate(origins=["TPE", "KHH"], destinations=["TYO", "LON"], month_offsets=[1])
        blocks = {b.split("\n", 1)[0]: b for b in text.split("### ")[1:]}
        tpe_tyo, khh_tyo, tpe_lon = blocks["TPE → TYO (2026-12)"], blocks["KHH → TYO (2026-12)"], blocks["TPE → LON (2026-12)"]
        self.assertIn("**skyscanner** — itinerary — VERIFIED codes (2026-11-06, browser)", tpe_tyo)
        self.assertIn("**skyscanner** — fallback — UNVERIFIED", khh_tyo)        # KHH not verified on Skyscanner
        self.assertIn("**tripcom** — itinerary — VERIFIED codes", khh_tyo)
        self.assertIn("**skyscanner** — fallback — UNVERIFIED", tpe_lon)
        self.assertIn("**tripcom** — fallback — UNVERIFIED", tpe_lon)
        self.assertIn("Candidate itinerary URL (UNVERIFIED, not used by the app): "
                      "https://www.skyscanner.com.tw/transport/flights/tpe/lon/261220/261227/", tpe_lon)
        self.assertNotIn("Candidate", tpe_tyo)
        self.assertIn("GENERAL PAGE BY DESIGN", tpe_tyo)

    def test_months_and_aviasales_year_cut_off(self):
        text = generate(origins=["TPE"], destinations=["TYO"])
        self.assertIn("## Departure month 2026-12 (offset +1): sample fare 2026-12-20 → 2026-12-27", text)
        self.assertIn("## Departure month 2027-05 (offset +6)", text)
        self.assertIn("## Departure month 2027-11 (offset +12): sample fare 2027-11-20 → 2027-11-27", text)
        last = text.split("## Departure month 2027-11")[1]
        self.assertIn("**aviasales** — fallback", last)          # return >= today + 12 months: no year in link
        self.assertIn("App link (marker removed): https://www.aviasales.com/", last)

    def test_only_allowed_hosts(self):
        hosts = set(re.findall(r"https://([^/\s?]+)", generate()))
        self.assertEqual(hosts, {"www.skyscanner.com.tw", "tw.trip.com", "www.aviasales.com", "www.google.com"})

    def test_unsupported_code_is_reported_not_linked(self):
        text = generate(origins=["TSA"], destinations=["TYO"], month_offsets=[1])
        self.assertIn("No links: unsupported_code", text)
        self.assertNotIn("https://", text.split("### TSA")[1])

    def test_strip_marker(self):
        self.assertEqual(tool.strip_marker("https://www.aviasales.com/?marker=000000"), "https://www.aviasales.com/")
        self.assertEqual(tool.strip_marker("https://www.aviasales.com/search/TPE1112TYO18121?marker=1&x=2"),
                         "https://www.aviasales.com/search/TPE1112TYO18121?x=2")

    def test_out_file_and_argument_checks(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "c.md"
            self.assertEqual(tool.main(["--today", "2026-11-23", "--months", "1", "--origins", "TPE",
                                        "--destinations", "TYO", "--out", str(out)]), 0)
            self.assertIn("### TPE → TYO (2026-12)", out.read_text(encoding="utf-8"))
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            for bad in (["--months", "0"], ["--months", "13"], ["--months", ""]):
                with self.assertRaises(SystemExit):
                    tool.main(["--today", "2026-11-23", *bad])

    def test_deterministic(self):
        self.assertEqual(generate(), generate())


if __name__ == "__main__":
    unittest.main()
