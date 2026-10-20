"""Regression coverage for the restored CI/thread-only readiness policy."""

import contextlib
import io
import json
import subprocess
import unittest
from unittest.mock import patch

import ready_for_review as queue
from test_ready_for_review import pr, response


class RestoredPolicyTests(unittest.TestCase):
    def candidate(self):
        candidate = pr()
        head = "a" * 40
        candidate["headRefOid"] = head
        candidate["commits"]["nodes"][0]["commit"]["oid"] = head
        return candidate

    def test_default_collection_needs_no_clean_review_or_rest_requests(self):
        # Only search/thread data should be read; no review signal is available.
        with patch.object(queue, "rest", create=True, side_effect=AssertionError("Unexpected review API request")):
            report = queue.collect(api=lambda query, variables: response([self.candidate()]))
        self.assertEqual(report["ready_count"], 1)
        self.assertNotIn("require_clean_review", report["criteria"])
        self.assertNotIn("clean_review", report["ready_prs"][0])

    def test_default_cli_accepts_green_ci_and_no_review_threads(self):
        payload = {"data": response([self.candidate()])}
        result = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(queue.subprocess, "run", return_value=result) as run:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = queue.main(["--format", "json"])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["ready_count"], 1)
        self.assertEqual(run.call_count, 1)
        self.assertIn("graphql", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
