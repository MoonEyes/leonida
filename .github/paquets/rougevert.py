#!/usr/bin/env python3
"""Red/green check of ProjectLeonida/mandy pull requests, one by one, read-only.

For each PR: red = main with the PR's added or changed files under tests/ applied (and
its deleted ones removed): the PR's test files must fail on an assertion. Green = main
merged into the PR head (merge, never rebase): the same test files must pass. Each test
run happens in an isolated container (see commun.py). Nothing is pushed, commented or
merged on GitHub. The report is rewritten after every PR.

Usage: rougevert.py --name L1 --prs "1 2 3" --states states.json --out OUTDIR
"""
import argparse
import os
import subprocess
import time

import commun as c


def run_files(tests):
    """Returns {file: (rc, output)} for the test files in the current work tree."""
    res = {}
    for f in tests:
        full = os.path.join(c.REPO, f)
        if not os.path.exists(full):
            res[f] = ("missing", "")
            continue
        src = open(full, encoding="utf-8", errors="replace").read()
        rc, out = c.in_container(c.test_command(f, src), 300)
        res[f] = (rc, out)
    return res


def summary(res):
    parts = []
    for f, (rc, out) in res.items():
        tail = [l for l in out.strip().splitlines() if l.strip()][-1:] or [""]
        parts.append(f"{os.path.basename(f)}: rc {rc} ({tail[0][:70]})")
    return c.md("; ".join(parts))


def check_pr(n, base, head):
    diff = c.git("diff", "--name-status", "--no-renames", f"{base}...pr{n}").stdout.splitlines()
    changes = [(l.split("\t")[0], l.split("\t")[-1]) for l in diff if "\t" in l]
    files = [f for _, f in changes]
    row = {"pr": n, "head": head, "files": len(files)}
    if any(f.startswith("runtime/") for f in files):
        row["verdict"] = "C++"
        return row
    tests = [f for s, f in changes if s != "D" and f.startswith("tests/") and f.endswith(".py")
             and os.path.basename(f).startswith("test_") and f not in c.EXCLUDED]
    if not tests:
        docs = all(f.endswith(".md") or f.startswith("docs/") for f in files)
        row["verdict"] = "DOCS-SEULES" if docs else "SANS-TEST"
        return row
    for f in tests:
        src = c.run([*c.GIT, "show", f"pr{n}:{f}"], cwd=c.REPO, check=False).stdout
        if c.RISKY.search(src):
            row.update(verdict="SKIPPED-UNSAFE", why=f"{f} builds or downloads")
            return row
    # Red: main plus the PR's side of tests/.
    c.switch(base)
    for s, f in changes:
        if not f.startswith("tests/"):
            continue
        path = os.path.join(c.REPO, f)
        if s == "D":
            if os.path.exists(path):
                os.remove(path)
            continue
        if subprocess.run([*c.GIT, "cat-file", "-e", f"pr{n}:{f}"], cwd=c.REPO).returncode != 0:
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        blob = subprocess.run([*c.GIT, "show", f"pr{n}:{f}"], cwd=c.REPO, capture_output=True).stdout
        with open(path, "wb") as fh:
            fh.write(blob)
    red = run_files(tests)
    # Green: the PR head with main merged in.
    c.switch(f"pr{n}", f"g{n}")
    if c.git("merge", "--no-ff", "--no-edit", "-q", base, check=False).returncode != 0:
        c.git("merge", "--abort", check=False)
        row["verdict"] = "CONFLIT"
        return row
    green = run_files(tests)
    c.switch(base)
    row.update(command=" ; ".join(tests), red=summary(red), green=summary(green))
    green_ok = all(rc == 0 for rc, _ in green.values())
    failing_red = {f: out for f, (rc, out) in red.items() if rc != 0}
    if not green_ok:
        row["verdict"] = "ÉCHEC-PR"
    elif not failing_red:
        row["verdict"] = "PAS-DE-ROUGE"
    elif all(c.is_assertion_red(out) for out in failing_red.values()):
        row["verdict"] = "PRÊTE"
    else:
        row.update(verdict="À-CONFIRMER", why="red is an error (import or other), not an assertion")
    return row


def write(a, report, t0):
    report["minutes"] = round((time.time() - t0) / 60, 1)
    lines = [f"## Lot {a.name} · main {report.get('main', '?')} · {report['minutes']} min", ""]
    for r in report["rows"]:
        lines.append(f"#{r['pr']} | {r.get('head', '')} | {c.md(r.get('command', ''))} | rouge: {r.get('red', '')} | "
                     f"vert: {r.get('green', '')} | {r['verdict']}" + (f" ({c.md(r['why'])})" if r.get("why") else ""))
    if report.get("note"):
        lines.append(c.md(report["note"]))
    c.write_report(a.out, a.name, report, lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--prs", required=True)
    ap.add_argument("--states", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    t0 = time.time()
    report = {"name": a.name, "rows": []}
    try:
        states = c.load_states(a.states)
        base = c.clone()
        report["main"] = base
        for n in [int(x) for x in a.prs.split()]:
            head, why = c.fetch_pr(n, states)
            if why:
                report["rows"].append({"pr": n, "verdict": why.upper()})
            else:
                try:
                    report["rows"].append(check_pr(n, base, head))
                except c.Budget:
                    raise
                except Exception as e:  # keep going with the next PR
                    report["rows"].append({"pr": n, "head": head, "verdict": "ERREUR", "why": str(e)[:300]})
                    c.git("merge", "--abort", check=False)
                    c.switch(base)
            write(a, report, t0)
    except c.Budget:
        report["note"] = "time budget reached, the remaining PRs were not checked"
    except Exception as e:
        report["note"] = f"stopped: {str(e)[:300]}"
    finally:
        write(a, report, t0)


if __name__ == "__main__":
    main()
