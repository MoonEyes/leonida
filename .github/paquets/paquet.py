#!/usr/bin/env python3
"""Test a batch of ProjectLeonida/mandy pull requests merged together, against main.

Read-only: nothing is pushed, commented or merged on GitHub. The batch is merged in a
local branch; a targeted test list runs on main and on the batch (each test in an
isolated container, see commun.py); a test that fails on the batch but passes on main is
a new failure, and each PR is then retried alone to find the one that causes it.

Usage: paquet.py --name P1 --prs "1318 1324 ..." --states states.json --out OUTDIR
"""
import argparse
import os
import re
import time

import commun as c


def merge_batch(base, prs, branch):
    c.switch(base, branch)
    kept, dropped = [], {}
    for n in prs:
        if c.git("merge", "--no-ff", "--no-edit", "-q", f"pr{n}", check=False).returncode != 0:
            c.git("merge", "--abort", check=False)
            dropped[n] = "conflict"
        else:
            kept.append(n)
    return kept, dropped


def select_tests(base, head):
    files = [f for f in c.git("diff", "--name-only", f"{base}...{head}").stdout.split() if f]
    tests = {f for f in files if f.startswith("tests/") and os.path.basename(f).startswith("test_")
             and f.endswith(".py")}
    modules = {os.path.splitext(os.path.basename(f))[0] for f in files
               if f.endswith(".py") and not f.startswith("tests/")}
    if modules:
        alt = "|".join(map(re.escape, sorted(modules)))
        pats = [re.compile(r"^\s*import\s+[\w.]*\b(" + alt + r")\b", re.M),
                re.compile(r"^\s*from\s+[\w.]+\s+import\s+[\w, ()]*\b(" + alt + r")\b", re.M),
                re.compile(r"^\s*from\s+[\w.]*\b(" + alt + r")\b", re.M),
                re.compile(r"\b(" + alt + r")\.py\b")]
        for t in sorted(os.listdir(os.path.join(c.REPO, "tests"))):
            if t.startswith("test_") and t.endswith(".py"):
                src = open(os.path.join(c.REPO, "tests", t), encoding="utf-8", errors="replace").read()
                if any(p.search(src) for p in pats):
                    tests.add(f"tests/{t}")
    tests |= {f"tests/{t}" for t in os.listdir(os.path.join(c.REPO, "tests"))
              if t.startswith("test_docs") and t.endswith(".py")}
    semantic = any(f.startswith(("tools/rdna2spirv", "tools/spirv", "tests/semantic/")) for f in files)
    skipped = {}
    for t in sorted(tests):
        if t in c.EXCLUDED:
            skipped[t] = "excluded (native build)"
        elif t.startswith("tests/semantic/") and semantic:
            skipped[t] = "covered by the translator suite"
    return sorted(tests - set(skipped)), skipped, semantic


def run_tests(tests, semantic):
    result = {}
    for t in tests:
        full = os.path.join(c.REPO, t)
        if not os.path.exists(full):
            result[t] = ["<missing>"]
            continue
        src = open(full, encoding="utf-8", errors="replace").read()
        if c.RISKY.search(src):
            result[t] = ["<skipped: builds or downloads>"]
            continue
        ids = c.failing_ids(*c.in_container(c.test_command(t, src), 300))
        if ids:
            result[t] = ids
    if semantic:
        ids = c.failing_ids(*c.in_container(["python", "scripts/run_semantic_tests.py", "tests/semantic"], 3000))
        if ids:
            result["<translator suite>"] = ids
    return result


def compare(main_res, batch_res):
    """Returns (new failures, files that cannot be compared)."""
    new, blind = {}, []
    for t, ids in batch_res.items():
        base = main_res.get(t)
        if base and any(i.startswith("<rc ") or i == "<timeout>" for i in base):
            if ids != base:
                blind.append(t)
            continue
        extra = [i for i in ids if not base or i not in base]
        if extra:
            new[t] = extra
    return new, blind


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--prs", required=True)
    ap.add_argument("--states", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    t0 = time.time()
    report = {"name": a.name, "verdict": "INCOMPLET", "kept": {}, "dropped": {}}
    lines = [f"## Paquet {a.name} : INCOMPLET"]
    try:
        states = c.load_states(a.states)
        base = c.clone()
        report["main"] = base
        wanted = []
        for n in [int(x) for x in a.prs.split()]:
            head, why = c.fetch_pr(n, states)
            if why:
                report["dropped"][n] = why
            else:
                report["kept"][n] = head
                wanted.append(n)
        kept, dropped = merge_batch(base, wanted, "paquet")
        report["dropped"].update(dropped)
        tests, skipped, semantic = select_tests(base, "paquet")
        report.update(tests=tests, skipped_tests=skipped, translator_suite=semantic)
        c.switch(base)
        main_res = run_tests(tests, semantic)
        c.switch("paquet")
        batch_res = run_tests(tests, semantic)
        new, blind = compare(main_res, batch_res)
        culprits = {}
        if new:
            files = [t for t in new if t != "<translator suite>"]
            for n in kept:
                c.switch(base, "solo")
                if c.git("merge", "--no-ff", "--no-edit", "-q", f"pr{n}", check=False).returncode != 0:
                    c.git("merge", "--abort", check=False)
                    continue
                solo_new, _ = compare(main_res, run_tests(files, "<translator suite>" in new))
                if solo_new:
                    culprits[n] = sorted(solo_new)
            for n, t in culprits.items():
                report["dropped"][n] = "new failure: " + ", ".join(t)
            if culprits:
                kept, dropped = merge_batch(base, [n for n in kept if n not in culprits], "paquet2")
                report["dropped"].update(dropped)
                batch_res = run_tests(tests, semantic)
                new, blind = compare(main_res, batch_res)
            if new:
                report["interaction"] = "new failure with no single PR at fault (PRs interact)"
        report["kept"] = {n: h for n, h in report["kept"].items() if n in kept}
        report.update(main_failures=main_res, batch_failures=batch_res, new_failures=new,
                      not_comparable=blind)
        report["verdict"] = "PAQUET-PROPRE" if not new and not blind else (
            "NON-PROPRE" if new else "À-VÉRIFIER")
    except c.Budget:
        report["note"] = "time budget reached, partial result"
    except Exception as e:  # always leave a report
        report["error"] = str(e)[:300]
    finally:
        report["minutes"] = round((time.time() - t0) / 60, 1)
        lines = [f"## Paquet {a.name} : {report['verdict']}",
                 f"main {report.get('main', '?')} · {report['minutes']} min",
                 f"Gardées ({len(report['kept'])}) : " + " ".join(f"#{n}" for n in report["kept"]),
                 "Retirées : " + ("; ".join(f"#{n} {c.md(r)}" for n, r in report["dropped"].items()) or "aucune"),
                 f"Tests lancés : {len(report.get('tests', []))} fichiers" + (" + traducteur" if report.get("translator_suite") else ""),
                 "Nouveaux échecs : " + (c.md(report.get("new_failures")) if report.get("new_failures") else "0"),
                 "Non comparables : " + (c.md(report.get("not_comparable")) if report.get("not_comparable") else "aucun"),
                 c.md(report.get("interaction", "")) + c.md(report.get("note", "")) + c.md(report.get("error", ""))]
        c.write_report(a.out, a.name, report, lines)


if __name__ == "__main__":
    main()
