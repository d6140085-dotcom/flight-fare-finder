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
# The shipped config with Skyscanner and Trip.com switched back on (and Aviasales in its pre-MVP
# `source` role), as if authorized: keeps their classification tests meaningful while the shipped
# MVP config has both disabled.
AUTHORIZED = tool.sl.load_config()
for _p in AUTHORIZED["platforms"]:
    _p.update({"skyscanner": {"enabled": True}, "tripcom": {"enabled": True},
               "aviasales": {"role": "source"}}.get(_p["id"], {}))


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
            if "attribution" in line.lower():
                self.assertRegex(line, r"(?i)attribution(: | )NOT VERIFIED|attribution is a separate", line)
                self.assertNotRegex(line, r"(?i)attribution(: | )verified", line)
            if "**aviasales**" in line:                      # every Aviasales row says so explicitly
                self.assertTrue(line.endswith("affiliate attribution NOT VERIFIED"), line)

    def test_three_route_classes(self):
        # Trip.com is disabled in the shipped config (B6-2B); classify it as if it were authorized.
        text = generate(origins=["TPE", "KHH", "RMQ"], destinations=["TYO", "OKA", "LON"], month_offsets=[1],
                        cfg=AUTHORIZED)
        blocks = {b.split("\n", 1)[0]: b for b in text.split("### ")[1:]}

        def status(o, d, platform):
            line = next(x for x in blocks[f"{o} → {d} (2026-12)"].splitlines() if x.startswith(f"- **{platform}**"))
            return line.split(" — ", 2)[2]

        browser = {("skyscanner", "TPE", "TYO"), ("skyscanner", "RMQ", "OKA"),
                   ("tripcom", "TPE", "TYO"), ("tripcom", "RMQ", "OKA"), ("tripcom", "KHH", "OKA"),
                   ("aviasales", "TPE", "TYO")}
        for platform in ("skyscanner", "tripcom", "aviasales"):
            for o in ("TPE", "KHH", "RMQ"):
                for d in ("TYO", "OKA", "LON"):
                    st = status(o, d, platform)
                    with self.subTest(platform=platform, route=f"{o}-{d}"):
                        if (platform, o, d) in browser:
                            self.assertTrue(st.startswith("BROWSER_VERIFIED_ROUTE (this exact route opened"), st)
                        else:
                            self.assertNotIn("BROWSER_VERIFIED_ROUTE", st)   # codes alone never suffice
        # both codes verified but the pair never opened -> VERIFIED_CODES_ONLY, not browser-verified
        for platform, o, d in (("skyscanner", "TPE", "OKA"), ("skyscanner", "RMQ", "TYO"),
                               ("tripcom", "KHH", "TYO"), ("tripcom", "TPE", "OKA"), ("tripcom", "RMQ", "TYO"),
                               ("aviasales", "TPE", "LON"), ("aviasales", "KHH", "TYO")):
            self.assertIn("VERIFIED_CODES_ONLY", status(o, d, platform))
            self.assertIn("this combination was NOT opened in a browser", status(o, d, platform))
        # a code outside verified_codes -> UNVERIFIED, general page in the app, candidate URL to check
        for platform, o, d in (("skyscanner", "KHH", "TYO"), ("skyscanner", "KHH", "OKA"),
                               ("skyscanner", "TPE", "LON"), ("tripcom", "RMQ", "LON")):
            self.assertTrue(status(o, d, platform).startswith("UNVERIFIED — "), status(o, d, platform))
        self.assertIn("Candidate itinerary URL (UNVERIFIED, not used by the app): "
                      "https://www.skyscanner.com.tw/transport/flights/tpe/lon/261220/261227/", blocks["TPE → LON (2026-12)"])
        self.assertNotIn("Candidate", blocks["TPE → TYO (2026-12)"])
        self.assertNotIn("Candidate", blocks["TPE → OKA (2026-12)"])
        self.assertIn("GENERAL PAGE BY DESIGN", blocks["TPE → TYO (2026-12)"])

    def test_browser_evidence_is_exactly_the_documented_routes(self):
        self.assertEqual(tool.BROWSER_VERIFIED_ON, "2026-11-06")
        self.assertEqual(dict(tool.BROWSER_VERIFIED_ROUTES), {
            "skyscanner": {("TPE", "TYO"), ("RMQ", "OKA")},
            "tripcom": {("TPE", "TYO"), ("RMQ", "OKA"), ("KHH", "OKA")},
            "aviasales": {("TPE", "TYO")},
        })
        cfg = tool.sl.load_config()
        for pid, routes in tool.BROWSER_VERIFIED_ROUTES.items():   # evidence must agree with the config
            codes = tool.verified_on(cfg, pid)[0]
            for o, d in routes:
                self.assertTrue(codes == "catalog" or (o in codes and d in codes), (pid, o, d))

    def test_route_class_does_not_infer_from_codes(self):
        cfg = tool.sl.load_config()
        self.assertEqual(tool.route_class(cfg, "skyscanner", "TPE", "OKA"), tool.VERIFIED_CODES_ONLY)
        self.assertEqual(tool.route_class(cfg, "skyscanner", "OKA", "TPE"), tool.VERIFIED_CODES_ONLY)  # direction matters
        self.assertEqual(tool.route_class(cfg, "skyscanner", "TPE", "TYO"), tool.BROWSER_VERIFIED_ROUTE)
        self.assertEqual(tool.route_class(cfg, "skyscanner", "KHH", "OKA"), tool.UNVERIFIED)
        self.assertEqual(tool.route_class(cfg, "tripcom", "KHH", "OKA"), tool.BROWSER_VERIFIED_ROUTE)
        self.assertIsNone(tool.route_class(cfg, "google", "TPE", "TYO"))

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
        self.assertEqual(hosts, {"www.aviasales.com", "www.google.com"})   # shipped MVP config
        hosts = set(re.findall(r"https://([^/\s?]+)", generate(cfg=AUTHORIZED)))
        self.assertEqual(hosts, {"www.skyscanner.com.tw", "tw.trip.com", "www.aviasales.com", "www.google.com"})

    def test_disabled_providers_are_not_listed_and_are_named(self):
        """Shipped MVP config: Skyscanner and Trip.com rows are absent and the header says why."""
        text = generate()
        for absent in ("**tripcom**", "trip.com/", "**skyscanner**", "skyscanner.com.tw"):
            self.assertNotIn(absent, text)
        self.assertIn("**Not listed (disabled in search-platforms.json): Skyscanner, Trip.com.** Don't open their",
                      text)
        self.assertIn("**aviasales**", text)
        self.assertNotIn("Not listed", generate(cfg=AUTHORIZED))

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
