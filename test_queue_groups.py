"""Required-CI-passing PRs are split into exclusive review-thread groups."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ready_for_review as queue
from test_ready_for_review import collect, connection, pr, response


class QueueGroupTests(unittest.TestCase):
    def test_groups_are_exclusive_and_unresolved_threads_only_block_final_review(self):
        pending = pr(4)
        pending["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"][0].update(
            status="IN_PROGRESS", conclusion=None,
        )
        report = collect([
            pr(3, reviewThreads=connection([
                {"isResolved": True}, {"isResolved": False, "isOutdated": True},
            ])),
            pr(1), pr(2, reviewThreads=connection([{"isResolved": True}])), pending,
        ])
        self.assertEqual(report["ci_passed_count"], 1)
        self.assertEqual([p["number"] for p in report["ci_passed_prs"]], [3])
        self.assertEqual(report["ready_count"], 2)
        self.assertEqual([p["number"] for p in report["ready_prs"]], [1, 2])
        self.assertEqual(report["ci_passed_prs"][0]["unresolved_review_threads"], 1)
        self.assertEqual(report["ci_passed_prs"][0]["total_review_threads"], 2)
        self.assertEqual(report["ci_passed_criteria"]["minimum_unresolved_review_threads"], 1)
        self.assertEqual(report["criteria"]["unresolved_review_threads"], 0)

    def test_ci_and_common_filters_apply_to_both_groups(self):
        candidates = [
            pr(title="[AutoPR Azure.Provisioning.Example]"), pr(state="CLOSED"),
            pr(isDraft=True), pr(headRefOid="new-head"),
            pr(commits={"nodes": []}),
        ]
        for state in ("PENDING", "FAILURE", "ERROR", "EXPECTED", None):
            candidate = pr()
            candidate["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = (
                {"contexts": connection([{"__typename": "StatusContext",
                    "context": queue.REQUIRED_CI_CHECK, "state": state}])} if state else None
            )
            candidates.append(candidate)
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                report = collect([candidate])
                self.assertEqual(report["ci_passed_count"], 0)
                self.assertEqual(report["ready_count"], 0)

    def test_late_unresolved_thread_keeps_pr_in_ci_group_only(self):
        candidate = pr(reviewThreads=connection([{"isResolved": True}] * 100, "thread101"))
        def api(query, variables):
            if query == queue.SEARCH_QUERY:
                return response([candidate])
            return {"repository": {"pullRequest": {"reviewThreads": connection([{"isResolved": False}])}}}
        report = queue.collect(api=api)
        self.assertEqual(report["ci_passed_count"], 1)
        self.assertEqual(report["ready_count"], 0)
        self.assertEqual(report["ci_passed_prs"][0]["total_review_threads"], 101)
        self.assertEqual(report["ci_passed_prs"][0]["unresolved_review_threads"], 1)

    def test_both_groups_are_sorted_and_deduplicated_across_search_pages(self):
        newer = pr(1, createdAt="2026-10-02T00:00:00Z")
        unresolved = pr(4, reviewThreads=connection([{"isResolved": False}]))
        newer_unresolved = pr(5, createdAt="2026-10-02T00:00:00Z",
                              reviewThreads=connection([{"isResolved": False}]))
        def api(query, variables):
            if variables["after"] is None:
                return response([newer, newer_unresolved], "page2", 5)
            return response([newer, newer_unresolved, pr(3), unresolved, pr(2)])
        report = queue.collect(api=api)
        self.assertEqual(report["search_results_scanned"], 5)
        self.assertEqual([p["number"] for p in report["ci_passed_prs"]], [4, 5])
        self.assertEqual([p["number"] for p in report["ready_prs"]], [2, 3, 1])

    def test_markdown_shows_both_groups_and_actual_thread_counts(self):
        report = collect([pr(1), pr(2, reviewThreads=connection([{"isResolved": False}]))])
        rendered = queue.markdown(report)
        self.assertIn("### 1. net - pullrequest passed + unresolved review threads (1)", rendered)
        self.assertIn("### 2. net - pullrequest passed + no unresolved review threads (1)", rendered)
        first, second = rendered.split("### 2.", 1)
        self.assertIn("0 resolved, 1 unresolved", first)
        self.assertIn(pr(2)["url"], first)
        self.assertNotIn(pr(2)["url"], second)
        self.assertNotIn(pr(1)["url"], first)
        self.assertEqual(rendered.count(pr(1)["url"]), 1)
        self.assertIn("exclusive", rendered)
        self.assertIn("not merge approval", rendered)

    def test_empty_groups_render_separately(self):
        rendered = queue.markdown(collect([]))
        self.assertIn("### 1. net - pullrequest passed + unresolved review threads (0)", rendered)
        self.assertIn("### 2. net - pullrequest passed + no unresolved review threads (0)", rendered)
        self.assertIn("No matching PRs currently have passing net - pullrequest CI and unresolved review threads.", rendered)
        self.assertIn("No matching PRs are currently ready for final review.", rendered)
        rendered = queue.markdown(collect([pr(reviewThreads=connection([{"isResolved": False}]))]))
        self.assertIn("### 1. net - pullrequest passed + unresolved review threads (1)", rendered)
        self.assertIn("### 2. net - pullrequest passed + no unresolved review threads (0)", rendered)

    def test_json_cli_contains_both_groups(self):
        report = collect([pr(1), pr(2, reviewThreads=connection([{"isResolved": False}]))])
        stdout = io.StringIO()
        with patch.object(queue, "collect", return_value=report):
            with contextlib.redirect_stdout(stdout):
                code = queue.main(["--format", "json"])
        self.assertEqual(code, 0)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["ci_passed_count"], 1)
        self.assertEqual(output["ready_count"], 1)
        self.assertEqual(output["schema_version"], 1)

    def test_thread_pagination_failure_preserves_whole_previous_report(self):
        candidate = pr(reviewThreads=connection([{"isResolved": True}] * 100, "thread101"))
        def api(query, variables):
            if query == queue.SEARCH_QUERY:
                return response([candidate])
            raise RuntimeError("thread page unavailable")
        original_collect = queue.collect
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "latest.md"
            output.write_text("old complete report", encoding="utf-8")
            with patch.object(queue, "collect", side_effect=lambda repo: original_collect(repo, api=api)):
                with contextlib.redirect_stderr(io.StringIO()):
                    code = queue.main(["--output", str(output)])
            self.assertEqual(code, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "old complete report")


if __name__ == "__main__":
    unittest.main()
