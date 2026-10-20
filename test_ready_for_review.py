import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ready_for_review as queue


def connection(nodes, cursor=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}


def pr(number=1, **changes):
    result = {
        "number": number, "title": "[AutoPR Azure.ResourceManager.Example]-generated",
        "url": f"https://github.com/Azure/azure-sdk-for-net/pull/{number}",
        "createdAt": "2026-10-01T00:00:00Z", "state": "OPEN", "isDraft": False,
        "headRefOid": "abc", "reviewDecision": "REVIEW_REQUIRED",
        "commits": {"nodes": [{"commit": {"oid": "abc", "statusCheckRollup": {
            "contexts": connection([{"__typename": "CheckRun", "name": queue.REQUIRED_CI_CHECK,
                                     "status": "COMPLETED", "conclusion": "SUCCESS"}]),
        }}}]},
        "reviewThreads": connection([]),
    }
    result.update(changes)
    return result


def response(prs, cursor=None, count=None):
    return {"search": dict(connection(prs, cursor), issueCount=len(prs) if count is None else count)}


def collect(prs):
    return queue.collect(api=lambda query, variables: response(prs))


class QueueTests(unittest.TestCase):
    def test_no_threads_and_resolved_threads_qualify(self):
        report = collect([
            pr(1), pr(2, reviewThreads=connection([{"isResolved": True}])),
            pr(3, reviewThreads=connection([{"isResolved": False, "isOutdated": True}])),
        ])
        self.assertEqual([p["number"] for p in report["ready_prs"]], [1, 2])
        self.assertEqual(report["ready_prs"][1]["total_review_threads"], 1)

    def test_excludes_stale_search_results_and_other_packages(self):
        cases = [
            pr(title="[AutoPR Azure.Provisioning.Example]"), pr(state="CLOSED"),
            pr(isDraft=True), pr(headRefOid="new-head"),
            pr(commits={"nodes": []}),
        ]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                self.assertEqual(collect([candidate])["ready_count"], 0)

    def test_ci_must_be_successful_and_present(self):
        for state in ("PENDING", "FAILURE", "ERROR", "EXPECTED", None):
            candidate = pr()
            candidate["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = (
                {"contexts": connection([{"__typename": "StatusContext",
                    "context": queue.REQUIRED_CI_CHECK, "state": state}])} if state else None
            )
            with self.subTest(state=state):
                self.assertEqual(collect([candidate])["ready_count"], 0)

    def test_search_pages_limit_nested_ci_and_thread_fanout(self):
        # 100 PRs with nested CI contexts/threads repeatedly hit GitHub HTTP 504.
        self.assertIn("type: ISSUE, first: 25", queue.SEARCH_QUERY)

    def test_search_pagination_and_deduplication(self):
        calls = []
        def api(query, variables):
            calls.append(variables.copy())
            return response([pr(2)], "page2", 2) if variables["after"] is None else response([pr(2), pr(1)])
        report = queue.collect(api=api)
        self.assertEqual(report["search_results_scanned"], 2)
        self.assertEqual([p["number"] for p in report["ready_prs"]], [1, 2])
        self.assertEqual(calls[1]["after"], "page2")
        self.assertNotIn("status:", calls[0]["query"])
        self.assertNotIn("review:", calls[0]["query"])

    def test_unresolved_thread_after_first_hundred_excludes_pr(self):
        candidate = pr(reviewThreads=connection([{"isResolved": True}] * 100, "thread101"))
        calls = []
        def api(query, variables):
            calls.append((query, variables))
            if query == queue.SEARCH_QUERY:
                return response([candidate])
            return {"repository": {"pullRequest": {"reviewThreads": connection([{"isResolved": False}])}}}
        self.assertEqual(queue.collect(api=api)["ready_count"], 0)
        self.assertEqual(calls[1][1]["after"], "thread101")

    def test_all_resolved_threads_across_pages_are_counted(self):
        candidate = pr(reviewThreads=connection([{"isResolved": True}] * 100, "thread101"))
        def api(query, variables):
            if query == queue.SEARCH_QUERY:
                return response([candidate])
            return {"repository": {"pullRequest": {"reviewThreads": connection([{"isResolved": True}])}}}
        self.assertEqual(queue.collect(api=api)["ready_prs"][0]["total_review_threads"], 101)

    def test_rejects_search_limit(self):
        with self.assertRaisesRegex(RuntimeError, "1,000"):
            queue.collect(api=lambda query, variables: response([], count=1001))

    def test_invalid_pagination_fails_closed(self):
        for cursor in (None, "repeated"):
            with self.subTest(cursor=cursor):
                seen = {"repeated"}
                with self.assertRaises(RuntimeError):
                    queue.next_cursor({"hasNextPage": True, "endCursor": cursor}, seen)

    def test_empty_queue_is_success_not_error(self):
        report = collect([])
        self.assertEqual(report["ready_count"], 0)
        self.assertIn("No matching PRs", queue.markdown(report))

    def test_output_formats(self):
        report = collect([pr()])
        self.assertEqual(json.loads(json.dumps(report))["schema_version"], 1)
        rendered = queue.markdown(report)
        self.assertIn("| https://github.com/Azure/azure-sdk-for-net/pull/1 |", rendered)
        self.assertNotIn("[#1](", rendered)
        self.assertIn("not merge approval", rendered)
        self.assertIn("No threads", rendered)

    def test_api_failure_has_nonzero_exit_and_no_report(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(queue, "collect", side_effect=RuntimeError("failed page 2")):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = queue.main(["--format", "json"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("failed page 2", stderr.getvalue())

    def test_graphql_errors_reject_even_partial_data(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({"data": {"search": {}}, "errors": ["denied"]}), "")
        with patch.object(queue.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "errors"):
                queue.graphql("query", {})

    def test_missing_cli_and_timeout_fail_closed(self):
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired("gh", 90)):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(queue.subprocess, "run", side_effect=failure):
                    with self.assertRaises(RuntimeError):
                        queue.graphql("query", {})

    def test_auth_or_network_failure_is_not_empty_queue(self):
        result = subprocess.CompletedProcess([], 1, "", "authentication failed")
        with patch.object(queue.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "authentication failed"):
                queue.graphql("query", {})

    def test_late_api_failure_does_not_return_partial_queue(self):
        def api(query, variables):
            if variables["after"] is None:
                return response([pr()], "next", 2)
            raise RuntimeError("network unavailable")
        with self.assertRaisesRegex(RuntimeError, "network unavailable"):
            queue.collect(api=api)

    def test_output_replaces_previous_report_without_opening_it_for_write(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "latest.md"
            output.write_text("old complete list", encoding="utf-8")
            direct_writes = []
            original_open = Path.open
            def observe_open(path, mode="r", *args, **kwargs):
                if path == output and any(flag in mode for flag in "wa+"):
                    direct_writes.append(mode)
                return original_open(path, mode, *args, **kwargs)
            report = collect([])
            with patch.object(queue, "collect", return_value=report):
                with patch.object(Path, "open", observe_open):
                    code = queue.main(["--output", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(output.read_text(encoding="utf-8"), queue.markdown(report))
            self.assertEqual(direct_writes, [], "The previous report must not be truncated in place")
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_failed_atomic_replace_preserves_last_report_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "latest.md"
            output.write_text("old complete list", encoding="utf-8")
            with patch.object(queue, "collect", return_value=collect([])):
                with patch.object(queue.os, "replace", side_effect=OSError("replace denied")):
                    with contextlib.redirect_stderr(io.StringIO()):
                        code = queue.main(["--output", str(output)])
            self.assertEqual(code, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "old complete list")
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_api_failure_preserves_last_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "latest.md"
            output.write_text("old complete list", encoding="utf-8")
            with patch.object(queue, "collect", side_effect=RuntimeError("API unavailable")):
                with contextlib.redirect_stderr(io.StringIO()):
                    code = queue.main(["--output", str(output)])
            self.assertEqual(code, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "old complete list")

    def test_input_fixture_is_not_modified(self):
        candidate = pr(reviewThreads=connection([{"isResolved": True}]))
        original = copy.deepcopy(candidate)
        collect([candidate])
        self.assertEqual(candidate, original)


if __name__ == "__main__":
    unittest.main()
