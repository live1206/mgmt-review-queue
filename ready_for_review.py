#!/usr/bin/env python3
"""Read-only management AutoPR final-review queue; requires Python 3.10+ and gh."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

DEFAULT_REPO = "Azure/azure-sdk-for-net"
TITLE_PREFIX = "[AutoPR Azure.ResourceManager"
REQUIRED_CI_CHECK = "net - pullrequest"
CHECK_CONTEXTS = """
contexts(first: 100, after: $checksAfter) {
  pageInfo { hasNextPage endCursor }
  nodes {
    __typename
    ... on CheckRun { name status conclusion }
    ... on StatusContext { context state }
  }
}
"""
SEARCH_QUERY = """
query($query: String!, $after: String, $checksAfter: String) {
  search(query: $query, type: ISSUE, first: 25, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title url createdAt state isDraft headRefOid reviewDecision
        commits(last: 1) {
          nodes { commit { oid statusCheckRollup { CHECK_CONTEXTS } } }
        }
        reviewThreads(first: 100) {
          pageInfo { hasNextPage endCursor }
          nodes { isResolved }
        }
      }
    }
  }
}
""".replace("CHECK_CONTEXTS", CHECK_CONTEXTS)
CHECKS_QUERY = """
query($owner: String!, $name: String!, $oid: String!, $checksAfter: String) {
  repository(owner: $owner, name: $name) {
    object(expression: $oid) {
      ... on Commit {
        oid
        statusCheckRollup { CHECK_CONTEXTS }
      }
    }
  }
}
""".replace("CHECK_CONTEXTS", CHECK_CONTEXTS)
THREAD_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved }
      }
    }
  }
}
"""


def graphql(query, variables):
    """Use gh's existing authentication; never return partial GraphQL data."""
    env = dict(os.environ, GH_PROMPT_DISABLED="1")
    try:
        result = subprocess.run(
            ["gh", "api", "--hostname", "github.com", "graphql", "--input", "-"],
            input=json.dumps({"query": query, "variables": variables}),
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("GitHub CLI is missing; install gh and run gh auth login.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("GitHub API timed out; no queue report was produced.") from exc
    if result.returncode:
        raise RuntimeError(f"GitHub API failed: {result.stderr.strip() or result.stdout.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub API returned invalid JSON.") from exc
    if payload.get("errors") or not payload.get("data"):
        raise RuntimeError(f"GitHub API returned errors or no data: {payload.get('errors')}")
    return payload["data"]


def next_cursor(page, seen):
    if not page["hasNextPage"]:
        return None
    cursor = page["endCursor"]
    if not cursor or cursor in seen:
        raise RuntimeError("Invalid API pagination; refusing to produce an incomplete queue.")
    seen.add(cursor)
    return cursor


def required_ci_passed(commit, repo, api):
    rollup = commit["statusCheckRollup"]
    if not rollup:
        return False
    contexts = rollup["contexts"]
    matches = []
    seen = set()
    while True:
        for context in contexts["nodes"]:
            if context["__typename"] == "CheckRun" and context["name"] == REQUIRED_CI_CHECK:
                matches.append(context["status"] == "COMPLETED" and context["conclusion"] == "SUCCESS")
            elif context["__typename"] == "StatusContext" and context["context"] == REQUIRED_CI_CHECK:
                matches.append(context["state"] == "SUCCESS")
        cursor = next_cursor(contexts["pageInfo"], seen)
        if cursor is None:
            return bool(matches) and all(matches)
        owner, name = repo.split("/")
        page = api(CHECKS_QUERY, {
            "owner": owner, "name": name, "oid": commit["oid"], "checksAfter": cursor,
        })["repository"]["object"]
        if not page or page["oid"] != commit["oid"] or not page["statusCheckRollup"]:
            raise RuntimeError("Unable to retrieve CI checks for the current head commit.")
        contexts = page["statusCheckRollup"]["contexts"]


def all_threads(pr, repo, api):
    connection = pr["reviewThreads"]
    threads = list(connection["nodes"])
    seen = set()
    owner, name = repo.split("/")
    while (cursor := next_cursor(connection["pageInfo"], seen)) is not None:
        connection = api(THREAD_QUERY, {
            "owner": owner, "name": name, "number": pr["number"], "after": cursor,
        })["repository"]["pullRequest"]["reviewThreads"]
        threads.extend(connection["nodes"])
    return threads


def collect(repo=DEFAULT_REPO, api=graphql):
    # GitHub API search uses different phrase matching from the web UI. Search
    # broadly for AutoPR and enforce the exact management title prefix locally.
    search = (
        f"repo:{repo} is:pr is:open AutoPR in:title draft:false "
        "sort:created-asc"
    )
    after = None
    seen_cursors = set()
    seen_prs = set()
    management_count = 0
    ci_passed = []
    while True:
        page = api(SEARCH_QUERY, {"query": search, "after": after})["search"]
        if page["issueCount"] > 1000:
            raise RuntimeError("Search exceeds GitHub's 1,000-result limit; narrow the query first.")
        for pr in page["nodes"]:
            if not pr or not pr.get("number"):
                raise RuntimeError("Search returned an incomplete pull request.")
            if pr["number"] in seen_prs:
                continue
            seen_prs.add(pr["number"])
            if not pr["title"].startswith(TITLE_PREFIX):
                continue
            management_count += 1
            # Recheck mutable fields rather than relying solely on the search index.
            if pr["state"] != "OPEN" or pr["isDraft"]:
                continue
            commits = pr["commits"]["nodes"]
            commit = commits[-1]["commit"] if commits else None
            if not commit or commit["oid"] != pr["headRefOid"]:
                continue
            if not required_ci_passed(commit, repo, api):
                continue
            threads = all_threads(pr, repo, api)
            # Outdated threads still count: only explicit resolution qualifies.
            unresolved = sum(thread["isResolved"] is not True for thread in threads)
            package = pr["title"].split("]", 1)[0].removeprefix("[AutoPR ")
            ci_passed.append({
                "number": pr["number"], "title": pr["title"], "package": package,
                "url": pr["url"], "created_at": pr["createdAt"],
                "head_sha": pr["headRefOid"], "ci_state": "SUCCESS",
                "required_ci_check": REQUIRED_CI_CHECK,
                "review_decision": pr["reviewDecision"],
                "total_review_threads": len(threads),
                "unresolved_review_threads": unresolved,
            })
        after = next_cursor(page["pageInfo"], seen_cursors)
        if after is None:
            break
    ci_passed.sort(key=lambda pr: (pr["created_at"], pr["number"]))
    ready = [pr for pr in ci_passed if pr["unresolved_review_threads"] == 0]
    ci_passed = [pr for pr in ci_passed if pr["unresolved_review_threads"] > 0]
    common_criteria = {
        "title_prefix": TITLE_PREFIX, "required_ci_check": REQUIRED_CI_CHECK,
        "required_ci_state": "SUCCESS",
        "include_prs_without_threads": True,
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repository": repo, "search_query": search,
        "criteria": {**common_criteria, "unresolved_review_threads": 0},
        "ci_passed_criteria": {
            **common_criteria, "minimum_unresolved_review_threads": 1,
            "include_prs_without_threads": False,
        },
        "search_results_scanned": len(seen_prs),
        "management_candidates_scanned": management_count,
        "ci_passed_count": len(ci_passed), "ci_passed_prs": ci_passed,
        "ready_count": len(ready), "ready_prs": ready,
    }


def markdown(report):
    lines = [
        "## Management AutoPR review queues",
        "", f"Checked: {report['generated_at']} · {report['repository']}", "",
        "Groups are exclusive; each qualifying PR appears in exactly one section.",
    ]
    groups = (
        (f"### 1. {REQUIRED_CI_CHECK} passed + unresolved review threads ({report['ci_passed_count']})", report["ci_passed_prs"],
         f"No matching PRs currently have passing {REQUIRED_CI_CHECK} CI and unresolved review threads."),
        (f"### 2. {REQUIRED_CI_CHECK} passed + no unresolved review threads ({report['ready_count']})", report["ready_prs"],
         "No matching PRs are currently ready for final review."),
    )
    for heading, prs, empty_message in groups:
        lines += ["", heading, ""]
        if not prs:
            lines.append(empty_message)
            continue
        lines += ["| PR | Package | Review threads |", "| --- | --- | --- |"]
        for pr in prs:
            package = pr["package"].replace("|", "\\|").replace("\n", " ")
            count = pr["total_review_threads"]
            unresolved = pr["unresolved_review_threads"]
            threads = f"{count - unresolved} resolved, {unresolved} unresolved" if count else "No threads"
            lines.append(f"| {pr['url']} | {package} | {threads} |")
    lines += [
        "", "Both groups: open, non-draft management AutoPR; "
        f"the current head's exact `{REQUIRED_CI_CHECK}` check must complete successfully.",
        "", "Group 1 requires at least one unresolved review thread. "
        "Group 2 requires no unresolved review threads (including outdated threads); "
        "PRs with no threads qualify for group 2 only.",
        "", "Other CI checks are optional and do not affect either group. "
        "A missing, pending, failed, skipped, or neutral required check does not qualify. "
        "This is a review queue, not merge approval. "
        "GitHub search may lag, and PRs can change after this snapshot.",
    ]
    return "\n".join(lines) + "\n"


def write_report(path, output):
    """Replace the complete report atomically, preserving the old one on failure."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(output)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub owner/repository")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path, help="Write report to this file instead of stdout")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("--repo must be owner/repository")
    try:
        report = collect(args.repo)
        output = json.dumps(report, indent=2) + "\n" if args.format == "json" else markdown(report)
        if args.output:
            write_report(args.output, output)
        else:
            print(output, end="")
    except (RuntimeError, KeyError, TypeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
