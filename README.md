# Management AutoPR review queue

[![Daily refresh](https://github.com/live1206/mgmt-review-queue/actions/workflows/refresh.yml/badge.svg)](https://github.com/live1206/mgmt-review-queue/actions/workflows/refresh.yml)

**[View the latest review queue](reports/latest.md)**

Read-only discovery of management AutoPRs in
[Azure/azure-sdk-for-net](https://github.com/Azure/azure-sdk-for-net). The report is
refreshed daily by GitHub Actions. Bookmark or share the link below with teammates:

**https://github.com/live1206/mgmt-review-queue/blob/main/reports/latest.md**

Nothing is posted to the source PRs or Teams. This repository is a review queue,
not merge approval, and does not require an LLM, webhook, or paid service.

## Daily refresh

The [refresh workflow](.github/workflows/refresh.yml) runs on standard
`ubuntu-latest` GitHub-hosted runners, which are free for public repositories:

- Scheduled daily at **08:08 Beijing time / UTC+08:00** (`8 0 * * *` in UTC).
  Scheduled runs can be delayed by GitHub; this is not an exact-time guarantee.
- Can also be started from **Actions → Refresh review queue → Run workflow**.
- Runs the tests, collects the complete report, then commits only
  `reports/latest.md` to the default branch.
- Retries a failed collection up to three total attempts. Collection and
  publication failures leave the last committed complete report available.
- Serializes refresh runs and never force-pushes. A concurrent branch update can
  reject publication rather than overwrite someone else's changes.

**Check the report's UTC `Checked` timestamp and the workflow status for staleness.**
Git history retains previous committed reports; `reports/latest.md` is the stable
link to the latest published result. GitHub may disable scheduled workflows in a
public repository after 60 days without repository activity.

The workflow uses the short-lived built-in `GITHUB_TOKEN` to read public PR data
and publish to this repository. No personal access token is committed or required
by the workflow configuration. The token has write access only to this report
repository, not the Azure SDK repository. Workflow permissions and branch rules
must allow its report commit. Manual runs must select the default branch.

## Selection

All three groups require:

- An open, non-draft PR whose title starts with `[AutoPR Azure.ResourceManager`.
- The latest commit matches the current head SHA.
- The exact current-head **`net - pullrequest`** check is `COMPLETED` with
  conclusion `SUCCESS`. A legacy status context with that exact name qualifies
  only with state `SUCCESS`. If multiple current-head contexts have that exact
  name, all must pass.

Missing, pending, failed, cancelled, skipped, or neutral required checks do not
qualify. **All other CI checks are optional**, including the automated management
SDK review. Aggregate CI status and CLEAN review signals do not gate any group.
GitHub's aggregate review decision separates PRs needing final review from those
whose review is complete.

The groups are **exclusive**, each sorted oldest first:

1. **CI passed + unresolved review threads:** at least one unresolved inline
   review thread, including outdated threads, regardless of approval.
2. **CI passed + no unresolved review threads:** every inline thread is resolved,
   or there are no threads at all, and the review decision is not `APPROVED`.
   Null, `REVIEW_REQUIRED`, and `CHANGES_REQUESTED` decisions remain here.
3. **Ready to merge:** the review decision is `APPROVED` and there are no
   unresolved inline review threads. Approved PRs with no threads qualify too.

Each qualifying PR appears exactly once. Issue-level/general PR comments have no
resolved flag and do not count as inline review threads. All sections show
actual resolved/unresolved thread counts.

Search, current-head CI contexts, and review threads are fully paginated. Search
pages contain up to 25 PRs to reduce nested query load and timeouts. API errors,
invalid pagination, and GitHub's 1,000-result search limit fail closed rather than
publish partial results. The APIs are not an atomic snapshot: PRs can change
during or after collection, and GitHub search can lag.

The script does not inspect CI logs, validate the fixes behind resolved threads,
regenerate SDKs, approve PRs, or determine mergeability. **Ready to merge** means
review is complete under the criteria above, not guaranteed mergeability: branch
rules, conflicts, and other merge restrictions are not evaluated.

## Run locally

Requires Python 3.10+, Git, and an authenticated GitHub CLI (`gh`). There are no
third-party Python dependencies.

```bash
git clone https://github.com/live1206/mgmt-review-queue.git
cd mgmt-review-queue
gh auth login --hostname github.com

python3 ready_for_review.py
python3 ready_for_review.py --format json
mkdir -p reports
python3 ready_for_review.py --output reports/latest.md
```

Optional: `--repo owner/repository` overrides the source repository. Default output
is Markdown on stdout; diagnostics go to stderr. Exit 0 means the scan succeeded,
including an empty queue. Exit 1 means discovery/output failed.

`--output` atomically replaces the file only after complete collection succeeds.
API errors and failed replacement preserve the previous file; the parent output
directory must already exist. Local runs do not commit or push anything.

### JSON output

JSON includes a UTC timestamp, criteria, head SHAs, PR URLs, and thread counts:

- `ci_passed_prs` / `ci_passed_count` / `ci_passed_criteria`: **group 1 only**, with
  at least one unresolved thread.
- `ready_prs` / `ready_count` / `criteria`: **group 2 only**, with zero unresolved
  threads and a review decision other than `APPROVED`.
- `merge_ready_prs` / `merge_ready_count` / `merge_ready_criteria`: **group 3 only**,
  with zero unresolved threads and an `APPROVED` review decision.
- Add all three counts for all qualifying PRs. The lists never overlap.
- `ci_state` describes the required check, not the aggregate CI rollup;
  `required_ci_check` identifies `net - pullrequest`.

JSON schema version remains 1. The `merge_ready_*` fields are additive, but
approved PRs without unresolved threads now move from `ready_prs` to
`merge_ready_prs`. Consumers must account for all three exclusive groups.

## Tests

```bash
python3 -m unittest -v
```

Tests use mocked API responses and temporary local Git repositories; no tokens or
network access are needed. Coverage includes exact required-CI matching, legacy
status contexts, optional failures, pagination, stale heads, outdated threads,
three-way exclusive membership, approval/thread combinations, sorting/deduplication,
API/authentication failures, atomic replacement, hosted refresh retries, and
publishing only the report file.

Regressions retain #63238's failed optional SDK-review check, #63545's null review
decision with an unresolved thread, #63702's no-thread/group-2-only case, and
#63960's approved/resolved-thread/group-3-only case.
