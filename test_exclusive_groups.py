"""A PR belongs to exactly one required-CI-passing review group."""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

import ready_for_review as queue
from test_ready_for_review import collect, connection, pr


class ExclusiveGroupTests(unittest.TestCase):
    def test_pr63702_with_no_threads_is_only_in_group2(self):
        candidate = pr(63702, title="[AutoPR Azure.ResourceManager.NetworkCloud]-generated")
        report = collect([candidate])
        self.assertEqual(report["ci_passed_count"], 0)
        self.assertEqual(report["ci_passed_prs"], [])
        self.assertEqual([p["number"] for p in report["ready_prs"]], [63702])
        rendered = queue.markdown(report)
        first, second = rendered.split("### 2.", 1)
        self.assertNotIn(candidate["url"], first)
        self.assertIn(candidate["url"], second)
        self.assertEqual(rendered.count(candidate["url"]), 1)

    def test_groups_partition_all_eligible_prs_without_losing_resolved_threads(self):
        candidates = [
            pr(1),
            pr(2, reviewThreads=connection([{"isResolved": True}])),
            pr(3, reviewThreads=connection([{"isResolved": False, "isOutdated": True}])),
            pr(4, reviewThreads=connection([{"isResolved": True}, {"isResolved": False}])),
        ]
        report = collect(candidates)
        first = {p["number"] for p in report["ci_passed_prs"]}
        second = {p["number"] for p in report["ready_prs"]}
        self.assertEqual(first, {3, 4})
        self.assertEqual(second, {1, 2})
        self.assertFalse(first & second)
        self.assertEqual(first | second, {1, 2, 3, 4})
        self.assertEqual(report["ci_passed_count"] + report["ready_count"], 4)
        rendered = queue.markdown(report)
        for candidate in candidates:
            self.assertEqual(rendered.count(candidate["url"]), 1)

    def test_criteria_and_labels_explain_the_exclusive_split(self):
        report = collect([])
        self.assertEqual(report["ci_passed_criteria"]["minimum_unresolved_review_threads"], 1)
        self.assertFalse(report["ci_passed_criteria"]["include_prs_without_threads"])
        self.assertEqual(report["criteria"]["unresolved_review_threads"], 0)
        self.assertTrue(report["criteria"]["include_prs_without_threads"])
        rendered = queue.markdown(report)
        self.assertIn("Groups are exclusive", rendered)
        self.assertIn("### 1. net - pullrequest passed + unresolved review threads (0)", rendered)
        self.assertNotIn("subset", rendered)
        self.assertNotIn("appear in both", rendered)

    def test_json_cli_uses_the_same_exclusive_membership(self):
        report = collect([pr(1), pr(2, reviewThreads=connection([{"isResolved": False}]))])
        stdout = io.StringIO()
        with patch.object(queue, "collect", return_value=report):
            with contextlib.redirect_stdout(stdout):
                code = queue.main(["--format", "json"])
        self.assertEqual(code, 0)
        output = json.loads(stdout.getvalue())
        self.assertEqual([p["number"] for p in output["ci_passed_prs"]], [2])
        self.assertEqual([p["number"] for p in output["ready_prs"]], [1])
        self.assertEqual(output["ci_passed_count"], 1)
        self.assertEqual(output["ready_count"], 1)


if __name__ == "__main__":
    unittest.main()
