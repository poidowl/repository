#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests and self-budget enforcement for keeper.py."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import keeper

REPO_ROOT = Path(__file__).resolve().parent


class KeeperTests(unittest.TestCase):
    def _write(self, root: Path, rel: str, content: str) -> Path:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def test_python_cycle_hidden_syspath_and_prescribe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._write(repo, "a.py", "import sys\nsys.path.insert(0, '.')\nimport b\n")
            self._write(repo, "b.py", "def f():\n    import a\n    return a\n")
            self._write(repo, "c.py", "import a\n")

            res = keeper.analyze_repo(repo)
            m = res["metrics"]
            self.assertEqual(m["scc_count"], 1)
            self.assertEqual(m["scc_max_size"], 2)
            self.assertEqual(m["scc_node_count"], 2)
            self.assertEqual(m["hidden_import_count"], 1)
            self.assertEqual(m["sys_path_files"], 1)

            presc = keeper.prescribe_repo(res)
            top = presc["top_cycle_cuts"][0]
            self.assertEqual(top["src"], "b.py")
            self.assertEqual(top["dst"], "a.py")
            self.assertTrue(top["hidden"])
            self.assertEqual(top["nodes_freed"], 2)

    def test_js_mjs_html_cycle_dynamic_import_and_large_func(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._write(repo, "index.html", '<script type="module" src="./app.mjs"></script>\n')
            big_body = "\n".join(f"  const v{i} = {i};" for i in range(160))
            self._write(
                repo,
                "app.mjs",
                f'import {{ x }} from "./util.mjs";\nexport function bigWork() {{\n{big_body}\n  return x;\n}}\n',
            )
            self._write(
                repo,
                "util.mjs",
                'export const x = 1;\nexport async function load() { return import("./app.mjs"); }\n',
            )

            res = keeper.analyze_repo(repo)
            m = res["metrics"]
            self.assertEqual(m["scc_count"], 1)
            self.assertEqual(m["scc_max_size"], 2)
            self.assertEqual(m["hidden_import_count"], 1)
            self.assertEqual(m["large_funcs_count"], 1)
            self.assertEqual(res["large_funcs"][0]["name"], "bigWork")

    def test_duplicate_detection_and_canonical_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            body = "\n".join(f"x_{i} = {i}" for i in range(60)) + "\n"
            self._write(repo, "dup.py", body)
            self._write(repo, "old/dup.py", body)
            self._write(repo, "caller.py", "import dup\n")
            self._write(repo, "test_dup.py", "import dup\n")

            res = keeper.analyze_repo(repo)
            self.assertEqual(res["metrics"]["duplicate_exact_groups"], 1)
            self.assertEqual(res["metrics"]["duplicate_name_groups"], 1)

            presc = keeper.prescribe_repo(res)
            self.assertEqual(len(presc["duplicate_prescriptions"]), 1)
            self.assertEqual(presc["duplicate_prescriptions"][0]["canonical"]["path"], "dup.py")

    def test_budget_init_check_and_ratchet(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._write(repo, "a.py", "import b\n")
            self._write(repo, "b.py", "import a\n")
            bfile = repo / "keeper_budget.json"

            self.assertEqual(keeper.main(["budget", "init", str(repo), "-o", str(bfile)]), 0)
            self.assertEqual(keeper.main(["budget", "check", str(repo), "-b", str(bfile)]), 0)

            self._write(repo, "b.py", "def f():\n    import a\n")
            self.assertEqual(keeper.main(["budget", "check", str(repo), "-b", str(bfile)]), 1)

            self._write(repo, "b.py", "x = 1\n")
            self.assertEqual(keeper.main(["budget", "check", str(repo), "-b", str(bfile)]), 0)
            self.assertEqual(
                keeper.main(["budget", "init", str(repo), "-o", str(bfile), "--ratchet"]), 0
            )
            data = json.loads(bfile.read_text(encoding="utf-8"))
            self.assertEqual(data["ceilings"]["scc_count"], 0)

    def test_structure_metrics_and_unicode_git_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            self._write(repo, "docs/neon/chara.mjs", "export const c = 1;\n")
            self._write(repo, "station/patch_1028.py", "x = 1\n")
            self._write(repo, "station/robot_v2.html", "<script>var a=1;</script>\n")
            self._write(repo, "station/sode_tameshi.html", "<script>var b=2;</script>\n")
            self._write(repo, "station/台本システム.py", "y = 2\n")
            for i in range(6):
                self._write(repo, f"station/note_{i}.md", "# memo\n")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)

            res = keeper.analyze_repo(repo, sprawl_dir_files=8)
            m = res["metrics"]
            self.assertEqual(m["code_files"], 5)
            self.assertEqual(m["code_in_docs_files"], 1)
            self.assertEqual(m["stamped_code_files"], 3)
            self.assertEqual(m["sprawl_dirs_count"], 1)
            self.assertEqual(m["max_code_dir_files"], 10)
            self.assertEqual(res["sprawl_dirs"][0]["dir"], "station")

    def test_self_budget_passes(self) -> None:
        budget_file = REPO_ROOT / "keeper_budget.json"
        self.assertTrue(budget_file.exists(), "keeper_budget.json must exist in repository root")
        self.assertEqual(keeper.cmd_budget_check(REPO_ROOT, budget_file), 0)
        res = keeper.analyze_repo(REPO_ROOT)
        for key in (
            "scc_count",
            "hidden_import_count",
            "sys_path_files",
            "duplicate_exact_groups",
            "duplicate_name_groups",
            "code_in_docs_files",
            "sprawl_dirs_count",
            "stamped_code_files",
            "large_files_count",
            "large_funcs_count",
        ):
            self.assertEqual(res["metrics"][key], 0, f"self-check failed for {key}")


if __name__ == "__main__":
    unittest.main()
