"""Run every `run:` step of the GitHub workflow locally, in order.

Why this exists: a workflow step that expects a non-zero exit code (a demo file
that is *supposed* to have breaking drift) fails the whole job under `bash -e`.
That mistake has cost two full red runs. Running the steps here first catches
it in ten seconds instead of after a push.

    python tools/simulate_ci.py            # every step
    python tools/simulate_ci.py -k Drift   # only steps whose name contains this

Steps marked `if: runner.os == 'Windows'` are skipped unless --windows is given.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def find_bash() -> str:
    """Prefer a real bash over the WSL launcher.

    On Windows, ``bash`` on PATH is often ``C:\\Windows\\System32\\bash.exe``
    (the WSL stub), which is not the shell GitHub uses. Git Bash is.
    """
    override = os.environ.get("GIT_BASH")
    if override and Path(override).exists():
        return override
    for candidate in (
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe",
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/usr/bin/bash.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Git/bin/bash.exe",
    ):
        if candidate.exists():
            return str(candidate)
    found = shutil.which("bash")
    return found or "bash"


def extract_steps(include_windows: bool):
    text = WORKFLOW.read_text(encoding="utf-8")
    chunks = re.split(r"\n(?=      - name:)", text)
    steps = []
    for chunk in chunks:
        match = re.search(r"      - name:\s*(.+)", chunk)
        if not match or "run: |" not in chunk:
            continue
        name = match.group(1).strip()
        windows_only = "runner.os == 'Windows'" in chunk
        if windows_only and not include_windows:
            continue
        body = chunk.split("run: |", 1)[1]
        body = re.split(r"\n\n      - name:", body)[0]
        lines = [line[10:] if line.startswith(" " * 10) else line
                 for line in body.split("\n")]
        steps.append((name, "\n".join(lines).rstrip(), windows_only))
    return steps


def decode(blob: bytes) -> str:
    for encoding in ("utf-8", "gbk", "cp1252"):
        try:
            return blob.decode(encoding)
        except UnicodeDecodeError:
            continue
    return blob.decode("utf-8", "replace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-k", "--filter", default=None,
                        help="只跑名字里包含这段文字的步骤")
    parser.add_argument("--windows", action="store_true",
                        help="也跑标记为 Windows-only 的步骤")
    parser.add_argument("--keep", action="store_true",
                        help="保留临时脚本（出问题时方便看）")
    args = parser.parse_args()

    bash = find_bash()
    steps = extract_steps(args.windows)
    if args.filter:
        steps = [s for s in steps if args.filter.lower() in s[0].lower()]
    if not steps:
        print("没有匹配的步骤", file=sys.stderr)
        return 1

    workdir = ROOT / "_ci_steps"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir()

    print(f"bash: {bash}")
    print(f"simulating {len(steps)} step(s)\n")

    for index, (name, script, _) in enumerate(steps, 1):
        path = workdir / f"{index:02d}.sh"
        path.write_text(script, encoding="utf-8", newline="\n")
        result = subprocess.run([bash, "-e", str(path)], capture_output=True, cwd=ROOT)
        if result.returncode != 0:
            print(f"[{index}/{len(steps)}] FAIL  {name}")
            if result.stdout:
                print("--- stdout ---\n" + decode(result.stdout)[-2000:])
            if result.stderr:
                print("--- stderr ---\n" + decode(result.stderr)[-2000:])
            print(f"\nscript kept at {path}")
            return 1
        print(f"[{index}/{len(steps)}] OK    {name}")

    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    print("\nall steps passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
