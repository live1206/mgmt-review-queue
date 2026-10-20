"""Approved PRs without unresolved threads belong in a separate third group."""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

import ready_for_review as queue
from test_ready_for_review import collect, connection, pr, response


class MergeReadyTests(unittest.TestCase):
    def test_pr63960_approved_with_resolved_threads_is_only_in_group3(self):
        candidate = pr(
            63960, title="[AutoPR Azure.ResourceManager.ContainerService]-generated",
            reviewDecision="APPROVED",
            reviewThreads=connection([{"isResolved": True}]),
        )
        report = collect([candidate])
        self.assertEqual(report["ci_passed_count"], 0)
        self.assertEqual(report["ready_count"], 0)
        self.assertEqual(report["merge_ready_count"], 1)
        self.assertEqual([p["number"] for p in report["merge_ready_prs"]], [63960])
        self.assertEqual(report["merge_ready_prs"][0]["total_review_threads"], 1)
        rendered = queue.markdown(report)
        review, merge_ready = rendered.split("### 3.", 1)
        self.assertNotIn(candidate["url"], review)
        self.assertEqual(merge_ready.count(candidate["url"]), 1)
        self.assertIn("1 resolved, 0 unresolved", merge_ready)

    def test_all_three_groups_are_exclusive_sorted_and_deduplicated(self):
        candidates = [
            pr(6, reviewDecision="APPROVED", createdAt="2026-10-02T00:00:00Z"),
            pr(5, reviewDecision="APPROVED"), pr(4, reviewDecision="APPROVED"),
            pr(3, reviewDecision="APPROVED", reviewThreads=connection([{"isResolved": False}])),
            pr(2, reviewDecision=None), pr(1, reviewDecision="CHANGES_REQUESTED"),
        ]
        def api(query, variables):
            if variables["after"] is None:
                return response(candidates[:3], "page2", 6)
            return response(candidates)
        report = queue.collect(api=api)
        self.assertEqual([p["number"] for p in report["ci_passed_prs"]], [3])
        self.assertEqual([p["number"] for p in report["ready_prs"]], [1, 2])
        self.assertEqual([p["number"] for p in report["merge_ready_prs"]], [4, 5, 6])
        self.assertEqual(report["search_results_scanned"], 6)
        for candidate in candidates:
            self.assertEqual(queue.markdown(report).count(candidate["url"]), 1)

    def test_approved_with_late_unresolved_thread_stays_in_group1(self):
        candidate = pr(
            reviewDecision="APPROVED",
            reviewThreads=connection([{"isResolved": True}] * 100, "thread101"),
        )
        def api(query, variables):
            if query == queue.SEARCH_QUERY:
                return response([candidate])
            return {"repository": {"pullRequest": {"reviewThreads": connection([
                {"isResolved": False, "isOutdated": True},
            ])}}}
        report = queue.collect(api=api)
        self.assertEqual(report["ci_passed_count"], 1)
        self.assertEqual(report["ready_count"], 0)
        self.assertEqual(report["merge_ready_count"], 0)
        self.assertEqual(report["ci_passed_prs"][0]["total_review_threads"], 101)

    def test_approved_with_all_threads_resolved_across_pages_is_group3(self):
        candidate = pr(
            reviewDecision="APPROVED",
            reviewThreads=connection([{"isResolved": True}] * 100, "thread101"),
        )
        def api(query, variables):
            if query == queue.SEARCH_QUERY:
                return response([candidate])
            return {"repository": {"pullRequest": {"reviewThreads": connection([{"isResolved": True}])}}}
        report = queue.collect(api=api)
        self.assertEqual(report["merge_ready_count"], 1)
        self.assertEqual(report["merge_ready_prs"][0]["total_review_threads"], 101)

    def test_common_filters_still_apply_to_merge_ready_group(self):
        candidates = [
            pr(state="CLOSED"), pr(isDraft=True), pr(headRefOid="new-head"),
            pr(title="[AutoPR Azure.Provisioning.Example]"), pr(commits={"nodes": []}),
        ]
        for state in ("PENDING", "FAILURE", "ERROR", "EXPECTED", None):
            candidate = pr()
            candidate["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = (
                {"contexts": connection([{"__typename": "StatusContext",
                    "context": queue.REQUIRED_CI_CHECK, "state": state}])} if state else None
            )
            candidates.append(candidate)
        for candidate in candidates:
            candidate["reviewDecision"] = "APPROVED"
            with self.subTest(candidate=candidate):
                report = collect([candidate])
                self.assertEqual(report["ci_passed_count"], 0)
                self.assertEqual(report["ready_count"], 0)
                self.assertEqual(report["merge_ready_count"], 0)

    def test_json_cli_and_criteria_describe_all_three_groups(self):
        report = collect([
            pr(1, reviewThreads=connection([{"isResolved": False}])),
            pr(2), pr(3, reviewDecision="APPROVED"),
        ])
        stdout = io.StringIO()
        with patch.object(queue, "collect", return_value=report):
            with contextlib.redirect_stdout(stdout):
                code = queue.main(["--format", "json"])
        self.assertEqual(code, 0)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["ci_passed_count"], 1)
        self.assertEqual(output["ready_count"], 1)
        self.assertEqual(output["merge_ready_count"], 1)
        self.assertEqual(output["criteria"]["excluded_review_decision"], "APPROVED")
        self.assertEqual(output["merge_ready_criteria"]["required_review_decision"], "APPROVED")
        self.assertEqual(output["merge_ready_criteria"]["unresolved_review_threads"], 0)
        self.assertEqual(output["merge_ready_criteria"]["required_ci_check"], queue.REQUIRED_CI_CHECK)
        self.assertTrue(output["merge_ready_criteria"]["include_prs_without_threads"])
        self.assertIn("No threads", queue.markdown(report).split("### 3.", 1)[1])

    def test_empty_merge_ready_group_is_always_rendered(self):
        report = collect([])
        self.assertEqual(report["merge_ready_prs"], [])
        self.assertEqual(report["merge_ready_count"], 0)
        rendered = queue.markdown(report)
        self.assertIn("### 3. Ready to merge", rendered)
        self.assertIn("No matching PRs are currently ready to merge.", rendered)
        self.assertIn("not merge approval", rendered)


if __name__ == "__main__":
    unittest.main()
