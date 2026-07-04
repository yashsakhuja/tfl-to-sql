#!/usr/bin/env python3
"""sync_to_repo.py — Copy freshly generated SQL into a target git repo (e.g.
the Dataform project), on its own branch, with a real commit and diff — the
"terraform plan before apply" step for a converted flow, and Phase 4's PR
automation from the platform roadmap.

By design this never pushes or opens a PR unless you explicitly ask it to
(--push, --pr) — copying files and making a local commit is reversible;
pushing and opening a PR are visible to other people, so those stay opt-in.

Usage:
    python tools/sync_to_repo.py <generated_dir> --repo /path/to/dataform-repo \\
        --dest definitions/tableau_prep [--branch tfl-sync/my-flow] \\
        [--message "Sync my_flow.tfl"] [--push] [--pr]

--pr requires the `gh` CLI to be installed and authenticated, and --push.
Without --push, the script stops after a local commit and prints the branch
name plus the exact `git push` / `gh pr create` commands to run by hand.
"""

import argparse
import shutil
import subprocess
from pathlib import Path


def run(cmd, cwd, check=True):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"ERROR: `{' '.join(cmd)}` failed:\n{result.stderr}")
    return result


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def has_gh_cli() -> bool:
    return shutil.which("gh") is not None


def sync_files(generated_dir: Path, dest_dir: Path) -> list:
    """Copy every file from generated_dir into dest_dir. Returns the list of
    filenames copied. Never deletes anything already in dest_dir — a repo
    directory may hold hand-maintained files this tool doesn't own."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for src in sorted(generated_dir.iterdir()):
        if src.is_file():
            shutil.copy2(src, dest_dir / src.name)
            copied.append(src.name)
    return copied


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("generated_dir", help="Folder of generated .sql/.sqlx files (tfl_to_sql.py --out)")
    parser.add_argument("--repo", required=True, help="Path to the target git repository")
    parser.add_argument("--dest", required=True, help="Destination subfolder inside --repo")
    parser.add_argument("--branch", help="Branch name (default: tfl-sync/<generated_dir name>)")
    parser.add_argument("--base", default=None, help="Base branch to branch from (default: current branch)")
    parser.add_argument("--message", default=None, help="Commit message (default: auto-generated)")
    parser.add_argument("--push", action="store_true", help="Push the branch after committing")
    parser.add_argument("--pr", action="store_true", help="Open a PR with `gh` after pushing (implies --push)")
    args = parser.parse_args()

    generated_dir = Path(args.generated_dir)
    if not generated_dir.is_dir():
        raise SystemExit(f"ERROR: Not a directory: {args.generated_dir}")

    repo = Path(args.repo).resolve()
    if not is_git_repo(repo):
        raise SystemExit(f"ERROR: {repo} is not a git repository (no .git found).")

    if args.pr and not args.push:
        args.push = True
    if args.pr and not has_gh_cli():
        raise SystemExit("ERROR: --pr requires the `gh` CLI. Install it or drop --pr and push/PR manually.")

    branch = args.branch or f"tfl-sync/{generated_dir.name}"
    dest_dir = repo / args.dest

    status = run(["git", "status", "--porcelain"], cwd=repo).stdout
    if status.strip():
        raise SystemExit(
            "ERROR: the target repo has uncommitted changes — commit or stash them first "
            "so this sync's diff isn't mixed up with unrelated work."
        )

    start_branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo).stdout.strip()
    base = args.base or start_branch

    if args.base:
        run(["git", "checkout", args.base], cwd=repo)
    run(["git", "checkout", "-B", branch], cwd=repo)

    copied = sync_files(generated_dir, dest_dir)
    if not copied:
        run(["git", "checkout", start_branch], cwd=repo)
        raise SystemExit(f"ERROR: no files found in {generated_dir}, nothing to sync.")

    run(["git", "add", str(Path(args.dest) / "*")], cwd=repo, check=False)
    run(["git", "add", args.dest], cwd=repo)

    diff = run(["git", "diff", "--cached", "--stat"], cwd=repo).stdout
    if not diff.strip():
        run(["git", "checkout", start_branch], cwd=repo)
        run(["git", "branch", "-D", branch], cwd=repo, check=False)
        print("Nothing changed — generated output is identical to what's already committed.")
        return

    print(f"Changes to be committed on branch '{branch}':\n{diff}")

    message = args.message or f"Sync generated SQL from {generated_dir.name} ({len(copied)} file(s))"
    run(["git", "commit", "-m", message], cwd=repo)
    print(f"Committed to local branch '{branch}' (based on '{base}').")

    if not args.push:
        print(
            "\nNot pushed (pass --push to push it). To finish by hand:\n"
            f"  git -C {repo} push -u origin {branch}\n"
            f"  gh pr create --repo <owner/repo> --base {base} --head {branch} "
            f'--title "{message}"'
        )
        return

    push_result = run(["git", "push", "-u", "origin", branch], cwd=repo, check=False)
    if push_result.returncode != 0:
        raise SystemExit(
            f"ERROR: push failed (no remote configured, or no push access):\n{push_result.stderr}\n"
            "The commit is still there locally on branch " + branch
        )
    print(f"Pushed '{branch}' to origin.")

    if args.pr:
        pr_result = run(
            ["gh", "pr", "create", "--base", base, "--head", branch, "--title", message,
             "--body", f"Auto-generated by tools/sync_to_repo.py from `{generated_dir}`."],
            cwd=repo, check=False,
        )
        if pr_result.returncode != 0:
            raise SystemExit(f"ERROR: `gh pr create` failed:\n{pr_result.stderr}")
        print(pr_result.stdout.strip())


if __name__ == "__main__":
    main()
