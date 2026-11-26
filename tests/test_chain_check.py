"""tools/chain_check.py on synthetic work folders: verdicts per link, the weakest link, and no program name or log
line printed.

    python -I tests/test_chain_check.py

Each test writes a small work folder in make_game.py's layout (index files, listings, modules, build records,
logs/shaders.log) and, for the runtime block, a small gta6rt log by hand; no game files, no llvm-mc, no GPU. The
checks run with probing off, so nothing is launched.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import chain_check  # noqa: E402

# A program name and a title line the report must never print: they stand for game data.
SECRET = "SyntheticProgramName42"
TITLE_LINE = "[rt] title: Synthetic Title Text 42"


def signature(version: str) -> str:
    """make_game.py's llvm_mc_signature(): the command and the --version text, as a JSON pair."""
    return json.dumps([["llvm-mc"], f"LLVM version {version}\n  Registered Targets:\n    amdgcn - AMD GCN GPUs\n"])


def write(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode, **({} if isinstance(data, bytes) else {"encoding": "utf-8", "newline": "\n"})) as f:
        f.write(data if isinstance(data, (str, bytes)) else json.dumps(data))


def make_work(root: str, *, llvm: str = "21.1.8", digest: str = "0" * 64, table: str | None = None,
              library_ok: int = 6, library_total: int = 10) -> str:
    """A work folder with 4 game programs (all translated), a library of library_total programs (library_ok
    translated, the rest refused for undecodable words) and no FSPX, BVH or executable programs."""
    sh = os.path.join(root, "shaders")
    game = [f"{SECRET}_g{i}" for i in range(4)]
    write(os.path.join(sh, "dxbc", "index.json"), [{"name": n, "file": n + ".dxbc"} for n in game])
    write(os.path.join(sh, "dxbc", "fssm_index.json"), [{"name": n} for n in game])
    for n in game:
        write(os.path.join(sh, "dxbc", "disasm", n + ".s"), "s_endpgm\n")
    write(os.path.join(sh, "dxbc", "disasm.done"), {"llvm": signature(llvm), "listings": game})
    meta = {"stage": "cs", **({"descriptor_table": table} if table else {})}
    write(os.path.join(sh, "spirv", "index.json"), {n: meta for n in game})
    for n in game:
        write(os.path.join(sh, "spirv", n + ".spv"), b"\x00")
    write(os.path.join(sh, "spirv", "translation_sources.sha256"), digest + "\n")
    lib = [f"{SECRET}_lib{i}" for i in range(library_total)]
    write(os.path.join(sh, "library", "index.json"),
          [{"file": n + ".dxbc", "invalid_words": 0 if i < library_ok else 3} for i, n in enumerate(lib)])
    for n in lib:
        write(os.path.join(sh, "library", "disasm", n + ".s"), "s_endpgm\n")
    write(os.path.join(sh, "spirv", "library", "index.json"),
          {n: {**meta, "fspx_code_sha256": "ab" * 32} for n in lib[:library_ok]})
    for n in lib[:library_ok]:
        write(os.path.join(sh, "spirv", "library", n + ".spv"), b"\x00")
    write(os.path.join(sh, "spirv", "library", "library_build.json"),
          {"llvm_mc": signature(llvm), "translator": digest})
    failed = library_total - library_ok
    write(os.path.join(root, "logs", "shaders.log"), "\n".join([
        f"20:00:00    rdna2spirv.py {root}/shaders/dxbc/disasm --out {root}/shaders/spirv --shard 0..1/2",
        "2 shaders: 2 translated, 2 pass spirv-val",
        "2 shaders: 2 translated, 2 pass spirv-val",
        f"20:01:00    python.exe -I rdna2spirv.py {root}/shaders/dxbc/disasm --out {root}/shaders/spirv --merge",
        f"20:02:00    rdna2spirv.py {root}/shaders/library/disasm --out {root}/shaders/spirv/library --shard 0..0/1",
        f"{library_total} shaders: {library_ok} translated, {library_ok} pass spirv-val",
        *([f"{failed}  fall-through into undecodable words"] if failed else []),
        f"20:03:00  shaders: {SECRET} done",
        ""]))
    return root


def runtime_log(path: str, *, slot_failures: int = 3, loaded: int = 10, encoding: str = "utf-8") -> str:
    lines = [f"[rt] d3d12: {loaded} translated shaders (6 fspx, 0 fspx_compute, 0 fspx_gfx, 0 bvh, 0 exe) from X",
             "[rt] d3d12: descriptor table: plain (all 10 modules)", TITLE_LINE]
    for i in range(slot_failures):
        lines.append(f"[rt] d3d12: no translated compute shader for {SECRET} sha256 {'ab' * 32}")
        lines.append(f"[rt] d3d12_xs: slot 67 call {i + 1} failed with 0x80070057 (kind 0 1 B, kind 1 2 B, ...)")
    lines += [f"[rt] d3d12: compute pipeline library/{SECRET} (100 B SPIR-V, wave32) build starts at t=1.000 s",
              "[d3d12-trace] #7 ID3D12Device2::CreatePipelineState failed",
              "[d3d12-trace] #8 ID3D12Device2::CreatePipelineState ok",
              "game exited with code 0"]
    with open(path, "w", encoding=encoding, newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return path


def block(report: dict, name: str) -> dict:
    return next(b for b in report["blocks"] if b["name"] == name)


class ChainCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def printed(self, *argv) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(chain_check.main([*argv, "--no-probe"]), 0)
        return out.getvalue()

    def test_broken_chain_names_each_link(self):
        work = make_work(os.path.join(self.root, "w"))
        log = runtime_log(os.path.join(self.root, "run.log"))
        report = chain_check.check(work, log, probe=False)
        tools = block(report, "tools")
        self.assertEqual(tools["verdict"], "WARN")
        self.assertEqual(tools["llvm_mc_recorded"], {"21.1.8": 2})
        self.assertEqual(tools["translator_stale"], ["game", "library"])
        self.assertIn("#1923", tools["cause"])
        game, lib = block(report, "game"), block(report, "library")
        self.assertEqual((game["verdict"], game["expected"], game["translated"]), ("OK", 4, 4))
        self.assertEqual((lib["verdict"], lib["expected"], lib["translated"], lib["failed"]), ("FAIL", 10, 6, 4))
        self.assertEqual(lib["listings_with_rejected_words"], 4)
        self.assertEqual(lib["reasons"], [[4, "fall-through into undecodable words"]])
        self.assertIn("llvm-mc 21.x", lib["cause"])
        for name in ("fspx", "bvh", "exe"):
            self.assertEqual(block(report, name)["verdict"], "n/a")
        contract = block(report, "contract")
        self.assertEqual(contract["verdict"], "WARN")
        self.assertEqual(contract["modules"], {"plain": 10})
        self.assertIn("fspx_compute", contract["folders_absent"])
        rt = block(report, "runtime")
        self.assertEqual(rt["verdict"], "FAIL")
        self.assertEqual(rt["slot_failures"], {"slot 67 0x80070057": 3})
        self.assertEqual(rt["no_translated_compute"], 3)
        self.assertEqual(rt["d3d12_problems"], {"ID3D12Device2::CreatePipelineState failed": 1})
        self.assertEqual((rt["loaded"]["total"], rt["exit_code"], rt["compute_builds"]), (10, 0, 1))
        self.assertIn("against 4 untranslated library programs", rt["cause"])
        self.assertEqual(report["weakest"]["name"], "library")

    def test_clean_chain_is_ok(self):
        work = make_work(os.path.join(self.root, "w"), llvm="23.1.5", digest=chain_check.repo_translation_digest(),
                         table="tagged", library_ok=10)
        log = runtime_log(os.path.join(self.root, "run.log"), slot_failures=0, loaded=14)
        report = chain_check.check(work, log, probe=False)
        verdicts = {b["name"]: b["verdict"] for b in report["blocks"]}
        self.assertEqual(verdicts, {"tools": "OK", "game": "OK", "library": "OK", "fspx": "n/a", "bvh": "n/a",
                                    "exe": "n/a", "contract": "OK", "runtime": "WARN"})
        self.assertEqual(report["weakest"]["name"], "runtime")   # the one failed CreatePipelineState

    def test_mixed_llvm_names_the_old_one(self):
        work = make_work(os.path.join(self.root, "w"), llvm="21.1.8")
        write(os.path.join(work, "shaders", "dxbc", "disasm.done"), {"llvm": signature("23.1.5"), "listings": []})
        report = chain_check.check(work, probe=False)
        tools = block(report, "tools")
        self.assertIn("llvm-mc 21.1.8 (used for library) predates 23", tools["cause"])
        self.assertIn("different llvm-mc versions", tools["cause"])
        self.assertIn("llvm-mc 21.x", block(report, "library")["cause"])

    def test_untranslated_without_build_log(self):
        work = make_work(os.path.join(self.root, "w"))
        os.remove(os.path.join(work, "logs", "shaders.log"))
        lib = block(chain_check.check(work, probe=False), "library")
        self.assertEqual((lib["verdict"], lib["reasons"]), ("FAIL", []))
        self.assertIn("no reason", lib["cause"])

    def test_missing_translation_fails(self):
        work = make_work(os.path.join(self.root, "w"))
        os.remove(os.path.join(work, "shaders", "spirv", "library", "index.json"))
        self.assertEqual(block(chain_check.check(work, probe=False), "library")["verdict"], "FAIL")

    def test_utf16_log(self):
        work = make_work(os.path.join(self.root, "w"))
        log = runtime_log(os.path.join(self.root, "run.log"), encoding="utf-16")
        self.assertEqual(block(chain_check.check(work, log, probe=False), "runtime")["no_translated_compute"], 3)

    def test_no_names_or_log_text_printed(self):
        work = make_work(os.path.join(self.root, "w"))
        log = runtime_log(os.path.join(self.root, "run.log"))
        for extra in ((), ("--json",)):
            text = self.printed(work, "--log", log, *extra)
            self.assertNotIn(SECRET, text)
            self.assertNotIn("Synthetic Title Text", text)
            self.assertIn("weakest", text)

    def test_paths_only_with_show_paths(self):
        work = make_work(os.path.join(self.root, "w"))
        log = runtime_log(os.path.join(self.root, "run.log"))
        for extra in ((), ("--json",)):
            text = self.printed(work, "--log", log, *extra)
            self.assertNotIn(self.root, text.replace("\\\\", "\\"))
            self.assertNotIn("run.log", text)
        self.assertTrue(self.printed(work, "--log", log).startswith("chain check\n"))
        shown = self.printed(work, "--log", log, "--show-paths")
        self.assertIn(work, shown)
        self.assertIn(log, shown)

    def test_reasons_are_the_translator_messages(self):
        work = make_work(os.path.join(self.root, "w"))
        with open(os.path.join(work, "logs", "shaders.log"), "a", encoding="utf-8", newline="\n") as f:
            f.write(f"20:04:00    rdna2spirv.py {work}/shaders/library/disasm --out {work}/shaders/spirv/library\n"
                    "10 shaders: 4 translated, 4 pass spirv-val\n"
                    "2  s_sethalt\n"
                    f"2  {SECRET} source\n"
                    f"1  E:\\work\\{SECRET}.s\n"
                    "1  translator error KeyError\n")
        lib = block(chain_check.check(work, probe=False), "library")
        self.assertEqual(dict((r, n) for n, r in lib["reasons"]),
                         {"s_sethalt": 2, "# source": 2, "other": 1, "translator error KeyError": 1})

    def test_empty_and_partial_folders_are_na(self):
        empty = os.path.join(self.root, "empty")
        os.makedirs(empty)
        report = chain_check.check(empty, probe=False)
        self.assertEqual({b["verdict"] for b in report["blocks"]}, {"n/a"})
        self.assertEqual(report["weakest"]["name"], "none")
        self.assertIn("weakest link: none", self.printed(empty))
        partial = os.path.join(self.root, "partial")
        write(os.path.join(partial, "shaders", "dxbc", "fssm_index.json"), "{not json")
        write(os.path.join(partial, "shaders", "library", "index.json"), [1, "x", None])
        write(os.path.join(partial, "shaders", "spirv", "library", "index.json"), {"": 3, "a": [1]})
        write(os.path.join(partial, "shaders", "spirv", "library", "library_build.json"), {"llvm_mc": 5})
        report = chain_check.check(partial, os.path.join(partial, "missing.log"), probe=False)
        verdicts = {b["name"]: b["verdict"] for b in report["blocks"]}
        self.assertEqual((verdicts["game"], verdicts["library"], verdicts["fspx"]), ("FAIL", "FAIL", "n/a"))
        self.assertEqual(verdicts["runtime"], "FAIL")   # no translated-shaders line in an unreadable log
        self.printed(partial)

    def test_strict_exit_code(self):
        work = make_work(os.path.join(self.root, "w"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(chain_check.main([work, "--no-probe"]), 0)
            self.assertEqual(chain_check.main([work, "--no-probe", "--strict"]), 1)
        clean = make_work(os.path.join(self.root, "c"), llvm="23.1.5", digest=chain_check.repo_translation_digest(),
                          table="tagged", library_ok=10)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(chain_check.main([clean, "--no-probe", "--strict"]), 0)

    def test_help_names_the_thresholds(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            chain_check.main(["--help"])
        text = " ".join(out.getvalue().split())
        self.assertIn("WARN up to 1% untranslated", text)
        self.assertIn("older than 23", text)
        self.assertIn("--strict", text)

    def test_repo_digest_matches_make_game(self):
        sys.path.insert(0, ROOT)
        import make_game
        self.assertEqual(chain_check.repo_translation_digest(), make_game.translation_sources_digest())

    def test_bad_arguments_fail(self):
        for argv in ([os.path.join(self.root, "none")], [self.root, "--log", os.path.join(self.root, "x.log")]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    chain_check.main(argv)
                self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
