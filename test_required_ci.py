"""Only the exact current-head net - pullrequest check gates either queue."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ready_for_review as queue
from test_ready_for_review import collect, connection, pr, response


REQUIRED_CHECK = "net - pullrequest"


def check(name=REQUIRED_CHECK, conclusion="SUCCESS", status="COMPLETED"):
    return {"__typename": "CheckRun", "name": name,
            "status": status, "conclusion": conclusion}


def candidate_with_checks(checks, aggregate="FAILURE", cursor=None, **kwargs):
    candidate = pr(**kwargs)
    candidate["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = {
        "state": aggregate, "contexts": connection(checks, cursor),
    }
    return candidate


class RequiredCITests(unittest.TestCase):
    def test_pr63238_optional_review_failure_does_not_block_either_group(self):
        candidate = candidate_with_checks([
            check("Azure .NET Management SDK PR Review", "FAILURE"),
            check(), check("Close stale generator upgrade PRs", "SKIPPED"),
        ], number=63238)
        head = "8a8ac71b45670a1cd8119d3873e8f8fe90ff3fac"
        candidate["headRefOid"] = candidate["commits"]["nodes"][0]["commit"]["oid"] = head
        for unresolved in (False, True):
            with self.subTest(unresolved=unresolved):
                candidate["reviewThreads"] = connection([{"isResolved": not unresolved}])
                report = collect([candidate])
                self.assertEqual(report["ci_passed_count"], int(unresolved))
                self.assertEqual(report["ready_count"], int(not unresolved))
                eligible = report["ci_passed_prs"] + report["ready_prs"]
                self.assertEqual(len(eligible), 1)
                self.assertEqual(eligible[0]["number"], 63238)
                self.assertEqual(eligible[0]["ci_state"], "SUCCESS")

    def test_search_does_not_hide_optional_failures_or_pending_checks(self):
        searches = []
        def api(query, variables):
            searches.append(variables["query"])
            return response([])
        queue.collect(api=api)
        self.assertNotIn("status:", searches[0])

    def test_optional_check_states_do_not_gate(self):
        for aggregate in ("FAILURE", "PENDING", "ERROR", "EXPECTED", "SUCCESS"):
            with self.subTest(aggregate=aggregate):
                candidate = candidate_with_checks([
                    check(), check("optional", None, "IN_PROGRESS"),
                ], aggregate=aggregate)
                self.assertEqual(collect([candidate])["ready_count"], 1)

    def test_required_check_must_complete_with_success(self):
        for conclusion in ("FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED",
                           "SKIPPED", "NEUTRAL", "STALE", None):
            with self.subTest(conclusion=conclusion):
                candidate = candidate_with_checks([check(conclusion=conclusion)], aggregate="SUCCESS")
                report = collect([candidate])
                self.assertEqual(report["ci_passed_count"] + report["ready_count"], 0)
        for status in ("QUEUED", "IN_PROGRESS", "PENDING", None):
            with self.subTest(status=status):
                candidate = candidate_with_checks([check(status=status)], aggregate="SUCCESS")
                report = collect([candidate])
                self.assertEqual(report["ci_passed_count"] + report["ready_count"], 0)

    def test_missing_or_only_similarly_named_checks_do_not_qualify(self):
        for checks in ([], [check("optional")], [check("net - pullrequest (Build Analyze PRBatch)")],
                       [check("net - pullrequest-extra")], [check("NET - pullrequest")]):
            with self.subTest(checks=checks):
                self.assertEqual(collect([candidate_with_checks(checks, aggregate="SUCCESS")])["ready_count"], 0)

    def test_legacy_status_context_requires_success(self):
        for state in ("SUCCESS", "FAILURE", "ERROR", "PENDING", "EXPECTED", None):
            with self.subTest(state=state):
                context = {"__typename": "StatusContext", "context": REQUIRED_CHECK, "state": state}
                report = collect([candidate_with_checks([context])])
                self.assertEqual(report["ready_count"], int(state == "SUCCESS"))

    def test_duplicate_required_checks_must_all_pass(self):
        for second in (check(), check(conclusion="FAILURE"), check(status="IN_PROGRESS")):
            with self.subTest(second=second):
                report = collect([candidate_with_checks([check(), second])])
                self.assertEqual(report["ready_count"], int(second == check()))

    def test_required_check_on_later_context_page_is_used(self):
        candidate = candidate_with_checks([check("optional", "FAILURE")], cursor="checks101")
        calls = []
        def api(query, variables):
            calls.append((query, variables))
            if query == queue.SEARCH_QUERY:
                return response([candidate])
            self.assertEqual(variables["oid"], "abc")
            self.assertEqual(variables["checksAfter"], "checks101")
            return {"repository": {"object": {"oid": "abc", "statusCheckRollup": {
                "contexts": connection([check()]),
            }}}}
        report = queue.collect(api=api)
        self.assertEqual(report["ready_count"], 1)
        self.assertEqual(len(calls), 2)

    def test_failed_required_check_on_later_page_is_not_ignored(self):
        candidate = candidate_with_checks([check()], cursor="checks101")
        def api(query, variables):
            if query == queue.SEARCH_QUERY:
                return response([candidate])
            return {"repository": {"object": {"oid": "abc", "statusCheckRollup": {
                "contexts": connection([check(conclusion="FAILURE")]),
            }}}}
        report = queue.collect(api=api)
        self.assertEqual(report["ci_passed_count"] + report["ready_count"], 0)

    def test_check_pagination_failure_preserves_previous_report(self):
        candidate = candidate_with_checks([check()], cursor="checks101")
        def api(query, variables):
            if query == queue.SEARCH_QUERY:
                return response([candidate])
            raise RuntimeError("checks page unavailable")
        original_collect = queue.collect
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "latest.md"
            output.write_text("old complete report", encoding="utf-8")
            with patch.object(queue, "collect", side_effect=lambda repo: original_collect(repo, api=api)):
                with contextlib.redirect_stderr(io.StringIO()):
                    code = queue.main(["--output", str(output)])
            self.assertEqual(code, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "old complete report")

    def test_stale_head_is_not_accepted_even_with_passing_required_check(self):
        candidate = candidate_with_checks([check()], aggregate="SUCCESS", headRefOid="new-head")
        report = collect([candidate])
        self.assertEqual(report["ci_passed_count"] + report["ready_count"], 0)

    def test_report_explains_required_ci_and_optional_checks(self):
        report = collect([candidate_with_checks([check()], aggregate="SUCCESS")])
        for field in ("criteria", "ci_passed_criteria"):
            self.assertEqual(report[field]["required_ci_check"], REQUIRED_CHECK)
            self.assertEqual(report[field]["required_ci_state"], "SUCCESS")
            self.assertNotIn("head_ci_rollup", report[field])
            self.assertNotIn("search_ci_status", report[field])
        rendered = queue.markdown(report)
        self.assertIn("net - pullrequest passed", rendered)
        self.assertIn("Other CI checks are optional", rendered)
        self.assertNotIn("All CI passed", rendered)


if __name__ == "__main__":
    unittest.main()
