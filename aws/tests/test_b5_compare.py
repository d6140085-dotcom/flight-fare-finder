"""Offline tests for aws/tools/b5_compare.py. No network, no AWS: every live-path test injects a
fake opener, and a guard makes any real socket or urlopen call fail the test."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

AWS = Path(__file__).resolve().parents[1]
TOOL = AWS / "tools" / "b5_compare.py"
TOKEN = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
ENV_OK = {"TP_TOKEN": TOKEN, "B5_LIVE_APPROVED": "yes"}


def no_network(*a, **k):
    raise AssertionError("network access attempted")


def load_tool():
    with mock.patch("socket.socket", no_network), mock.patch("urllib.request.urlopen", no_network):
        spec = importlib.util.spec_from_file_location("b5_compare", TOOL)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    return m


b5 = load_tool()


class FakeResponse(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(json.dumps(body).encode() if not isinstance(body, bytes) else body)
        self.status, self.headers = status, headers or {}


def offer(price, dep, ret, **kw):
    o = {"price": price, "airline": "BR", "departure_at": dep, "return_at": ret}
    o.update(kw)
    return o


class FakeApi:
    """Answers like Travelpayouts from a scenario function; records every request it receives."""
    def __init__(self, scenario=None, status_for=None):
        self.calls, self.scenario, self.status_for = [], scenario or self.default, status_for or {}

    @staticmethod
    def month_dates(month):
        return f"{month}-11T07:30:00+08:00", f"{month}-18T19:00:00+09:00"

    def default(self, api, q):
        dep, ret = self.month_dates(q.get("departure_at") or q.get("depart_date"))
        if api == "v1":
            return {"success": True, "currency": q["currency"],
                    "data": {q["destination"]: {"0": offer(9000, dep, ret, expires_at="2099-01-01T00:00:00Z")}}}
        return {"success": True, "currency": q["currency"],
                "data": [offer(8800, dep, ret, origin=q["origin"], destination=q["destination"])]}

    def __call__(self, req, timeout):
        url = req.full_url
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        api = "v3" if "/v3/" in url else "v1"
        self.calls.append({"url": url, "headers": dict(req.header_items()), "query": q})
        n = len(self.calls)
        if n in self.status_for:
            raise urllib.error.HTTPError(url, self.status_for[n], "err", {"Retry-After": "60"}, None)
        return FakeResponse(self.scenario(api, q), headers={"X-RateLimit-Remaining": str(600 - n),
                                                            "X-RateLimit-Limit": "600", "Set-Cookie": "x=1"})


class Clock:
    def __init__(self):
        self.t, self.sleeps, self.starts = 1000.0, [], []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def run_live(tmp, api=None, argv_extra=(), env=ENV_OK, clock=None):
    api = api or FakeApi()
    clock = clock or Clock()
    lines = []
    factory = lambda cap, interval: b5.LiveTransport(cap, interval, opener=api, clock=clock.clock, sleep=clock.sleep)
    with mock.patch("socket.socket", no_network):
        code = b5.main(["live", "--round", "1", "--confirm-live", "--out", str(Path(tmp) / "r1"), *argv_extra],
                       env=dict(env), out=lines.append, transport_factory=factory)
    return code, api, clock, lines


class PlanTests(unittest.TestCase):
    def test_plan_matches_the_approved_sample(self):
        reqs = b5.build_requests(b5.load_plan(), datetime(2026, 11, 9, 6, tzinfo=timezone.utc), b5.config.load_catalog())
        items = {r.item_key for r in reqs if r.variant == "twd"}
        self.assertEqual(len(items), 36)
        self.assertEqual(len(reqs), 75)                       # 36 v1 + 36 v3 + 3 market checks
        self.assertEqual({r.origin for r in reqs}, {"TPE", "KHH", "RMQ"})
        self.assertEqual({r.month for r in reqs}, {"2026-12", "2027-03", "2027-09"})
        dests = {r.destination for r in reqs}
        self.assertTrue({"LON", "NYC", "SYD"} <= dests and {"TYO", "OKA", "HKG"} <= dests)   # long and short haul
        self.assertEqual([r.variant for r in reqs].count("market"), 3)
        self.assertLessEqual(len(reqs) * 3, 300)              # three rounds fit the absolute cap

    def test_usd_adds_one_v3_request_per_item(self):
        reqs = b5.build_requests(b5.load_plan(), datetime(2026, 11, 9, tzinfo=timezone.utc), b5.config.load_catalog(), usd=True)
        self.assertEqual((len(reqs), sum(r.variant == "usd" for r in reqs)), (111, 36))

    def test_requests_are_round_trip_twd_and_market_only_on_the_subset(self):
        for r in b5.build_requests(b5.load_plan(), datetime(2026, 11, 9, tzinfo=timezone.utc), b5.config.load_catalog()):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(b5.request_url(r)).query))
            self.assertEqual(q["currency"], "twd")
            if r.api == "v3":
                self.assertEqual(q["one_way"], "false")
            self.assertEqual("market" in q, r.variant == "market")

    def test_plan_rejects_unknown_codes_and_duplicates(self):
        now, cat = datetime(2026, 11, 9, tzinfo=timezone.utc), b5.config.load_catalog()
        bad = {"groups": [{"origin": "TPE", "month_offsets": [1], "destinations": ["ZZZ"]}]}
        with self.assertRaises(ValueError):
            b5.build_requests(bad, now, cat)
        dup = {"groups": [{"origin": "TPE", "month_offsets": [1], "destinations": ["TYO", "TYO"]}]}
        with self.assertRaises(ValueError):
            b5.build_requests(dup, now, cat)

    def test_month_rollover_in_taipei(self):
        reqs = b5.build_requests(b5.load_plan(), datetime(2026, 12, 31, 16, 30, tzinfo=timezone.utc), b5.config.load_catalog())
        self.assertEqual(min(r.month for r in reqs), "2027-02")


class NoNetworkTests(unittest.TestCase):
    def test_preview_makes_no_request_even_with_token_set(self):
        lines = []
        with mock.patch("socket.socket", no_network), mock.patch("urllib.request.urlopen", no_network):
            code = b5.main(["preview"], env=dict(ENV_OK), out=lines.append)
        self.assertEqual(code, 0)
        text = "\n".join(lines)
        self.assertIn("set (never shown)", text)
        self.assertNotIn(TOKEN, text)
        self.assertEqual(sum(" https://api.travelpayouts.com/" in l for l in lines), 75)

    def test_tool_does_not_import_boto3_or_touch_aws(self):
        import re
        src = TOOL.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^\s*(import|from)\s+(boto3|botocore|smtplib|sqlite3)\b", src, re.M))
        script = (
            "import socket, sys, runpy\n"
            "def no(*a, **k): raise SystemExit('network attempted')\n"
            "socket.socket.connect = no; socket.socket.connect_ex = no; socket.create_connection = no\n"
            f"sys.argv = ['b5', 'preview']\n"
            f"try:\n    runpy.run_path(r'{TOOL}', run_name='__main__')\n"
            "except SystemExit as e:\n    assert e.code in (0, None), e.code\n"
            "assert 'boto3' not in sys.modules and 'botocore' not in sys.modules\n"
            "print('OK-NO-AWS')\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("AWS")}
        r = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, env=env, timeout=60)
        self.assertIn("OK-NO-AWS", r.stdout, r.stderr)

    def test_module_import_makes_no_request(self):
        load_tool()   # load_tool itself fails the test on any socket/urlopen use


class GateTests(unittest.TestCase):
    def refused(self, argv, env):
        lines = []
        with mock.patch("socket.socket", no_network), mock.patch("urllib.request.urlopen", no_network):
            code = b5.main(argv, env=env, out=lines.append, transport_factory=lambda *a: self.fail("transport built"))
        self.assertEqual(code, 2, lines)
        self.assertTrue(lines[-1].startswith("REFUSED"))
        self.assertNotIn(TOKEN, "\n".join(lines))
        return lines[-1]

    def test_live_gates(self):
        with tempfile.TemporaryDirectory() as t:
            base = ["live", "--round", "1", "--out", str(Path(t) / "x")]
            self.assertIn("--confirm-live", self.refused(base, dict(ENV_OK)))
            self.assertIn("B5_LIVE_APPROVED", self.refused(base + ["--confirm-live"], {"TP_TOKEN": TOKEN}))
            self.assertIn("B5_LIVE_APPROVED", self.refused(base + ["--confirm-live"], {"TP_TOKEN": TOKEN, "B5_LIVE_APPROVED": "true"}))
            self.assertIn("TP_TOKEN is not set", self.refused(base + ["--confirm-live"], {"B5_LIVE_APPROVED": "yes"}))
            for bad in ("paste-token-here", "short", "x" * 70, "token with spaces 1234567890"):
                self.assertIn("does not look like", self.refused(base + ["--confirm-live"], {"TP_TOKEN": bad, "B5_LIVE_APPROVED": "yes"}))

    def test_caps_and_interval_limits(self):
        with tempfile.TemporaryDirectory() as t:
            base = ["live", "--round", "1", "--confirm-live", "--out", str(Path(t) / "x")]
            self.assertIn("1..300", self.refused(base + ["--max-requests", "301"], dict(ENV_OK)))
            self.assertIn("above --max-requests", self.refused(base + ["--max-requests", "50"], dict(ENV_OK)))
            self.assertIn("at least 1.0", self.refused(base + ["--interval", "0.5"], dict(ENV_OK)))

    def test_refuses_a_non_empty_output_folder(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "r1").mkdir()
            (Path(t) / "r1" / "old.txt").write_text("x")
            self.assertIn("not empty", self.refused(["live", "--round", "1", "--confirm-live", "--out", str(Path(t) / "r1")], dict(ENV_OK)))


class LiveRoundTests(unittest.TestCase):
    def test_full_round_with_fake_api(self):
        with tempfile.TemporaryDirectory() as t:
            code, api, clock, lines = run_live(t)
            out = Path(t) / "r1"
            self.assertEqual(code, 0, lines)
            self.assertEqual(len(api.calls), 75)
            for c in api.calls:                                  # token only in the header
                self.assertNotIn(TOKEN, c["url"])
                self.assertEqual(c["headers"]["X-access-token"], TOKEN)
            self.assertEqual(sorted(p.name for p in out.iterdir()),
                             ["comparisons.json", "requests.jsonl", "run.json", "summary.json", "summary.md"])
            self.assertEqual(b5.check_output(out, TOKEN), {})
            blob = "".join(p.read_text(encoding="utf-8") for p in out.iterdir())
            self.assertNotIn(TOKEN, blob)
            self.assertNotIn("Set-Cookie", blob)                 # only rate-limit headers are kept
            self.assertIn("X-RateLimit-Remaining", blob)
            summary = json.loads((out / "summary.json").read_text())
            self.assertEqual(summary["requests_sent"], 75)
            self.assertEqual(summary["rate_limit_lowest_remaining"], 525)
            self.assertTrue(summary["compat"]["switch_ok"])
            self.assertIn(b5.token_fingerprint(TOKEN), "\n".join(lines))
            self.assertNotIn(TOKEN, "\n".join(lines))

    def test_requests_are_sequential_and_spaced(self):
        with tempfile.TemporaryDirectory() as t:
            clock = Clock()
            starts = []
            api = FakeApi()
            orig = api.__call__

            class Spy(FakeApi):
                def __call__(self, req, timeout):
                    starts.append(clock.t)
                    return orig(req, timeout)
            code, *_ = run_live(t, api=Spy(), clock=clock, argv_extra=["--interval", "2.5"])
            self.assertEqual(code, 0)
            gaps = [b - a for a, b in zip(starts, starts[1:])]
            self.assertEqual(len(starts), 75)
            self.assertTrue(all(g >= 2.5 - 1e-9 for g in gaps), min(gaps))

    def test_transport_cap_is_a_hard_stop(self):
        clock = Clock()
        tr = b5.LiveTransport(2, 1.0, opener=FakeApi(), clock=clock.clock, sleep=clock.sleep)
        req = lambda: ("https://api.travelpayouts.com/v1/prices/cheap?origin=TPE&destination=TYO&depart_date=2026-12&currency=twd",
                       {"X-Access-Token": TOKEN}, 10)
        tr(*req())
        tr(*req())
        with self.assertRaises(b5.Abort):
            tr(*req())
        self.assertEqual(tr.count, 2)

    def test_stops_on_auth_failure_rate_limit_and_repeated_errors(self):
        for statuses, expect_calls, words in (({1: 401}, 1, "token rejected"), ({3: 429}, 3, "rate limited"),
                                              ({1: 500, 2: 502, 3: 503}, 3, "consecutive errors")):
            with tempfile.TemporaryDirectory() as t:
                code, api, _, lines = run_live(t, api=FakeApi(status_for=statuses))
                self.assertEqual(code, 1)
                self.assertEqual(len(api.calls), expect_calls)
                summary = json.loads((Path(t) / "r1" / "summary.json").read_text())
                self.assertIn(words, summary["aborted"])
                self.assertEqual(b5.check_output(Path(t) / "r1", TOKEN), {})

    def test_usd_only_in_round_one(self):
        with tempfile.TemporaryDirectory() as t:
            _, api, _, _ = run_live(t, argv_extra=["--usd"])
            self.assertEqual(sum(c["query"]["currency"] == "usd" for c in api.calls), 36)
        with tempfile.TemporaryDirectory() as t:
            api = FakeApi()
            clock = Clock()
            factory = lambda cap, interval: b5.LiveTransport(cap, interval, opener=api, clock=clock.clock, sleep=clock.sleep)
            b5.main(["live", "--round", "2", "--confirm-live", "--usd", "--out", str(Path(t) / "r2")], env=dict(ENV_OK),
                    out=lambda m: None, transport_factory=factory)
            self.assertEqual(sum(c["query"]["currency"] == "usd" for c in api.calls), 0)


def risky_scenario(api, q):
    """Every risk the plan lists, on TPE-VCE and TPE-TYO; other items return clean fares."""
    m = q.get("departure_at") or q.get("depart_date")
    dep, ret = FakeApi.month_dates(m)
    route = f"{q['origin']}-{q['destination']}"
    if route == "TPE-TYO" and api == "v3":
        return {"success": True, "currency": "twd", "data": [
            offer(3000, dep, None, origin="TPE", destination="TYO"),                          # one-way / missing return
            offer(3100, dep, ret, origin="TPE", destination="TYO", currency="usd"),          # currency mismatch
            offer(3200, "2030-01-05T08:00:00+08:00", "2030-01-09T08:00:00+08:00", origin="TPE", destination="TYO"),  # wrong month
            offer(9100, dep, ret, origin="TPE", destination="TYO", origin_airport="TSA", destination_airport="HND")]}
    if route == "TPE-TYO" and api == "v1":
        return {"success": True, "currency": "twd", "data": {"TYO": {
            "0": offer(2500, dep, ret, expires_at="2020-01-01T00:00:00Z"),                   # expired cache
            "1": offer(9300, dep, ret, expires_at="2099-01-01T00:00:00Z")}}}
    if route == "TPE-VCE" and api == "v3":
        return {"success": True, "currency": "twd", "data": [
            offer(21000, dep, ret, origin="TPE", destination="VCE", destination_airport="TSF"),   # Treviso
            offer(26000, dep, ret, origin="TPE", destination="VCE", destination_airport="VCE")]}
    if route == "TPE-KMJ":
        return {"success": True, "currency": "twd", "data": [] if api == "v3" else {}}      # missing data
    if route == "TPE-SYD" and api == "v1":
        return {"success": True, "currency": "twd", "data": {}}                             # v3-only availability
    if route == "TPE-NYC" and api == "v3":
        return b"{not json"                                                                  # malformed
    return FakeApi().default(api, q)


class RiskReportTests(unittest.TestCase):
    def test_every_listed_risk_is_detected_and_reported(self):
        def scenario(api, q):
            r = risky_scenario(api, q)
            return r
        with tempfile.TemporaryDirectory() as t:
            code, api, _, _ = run_live(t, api=FakeApi(scenario=scenario))
            self.assertEqual(code, 0)
            out = Path(t) / "r1"
            s = json.loads((out / "summary.json").read_text())
            r = s["risks"]
            for key in ("rejected_one_way", "rejected_currency_mismatch", "rejected_wrong_month", "rejected_expired",
                        "rejected_destination_airport_unexpected"):
                self.assertGreaterEqual(r.get(key, 0), 1, key)
            self.assertGreaterEqual(r["false_positives_prevented"], 2)          # v3 TPE-TYO and v1 TPE-TYO
            self.assertTrue(any(c["destination_airport"] == "TSF" and c["price"] == 21000
                                for c in s["airport_rejected_cheaper"]))
            self.assertIn("TPE-SYD#2026-12", s["availability"]["v3_only"])
            self.assertIn("TPE-NYC#2026-12", s["availability"]["v1_only"])
            rows = [json.loads(l) for l in (out / "requests.jsonl").read_text().splitlines()]
            nyc = next(x for x in rows if x["item"] == "TPE-NYC#2026-12" and x["api"] == "v3")
            self.assertEqual((nyc["kind"], nyc["reason"]), ("error", "malformed"))
            kmj = [x for x in rows if x["item"].startswith("TPE-KMJ")]
            self.assertTrue(all(x["kind"] == "no_data" for x in kmj))
            tyo = next(x for x in rows if x["item"] == "TPE-TYO#2026-12" and x["api"] == "v3" and x["variant"] == "twd")
            self.assertEqual(tyo["chosen"]["price"], 9100)                       # never the 3,000 one-way
            self.assertEqual(tyo["signals"]["naive_min_price"], 3000)
            md = (out / "summary.md").read_text()
            self.assertIn("Cheaper offers rejected for airport mapping", md)

    def test_summarize_evaluates_criteria_across_rounds(self):
        with tempfile.TemporaryDirectory() as t:
            dirs = []
            for n in (1, 2):
                api, clock = FakeApi(scenario=risky_scenario), Clock()
                factory = lambda cap, interval, api=api, clock=clock: b5.LiveTransport(cap, interval, opener=api,
                                                                                       clock=clock.clock, sleep=clock.sleep)
                d = Path(t) / f"r{n}"
                b5.main(["live", "--round", str(n), "--confirm-live", "--out", str(d)], env=dict(ENV_OK),
                        out=lambda m: None, transport_factory=factory)
                dirs.append(str(d))
            lines = []
            b5.main(["summarize", *dirs, "--out", str(Path(t) / "b5.json")], env={}, out=lines.append)
            report = json.loads((Path(t) / "b5.json").read_text())
            c = report["criteria"]
            self.assertEqual(report["requests"], 150)
            self.assertEqual(c["4_airport_rejections_understood"][0], "review")
            self.assertEqual(c["3_currency_and_round_trip"][0], "pass")
            self.assertIn(c["1_v3_availability_at_least_v1"][0], ("pass", "fail"))


class SanitizationTests(unittest.TestCase):
    def test_token_echoed_by_the_api_is_redacted(self):
        def echo(api, q):
            body = FakeApi().default(api, q)
            body["error_detail"] = f"token {TOKEN} accepted"   # hypothetical echo
            return body
        with tempfile.TemporaryDirectory() as t:
            code, *_ = run_live(t, api=FakeApi(scenario=echo))
            out = Path(t) / "r1"
            self.assertEqual(code, 0)
            self.assertNotIn(TOKEN, "".join(p.read_text() for p in out.iterdir()))

    def test_check_output_finds_planted_secrets(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "clean.json").write_text('{"price": 8062}')
            (d / "bad1.txt").write_text(f"x {TOKEN} y")
            (d / "bad2.txt").write_text("contact someone@example.com")
            (d / "bad3.txt").write_text("X-Access-Token: zzz")
            (d / "bad4.txt").write_text("link /search/TPE?marker=123456")
            found = b5.check_output(d, TOKEN)
            self.assertEqual(sorted(found), ["bad1.txt", "bad2.txt", "bad3.txt", "bad4.txt"])
            lines = []
            self.assertEqual(b5.main(["check-output", str(d)], env={"TP_TOKEN": TOKEN}, out=lines.append), 3)
            self.assertNotIn(TOKEN, "\n".join(lines))

    def test_sanitize_keys_and_nested_values(self):
        data = {TOKEN: [f"a{TOKEN}b", {"k": TOKEN}], "n": 1}
        self.assertNotIn(TOKEN, json.dumps(b5.sanitize(data, TOKEN)))


if __name__ == "__main__":
    unittest.main()
