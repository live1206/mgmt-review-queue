"""Review decisions split resolved PRs, but never hide unresolved follow-up."""

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

    def test_review_status_only_splits_prs_without_unresolved_threads(self):
        for decision in (None, "REVIEW_REQUIRED", "APPROVED", "CHANGES_REQUESTED"):
            for unresolved in (False, True):
                with self.subTest(decision=decision, unresolved=unresolved):
                    report = collect([pr(
                        reviewDecision=decision,
                        reviewThreads=connection([{"isResolved": not unresolved}]),
                    )])
                    self.assertEqual(report["ci_passed_count"], int(unresolved))
                    self.assertEqual(report["ready_count"], int(not unresolved and decision != "APPROVED"))
                    self.assertEqual(report["merge_ready_count"], int(not unresolved and decision == "APPROVED"))
                    eligible = report["ci_passed_prs"] + report["ready_prs"] + report["merge_ready_prs"]
                    self.assertEqual(len(eligible), 1)
                    self.assertEqual(eligible[0]["review_decision"], decision)

    def test_search_does_not_filter_review_status_and_group1_accepts_any_decision(self):
        queries = []
        def api(query, variables):
            queries.append(variables["query"])
            return response([pr()])
        report = queue.collect(api=api)
        self.assertNotIn("status:", queries[0])
        self.assertNotIn("review:", queries[0])
        self.assertNotIn("required_review_decision", report["ci_passed_criteria"])
        self.assertNotIn("excluded_review_decision", report["ci_passed_criteria"])
        self.assertEqual(report["criteria"]["excluded_review_decision"], "APPROVED")
        self.assertEqual(report["merge_ready_criteria"]["required_review_decision"], "APPROVED")
        self.assertNotIn("review required", queue.markdown(report))


if __name__ == "__main__":
    unittest.main()
