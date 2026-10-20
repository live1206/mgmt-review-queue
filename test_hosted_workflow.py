"""Exercise the hosted workflow's actual shell steps without network access."""

import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


WORKFLOW = Path(__file__).parent / ".github" / "workflows" / "refresh.yml"


def step_script(name):
    block = WORKFLOW.read_text(encoding="utf-8").split(f"      - name: {name}\n", 1)[1]
    block = block.split("\n      - ", 1)[0]
    return textwrap.dedent(block.split("        run: |\n", 1)[1])


def execute(script, directory, **env):
    return subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        cwd=directory, env={**os.environ, **env}, capture_output=True, text=True,
        timeout=15,
    )


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args], text=True).strip()


class HostedWorkflowTests(unittest.TestCase):
    def refresh(self, failures):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            report = root / "reports" / "latest.md"
            report.write_text("old complete report", encoding="utf-8")
            mocks = root / "bin"
            mocks.mkdir()
            python = mocks / "python"
            python.write_text("""#!/bin/bash
set -eu
count=0
if [[ -f attempts ]]; then read -r count < attempts; fi
count=$((count + 1))
printf '%s\\n' "$count" > attempts
[[ "$*" == 'ready_for_review.py --format markdown --output reports/latest.md' ]]
if (( count <= FAILURES )); then exit 1; fi
printf 'new complete report' > reports/latest.md
""", encoding="utf-8")
            sleep = mocks / "sleep"
            sleep.write_text('#!/bin/bash\nprintf "%s\\n" "$1" >> sleeps\n', encoding="utf-8")
            python.chmod(0o755)
            sleep.chmod(0o755)
            result = execute(step_script("Refresh report"), root,
                             PATH=f"{mocks}:{os.environ['PATH']}", FAILURES=str(failures))
            return (result, report.read_text(encoding="utf-8"),
                    (root / "attempts").read_text(encoding="utf-8").strip(),
                    (root / "sleeps").read_text(encoding="utf-8").splitlines()
                    if (root / "sleeps").exists() else [])

    def test_successful_refresh_does_not_retry(self):
        result, report, attempts, sleeps = self.refresh(0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report, "new complete report")
        self.assertEqual(attempts, "1")
        self.assertEqual(sleeps, [])

    def test_transient_failure_retries_the_complete_collection(self):
        result, report, attempts, sleeps = self.refresh(2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report, "new complete report")
        self.assertEqual(attempts, "3")
        self.assertEqual(sleeps, ["10", "20"])

    def test_exhausted_retries_fail_without_replacing_last_report(self):
        result, report, attempts, sleeps = self.refresh(3)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report, "old complete report")
        self.assertEqual(attempts, "3")
        self.assertEqual(sleeps, ["10", "20"])

    def publish(self, change_report):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            working, remote = root / "working", root / "remote.git"
            git(root, "init", "--quiet", "--bare", "--initial-branch=main", str(remote))
            git(root, "init", "--quiet", "--initial-branch=main", str(working))
            git(working, "config", "user.name", "Test")
            git(working, "config", "user.email", "test@example.invalid")
            (working / "reports").mkdir()
            (working / "README.md").write_text("original docs", encoding="utf-8")
            report = working / "reports" / "latest.md"
            report.write_text("old report", encoding="utf-8")
            git(working, "add", "README.md", "reports/latest.md")
            git(working, "commit", "--quiet", "-m", "Initial")
            git(working, "remote", "add", "origin", str(remote))
            git(working, "push", "--quiet", "origin", "main")
            original_head = git(working, "rev-parse", "HEAD")
            (working / "README.md").write_text("unrelated local change", encoding="utf-8")
            (working / "private.txt").write_text("not for publication", encoding="utf-8")
            if change_report:
                report.write_text("new report", encoding="utf-8")
            result = execute(step_script("Publish report"), working, PUBLISH_BRANCH="main")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(git(working, "rev-parse", "HEAD"), git(remote, "rev-parse", "main"))
            self.assertEqual(git(remote, "show", "main:README.md"), "original docs")
            self.assertNotIn("private.txt", git(remote, "ls-tree", "-r", "--name-only", "main"))
            if change_report:
                self.assertEqual(git(remote, "show", "main:reports/latest.md"), "new report")
                self.assertEqual(git(working, "diff", "--name-only", f"{original_head}..HEAD"),
                                 "reports/latest.md")
            else:
                self.assertEqual(git(working, "rev-parse", "HEAD"), original_head)

    def test_publication_commits_only_the_report(self):
        self.publish(change_report=True)

    def test_unchanged_report_does_not_create_a_commit(self):
        self.publish(change_report=False)


if __name__ == "__main__":
    unittest.main()
