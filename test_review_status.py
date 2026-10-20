"""CI/thread groups must not depend on GitHub's aggregate review decision."""

import unittest

import ready_for_review as queue
from test_ready_for_review import collect, connection, pr, response


class ReviewStatusTests(unittest.TestCase):
    def test_pr63545_null_review_decision_with_unresolved_thread_is_ci_only(self):
        candidate = pr(
            63545, title="[AutoPR Azure.ResourceManager.AppService]-generated",
            reviewDecision=None,
            reviewThreads=connection([{"isResolved": True}, {"isResolved": False}]),
        )
        head = "34a2d02025b17fbaf7b51cc7084b4caf1cc9ad34"
        candidate["headRefOid"] = candidate["commits"]["nodes"][0]["commit"]["oid"] = head
        report = collect([candidate])
        self.assertEqual(report["ci_passed_count"], 1)
        self.assertEqual(report["ci_passed_prs"][0]["number"], 63545)
        self.assertIsNone(report["ci_passed_prs"][0]["review_decision"])
        self.assertEqual(report["ci_passed_prs"][0]["unresolved_review_threads"], 1)
        self.assertEqual(report["ready_count"], 0)

    def test_review_status_does_not_gate_either_group(self):
        for decision in (None, "REVIEW_REQUIRED", "APPROVED", "CHANGES_REQUESTED"):
            for unresolved in (False, True):
                with self.subTest(decision=decision, unresolved=unresolved):
                    report = collect([pr(
                        reviewDecision=decision,
                        reviewThreads=connection([{"isResolved": not unresolved}]),
                    )])
                    self.assertEqual(report["ci_passed_count"], int(unresolved))
                    self.assertEqual(report["ready_count"], int(not unresolved))
                    eligible = report["ci_passed_prs"] + report["ready_prs"]
                    self.assertEqual(len(eligible), 1)
                    self.assertEqual(eligible[0]["review_decision"], decision)

    def test_search_criteria_and_markdown_do_not_require_review_status(self):
        queries = []
        def api(query, variables):
            queries.append(variables["query"])
            return response([pr()])
        report = queue.collect(api=api)
        self.assertNotIn("status:", queries[0])
        self.assertNotIn("review:", queries[0])
        for field in ("criteria", "ci_passed_criteria"):
            self.assertNotIn("review_decision", report[field])
        self.assertNotIn("review required", queue.markdown(report))


if __name__ == "__main__":
    unittest.main()
