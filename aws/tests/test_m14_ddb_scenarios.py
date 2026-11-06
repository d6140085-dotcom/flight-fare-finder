"""B4 concurrency validation scenarios (aws/integration/m14_scenarios.py) run locally.

Engine: moto's DynamoDB-compatible server (default), or Amazon DynamoDB Local when
DDB_ENDPOINT=http://127.0.0.1:8000 is set (aws/scripts/test_dynamodb_local.sh). The same
scenarios run against real DynamoDB only through the temporary, owner-approved test stack
(aws/integration/README.md). Skips without boto3/moto: pip install -r aws/requirements-test.txt
"""
import os
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ddb_local  # noqa: E402  (before anything imports boto3)

sys.path.insert(0, str(HERE.parent / "lambdas" / "flight-subscriptions-api"))
sys.path.insert(0, str(HERE.parent / "integration"))
if ddb_local.boto3 is not None:
    import m14_scenarios as sc  # noqa: E402
else:
    sc = None


@unittest.skipUnless(ddb_local.available(), "boto3/moto (or DDB_ENDPOINT) not available")
class B4Scenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = ddb_local.client()
        cls.subs, cls.quota = ddb_local.create_tables(cls.client)
        engine = "dynamodb-local" if os.environ.get("DDB_ENDPOINT") else "moto"
        cls.ctx = sc.Ctx(cls.client, cls.subs, cls.quota, uuid.uuid4().hex[:8], engine)
        cls.bystander = cls.ctx.user("bystander")
        cls.ctx.seed(cls.bystander, "TPE-TYO", plan_name="tokyo")
        cls.ctx.seed_quota(cls.bystander, 1, 3)
        cls.before = cls.ctx.raw_items(cls.bystander)

    @classmethod
    def tearDownClass(cls):
        ddb_local.drop_tables(cls.client, cls.subs, cls.quota)

    def run_scenario(self, fn):
        result = fn(self.ctx)
        self.assertTrue(result["passed"], f"{result['scenario']}: {result['failures']} {result['metrics']}")
        self.assertEqual(self.ctx.raw_items(self.bystander), self.before, "I5 bystander changed")

    def test_parallelism_is_at_least_50(self):
        self.assertGreaterEqual(sc.PARALLEL, 50)


if sc is not None:
    for _fn in sc.SCENARIOS:
        setattr(B4Scenarios, f"test_{_fn.__name__}", lambda self, fn=_fn: self.run_scenario(fn))


if __name__ == "__main__":
    unittest.main()
