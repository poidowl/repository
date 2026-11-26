#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Repository health diagnostics, prescriptions, and budget enforcement.

Zero-dependency (Python standard library only), read-only analyzer for
Python, JavaScript/TypeScript (.js/.mjs/.cjs/.ts/.tsx), and HTML (<script>).

Commands:
  diagnose <repo> [--json] [--large-file N] [--large-func N]
  prescribe <repo> [--json] [--top N]
  budget init <repo> [-o FILE] [--ratchet]
  budget check <repo> [-b FILE]
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import warnings
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

PY_EXTS = {".py"}
JS_EXTS = {".js", ".mjs", ".cjs", ".ts", ".tsx"}
HTML_EXTS = {".html", ".htm"}
CODE_EXTS = PY_EXTS | JS_EXTS | HTML_EXTS

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".cache",
}

IGNORED_DUP_NAMES = {"__init__.py", "index.html", "index.js", "index.mjs", "index.ts", "main.py"}

_RE_JS_BLOCK_COMMENT = re.compile(r"/\*[\s\S]*?\*/")
_RE_JS_LINE_COMMENT = re.compile(r"(^|[^\:])//[^\n]*")
_RE_JS_STRING = re.compile(r"""'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`""")
_RE_JS_STATIC_IMPORT = re.compile(
    r"""(?:^|[;\n])\s*(?:import|export)\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]""",
    re.MULTILINE,
)
_RE_JS_REQUIRE = re.compile(r"""\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_RE_JS_DYNAMIC_IMPORT = re.compile(r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_RE_JS_FUNC_START = re.compile(
    r"""(?:^|[;\n])\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{"""
    r"""|(?:^|[;\n])\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>\s*\{""",
    re.MULTILINE,
)
_RE_HTML_SCRIPT_SRC = re.compile(
    r"""<script\b[^>]*\bsrc\s*=\s*['"]([^'"]+)['"][^>]*>""",
    re.IGNORECASE,
)
_RE_HTML_INLINE_SCRIPT = re.compile(
    r"""<script\b(?![^>]*\bsrc\s*=)[^>]*>([\s\S]*?)</script>""",
    re.IGNORECASE,
)

BUDGET_KEYS = (
    "scc_count",
    "scc_max_size",
    "scc_node_count",
    "hidden_import_count",
    "sys_path_files",
    "duplicate_exact_groups",
    "duplicate_name_groups",
    "large_files_count",
    "max_file_lines",
    "large_funcs_count",
)


@dataclass(frozen=True)
class ImportRef:
    src: str
    dst: str
    line: int
    hidden: bool
    raw: str


def _is_test_path(rel_path: str) -> bool:
    parts = rel_path.lower().split("/")
    base = parts[-1]
    if any(p in ("test", "tests", "__tests__", "spec", "specs") for p in parts[:-1]):
        return True
    return (
        base.startswith("test_")
        or base.endswith("_test.py")
        or ".test." in base
        or ".spec." in base
    )


def _list_repo_files(repo: Path) -> List[str]:
    """Return sorted relative POSIX paths (tracked + non-ignored untracked)."""
    if (repo / ".git").exists():
        try:
            out = subprocess.check_output(
                ["git", "-C", str(repo), "ls-files", "--cached", "--others", "--exclude-standard"],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", errors="replace")
            files = sorted({line.strip() for line in out.splitlines() if line.strip()})
            return [f for f in files if (repo / f).is_file()]
        except Exception:
            pass

    result: List[str] = []
    for root, dirs, filenames in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(root) / fn
            try:
                result.append(p.relative_to(repo).as_posix())
            except ValueError:
                continue
    return sorted(result)


def _is_shallow_repo(repo: Path) -> bool:
    if not (repo / ".git").exists():
        return False
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="replace").strip()
        return out == "true"
    except Exception:
        return False


def _strip_js_comments_keep_lines(src: str) -> str:
    s = _RE_JS_BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), src)
    return _RE_JS_LINE_COMMENT.sub(r"\1", s)


def _blank_js_strings_keep_lines(src: str) -> str:
    return _RE_JS_STRING.sub(lambda m: "\n" * m.group(0).count("\n"), src)


def _find_js_large_funcs(rel: str, cleaned: str, threshold: int) -> List[Dict[str, Any]]:
    masked = _blank_js_strings_keep_lines(cleaned)
    found: List[Dict[str, Any]] = []
    n = len(masked)
    for m in _RE_JS_FUNC_START.finditer(masked):
        name = m.group(1) or m.group(2) or "<anon>"
        brace_pos = masked.find("{", m.start(), m.end())
        if brace_pos < 0:
            continue
        depth = 0
        end_pos = -1
        for i in range(brace_pos, n):
            ch = masked[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_pos = i
                    break
        if end_pos < 0:
            continue
        start_ln = masked.count("\n", 0, brace_pos) + 1
        end_ln = masked.count("\n", 0, end_pos) + 1
        flen = end_ln - start_ln + 1
        if flen >= threshold:
            found.append({"file": rel, "name": name, "line": start_ln, "lines": flen})
    return found


def _resolve_js_spec(src_rel: str, spec: str, file_set: Set[str]) -> Optional[str]:
    if not spec or spec.startswith(("http://", "https://", "//", "data:")):
        return None
    base_dir = posixpath.dirname(src_rel)
    if spec.startswith("/"):
        cand_base = spec.lstrip("/")
    else:
        cand_base = posixpath.normpath(posixpath.join(base_dir, spec))

    candidates = [cand_base]
    if not posixpath.splitext(cand_base)[1]:
        for ext in (".mjs", ".js", ".ts", ".tsx", ".cjs"):
            candidates.append(cand_base + ext)
            candidates.append(posixpath.join(cand_base, "index" + ext))
    for c in candidates:
        if c in file_set:
            return c
    return None


def _common_prefix_len(a: Sequence[str], b: Sequence[str]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x == y:
            n += 1
        else:
            break
    return n


def _resolve_py_target(
    src_rel: str,
    mod_name: Optional[str],
    level: int,
    py_set: Set[str],
    stem_to_files: Dict[str, List[str]],
) -> Optional[str]:
    src_dir = posixpath.dirname(src_rel)
    if level > 0:
        cur = src_dir
        for _ in range(level - 1):
            cur = posixpath.dirname(cur)
        if mod_name:
            rel_mod = mod_name.replace(".", "/")
            for cand in (
                posixpath.normpath(posixpath.join(cur, rel_mod + ".py")),
                posixpath.normpath(posixpath.join(cur, rel_mod, "__init__.py")),
            ):
                if cand in py_set:
                    return cand
        return None

    if not mod_name:
        return None

    rel_mod = mod_name.replace(".", "/")
    for prefix in (src_dir, ""):
        for cand in (
            posixpath.normpath(posixpath.join(prefix, rel_mod + ".py")) if prefix else rel_mod + ".py",
            posixpath.normpath(posixpath.join(prefix, rel_mod, "__init__.py")) if prefix else rel_mod + "/__init__.py",
        ):
            if cand in py_set:
                return cand

    parts = mod_name.split(".")
    src_parts = src_dir.split("/") if src_dir else []
    for stem in (parts[-1], parts[0]):
        cands = stem_to_files.get(stem)
        if not cands:
            continue
        if len(cands) == 1:
            return cands[0]
        return max(
            cands,
            key=lambda c: _common_prefix_len(src_parts, posixpath.dirname(c).split("/")),
        )
    return None


def _scan_python_file(
    rel: str,
    text: str,
    py_set: Set[str],
    stem_to_files: Dict[str, List[str]],
    large_func_lines: int,
) -> Tuple[List[ImportRef], List[Dict[str, Any]], bool]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(text, filename=rel)
    except SyntaxError:
        return ([], [], False)

    uses_sys_path = False
    large_funcs: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "path"
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
        ):
            uses_sys_path = True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_ln = getattr(node, "end_lineno", None)
            if end_ln:
                flen = end_ln - node.lineno + 1
                if flen >= large_func_lines:
                    large_funcs.append(
                        {"file": rel, "name": node.name, "line": node.lineno, "lines": flen}
                    )

    refs: List[ImportRef] = []

    def _visit(node: ast.AST, in_func: bool) -> None:
        for child in ast.iter_child_nodes(node):
            child_in_func = in_func or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            if isinstance(child, ast.Import):
                for alias in child.names:
                    tgt = _resolve_py_target(rel, alias.name, 0, py_set, stem_to_files)
                    if tgt and tgt != rel:
                        refs.append(ImportRef(rel, tgt, child.lineno, in_func, alias.name))
            elif isinstance(child, ast.ImportFrom):
                tgt = _resolve_py_target(rel, child.module, child.level, py_set, stem_to_files)
                if tgt and tgt != rel:
                    refs.append(
                        ImportRef(rel, tgt, child.lineno, in_func, "." * child.level + (child.module or ""))
                    )
                elif child.level > 0 and not child.module:
                    for alias in child.names:
                        tgt2 = _resolve_py_target(rel, alias.name, child.level, py_set, stem_to_files)
                        if tgt2 and tgt2 != rel:
                            refs.append(
                                ImportRef(rel, tgt2, child.lineno, in_func, "." * child.level + alias.name)
                            )
            _visit(child, child_in_func)

    _visit(tree, False)
    return (refs, large_funcs, uses_sys_path)


def _scan_js_file(
    rel: str, text: str, code_set: Set[str], large_func_lines: int
) -> Tuple[List[ImportRef], List[Dict[str, Any]]]:
    cleaned = _strip_js_comments_keep_lines(text)
    refs: List[ImportRef] = []
    for regex, hidden in (
        (_RE_JS_STATIC_IMPORT, False),
        (_RE_JS_REQUIRE, False),
        (_RE_JS_DYNAMIC_IMPORT, True),
    ):
        for m in regex.finditer(cleaned):
            spec = m.group(1)
            tgt = _resolve_js_spec(rel, spec, code_set)
            if tgt and tgt != rel:
                ln = cleaned.count("\n", 0, m.start()) + 1
                refs.append(ImportRef(rel, tgt, ln, hidden, spec))
    return (refs, _find_js_large_funcs(rel, cleaned, large_func_lines))


def _scan_html_file(rel: str, text: str, code_set: Set[str]) -> List[ImportRef]:
    refs: List[ImportRef] = []
    for m in _RE_HTML_SCRIPT_SRC.finditer(text):
        spec = m.group(1)
        tgt = _resolve_js_spec(rel, spec, code_set)
        if tgt and tgt != rel:
            refs.append(ImportRef(rel, tgt, text.count("\n", 0, m.start()) + 1, False, spec))
    for sm in _RE_HTML_INLINE_SCRIPT.finditer(text):
        base_ln = text.count("\n", 0, sm.start(1))
        cleaned = _strip_js_comments_keep_lines(sm.group(1))
        for regex, hidden in ((_RE_JS_STATIC_IMPORT, False), (_RE_JS_DYNAMIC_IMPORT, True)):
            for m in regex.finditer(cleaned):
                spec = m.group(1)
                tgt = _resolve_js_spec(rel, spec, code_set)
                if tgt and tgt != rel:
                    ln = base_ln + cleaned.count("\n", 0, m.start()) + 1
                    refs.append(ImportRef(rel, tgt, ln, hidden, spec))
    return refs


def _tarjan_scc(nodes: Iterable[str], adj: Dict[str, Set[str]]) -> List[List[str]]:
    index_counter = 0
    stack: List[str] = []
    on_stack: Set[str] = set()
    index: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    sccs: List[List[str]] = []
    sys.setrecursionlimit(max(10000, sys.getrecursionlimit()))

    def strongconnect(v: str) -> None:
        nonlocal index_counter
        index[v] = lowlink[v] = index_counter
        index_counter += 1
        stack.append(v)
        on_stack.add(v)
        for w in sorted(adj.get(v, ())):
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])
        if lowlink[v] == index[v]:
            comp: List[str] = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                sccs.append(sorted(comp))

    for n in sorted(set(nodes)):
        if n not in index:
            strongconnect(n)
    sccs.sort(key=lambda c: (-len(c), c[0]))
    return sccs


def _collect_duplicates(
    code_files: Sequence[str],
    file_lines: Dict[str, int],
    file_hashes: Dict[str, str],
    min_dup_lines: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    by_hash: Dict[str, List[str]] = defaultdict(list)
    by_name: Dict[str, List[str]] = defaultdict(list)
    for rel in code_files:
        if file_lines[rel] < min_dup_lines:
            continue
        by_hash[file_hashes[rel]].append(rel)
        bn = posixpath.basename(rel)
        if bn not in IGNORED_DUP_NAMES:
            by_name[bn].append(rel)

    exact = [
        {"lines": file_lines[g[0]], "files": sorted(g)}
        for g in by_hash.values()
        if len(g) > 1
    ]
    exact.sort(key=lambda x: (-x["lines"] * len(x["files"]), x["files"][0]))

    by_basename = [
        {
            "name": name,
            "identical": len({file_hashes[f] for f in g}) == 1,
            "files": [{"path": f, "lines": file_lines[f]} for f in sorted(g)],
        }
        for name, g in by_name.items()
        if len(g) > 1
    ]
    by_basename.sort(key=lambda x: (-sum(i["lines"] for i in x["files"]), x["name"]))
    return (exact, by_basename)


def analyze_repo(
    repo: Path,
    *,
    include_tests: bool = False,
    large_file_lines: int = 1000,
    large_func_lines: int = 150,
    min_dup_lines: int = 50,
) -> Dict[str, Any]:
    repo = repo.resolve()
    code_files: List[str] = []
    test_files: Set[str] = set()
    file_lines: Dict[str, int] = {}
    file_hashes: Dict[str, str] = {}

    for rel in _list_repo_files(repo):
        if posixpath.splitext(rel)[1].lower() not in CODE_EXTS:
            continue
        try:
            raw_bytes = (repo / rel).read_bytes()
        except OSError:
            continue
        if b"\x00" in raw_bytes[:8192]:
            continue
        file_lines[rel] = raw_bytes.count(b"\n") + (1 if raw_bytes and not raw_bytes.endswith(b"\n") else 0)
        file_hashes[rel] = hashlib.sha256(raw_bytes).hexdigest()
        if _is_test_path(rel):
            test_files.add(rel)
            if not include_tests:
                continue
        code_files.append(rel)

    code_set = set(code_files)
    py_set = {f for f in code_files if posixpath.splitext(f)[1].lower() in PY_EXTS}
    stem_to_files: Dict[str, List[str]] = defaultdict(list)
    for f in sorted(py_set):
        stem = posixpath.splitext(posixpath.basename(f))[0]
        if stem != "__init__":
            stem_to_files[stem].append(f)

    import_refs: List[ImportRef] = []
    sys_path_files: List[str] = []
    large_funcs: List[Dict[str, Any]] = []

    for rel in code_files:
        ext = posixpath.splitext(rel)[1].lower()
        try:
            text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if ext in PY_EXTS:
            refs, funcs, uses_sp = _scan_python_file(rel, text, py_set, stem_to_files, large_func_lines)
            import_refs.extend(refs)
            large_funcs.extend(funcs)
            if uses_sp:
                sys_path_files.append(rel)
        elif ext in JS_EXTS:
            refs, funcs = _scan_js_file(rel, text, code_set, large_func_lines)
            import_refs.extend(refs)
            large_funcs.extend(funcs)
        elif ext in HTML_EXTS:
            import_refs.extend(_scan_html_file(rel, text, code_set))

    adj: Dict[str, Set[str]] = defaultdict(set)
    rev_adj: Dict[str, Set[str]] = defaultdict(set)
    edge_refs: Dict[Tuple[str, str], List[ImportRef]] = defaultdict(list)
    hidden_count = 0
    for ref in import_refs:
        adj[ref.src].add(ref.dst)
        rev_adj[ref.dst].add(ref.src)
        edge_refs[(ref.src, ref.dst)].append(ref)
        if ref.hidden:
            hidden_count += 1

    sccs = _tarjan_scc(code_files, adj)
    exact_dups, name_dups = _collect_duplicates(code_files, file_lines, file_hashes, min_dup_lines)
    large_files = sorted(
        [{"file": f, "lines": file_lines[f]} for f in code_files if file_lines[f] >= large_file_lines],
        key=lambda x: (-x["lines"], x["file"]),
    )
    large_funcs.sort(key=lambda x: (-x["lines"], x["file"], x["line"]))
    ext_counts: Dict[str, int] = defaultdict(int)
    for f in code_files:
        ext_counts[posixpath.splitext(f)[1].lower()] += 1

    metrics = {
        "code_files": len(code_files),
        "total_code_lines": sum(file_lines[f] for f in code_files),
        "scc_count": len(sccs),
        "scc_max_size": max((len(c) for c in sccs), default=0),
        "scc_node_count": sum(len(c) for c in sccs),
        "hidden_import_count": hidden_count,
        "sys_path_files": len(sys_path_files),
        "duplicate_exact_groups": len(exact_dups),
        "duplicate_name_groups": len(name_dups),
        "large_files_count": len(large_files),
        "max_file_lines": max((file_lines[f] for f in code_files), default=0),
        "large_funcs_count": len(large_funcs),
    }

    return {
        "repo": str(repo),
        "repo_name": repo.name,
        "is_shallow": _is_shallow_repo(repo),
        "languages": dict(sorted(ext_counts.items())),
        "metrics": metrics,
        "sccs": sccs,
        "exact_dup_groups": exact_dups,
        "name_dup_groups": name_dups,
        "large_files": large_files,
        "large_funcs": large_funcs,
        "top_fan_in": sorted(
            [{"file": f, "callers": len(c)} for f, c in rev_adj.items()],
            key=lambda x: (-x["callers"], x["file"]),
        )[:15],
        "top_fan_out": sorted(
            [{"file": f, "deps": len(d)} for f, d in adj.items()],
            key=lambda x: (-x["deps"], x["file"]),
        )[:15],
        "sys_path_file_list": sorted(sys_path_files),
        "_adj": adj,
        "_rev_adj": rev_adj,
        "_edge_refs": edge_refs,
        "_test_files": test_files,
    }


def _git_file_stats(repo: Path, rel_path: str) -> Tuple[int, str]:
    if not (repo / ".git").exists():
        return (0, "")
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo), "log", "--format=%cs", "--", rel_path],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="replace")
        dates = [d.strip() for d in out.splitlines() if d.strip()]
        return (len(dates), dates[0] if dates else "")
    except Exception:
        return (0, "")


def _has_companion_test(rel_path: str, test_files: Set[str]) -> bool:
    stem = posixpath.splitext(posixpath.basename(rel_path))[0].lower()
    parent = posixpath.dirname(rel_path).lower()
    for tf in test_files:
        tbase = posixpath.basename(tf).lower()
        tparent = posixpath.dirname(tf).lower()
        if stem in tbase and (tparent == parent or tparent.startswith(parent + "/")):
            return True
    return False


def prescribe_repo(analysis: Dict[str, Any], *, top_n: int = 10) -> Dict[str, Any]:
    repo = Path(analysis["repo"])
    adj: Dict[str, Set[str]] = analysis["_adj"]
    rev_adj: Dict[str, Set[str]] = analysis["_rev_adj"]
    edge_refs: Dict[Tuple[str, str], List[ImportRef]] = analysis["_edge_refs"]
    test_files: Set[str] = analysis["_test_files"]

    cycle_cuts: List[Dict[str, Any]] = []
    for scc_idx, scc in enumerate(analysis["sccs"]):
        scc_set = set(scc)
        sub_adj = {u: {v for v in adj.get(u, ()) if v in scc_set} for u in scc}
        candidates: List[Dict[str, Any]] = []
        for u in scc:
            for v in sorted(sub_adj[u]):
                sub_adj[u].remove(v)
                new_sccs = _tarjan_scc(scc, sub_adj)
                sub_adj[u].add(v)
                new_nodes = sum(len(c) for c in new_sccs)
                new_max = max((len(c) for c in new_sccs), default=0)
                nodes_freed = len(scc) - new_nodes
                refs = edge_refs.get((u, v), [])
                has_hidden = any(r.hidden for r in refs)
                score = (
                    nodes_freed * 100
                    + (len(scc) - new_max) * 40
                    + (15 if has_hidden else 0)
                    - min(len(refs), 10) * 2
                )
                candidates.append(
                    {
                        "scc_index": scc_idx + 1,
                        "scc_size": len(scc),
                        "src": u,
                        "dst": v,
                        "lines": sorted({r.line for r in refs if r.line > 0}),
                        "ref_count": len(refs),
                        "hidden": has_hidden,
                        "nodes_freed": nodes_freed,
                        "new_max_scc": new_max,
                        "new_scc_count": len(new_sccs),
                        "score": score,
                    }
                )
        candidates.sort(key=lambda x: (-x["score"], -x["nodes_freed"], x["ref_count"], x["src"], x["dst"]))
        cycle_cuts.append({"scc_index": scc_idx + 1, "scc_size": len(scc), "members": scc, "top_cuts": candidates[:5]})

    all_cuts = [c for g in cycle_cuts for c in g["top_cuts"][:3]]
    all_cuts.sort(key=lambda x: (-x["score"], -x["scc_size"], -x["nodes_freed"], x["src"], x["dst"]))

    dup_prescriptions: List[Dict[str, Any]] = []
    for grp in analysis["name_dup_groups"]:
        scored: List[Dict[str, Any]] = []
        for entry in grp["files"]:
            path = entry["path"]
            callers = len(rev_adj.get(path, ()))
            has_test = _has_companion_test(path, test_files)
            commits, last_date = _git_file_stats(repo, path)
            score = callers * 50 + (20 if has_test else 0) + min(commits, 30) * 2 - path.count("/") * 3
            scored.append(
                {
                    "path": path,
                    "lines": entry["lines"],
                    "callers": callers,
                    "has_test": has_test,
                    "git_commits": commits,
                    "last_date": last_date,
                    "score": score,
                }
            )
        scored.sort(key=lambda x: (-x["score"], -x["callers"], -x["git_commits"], x["last_date"], x["path"]))
        dup_prescriptions.append(
            {
                "name": grp["name"],
                "identical": grp["identical"],
                "total_lines": sum(f["lines"] for f in scored),
                "canonical": scored[0],
                "others": scored[1:],
            }
        )
    dup_prescriptions.sort(key=lambda x: (-x["total_lines"], x["name"]))

    return {
        "repo": analysis["repo"],
        "repo_name": analysis["repo_name"],
        "is_shallow": analysis["is_shallow"],
        "summary": analysis["metrics"],
        "top_cycle_cuts": all_cuts[:top_n],
        "cycle_clusters": cycle_cuts,
        "duplicate_prescriptions": dup_prescriptions[:top_n],
    }


def _strip_internal(analysis: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in analysis.items() if not k.startswith("_")}


def _print_diagnose(analysis: Dict[str, Any]) -> None:
    m = analysis["metrics"]
    langs = ", ".join(f"{k}:{v}" for k, v in analysis["languages"].items()) or "(none)"
    print(f"=== DIAGNOSE: {analysis['repo_name']} ({analysis['repo']}) ===")
    print(f"Languages : {langs}")
    print(f"Code files: {m['code_files']} ({m['total_code_lines']} lines)")
    print()
    print("Metrics:")
    for k in BUDGET_KEYS:
        extra = f" (max={m['max_file_lines']} lines)" if k == "large_files_count" else ""
        if k == "max_file_lines":
            continue
        print(f"  {k:24s}: {m[k]}{extra}")
    if analysis["sccs"]:
        print("\nCycle clusters (SCC):")
        for i, scc in enumerate(analysis["sccs"], 1):
            preview = ", ".join(scc[:6]) + (f" ... (+{len(scc)-6})" if len(scc) > 6 else "")
            print(f"  #{i} (size {len(scc)}): {preview}")
    if analysis["large_files"]:
        print("\nLargest files (top 10):")
        for lf in analysis["large_files"][:10]:
            print(f"  {lf['lines']:6d}  {lf['file']}")


def _print_prescribe(presc: Dict[str, Any]) -> None:
    print(f"=== PRESCRIBE: {presc['repo_name']} ===")
    cuts = presc["top_cycle_cuts"]
    if not cuts:
        print("\n[Cycle cuts] No circular dependencies found.")
    else:
        print(f"\n[Cycle cuts] Top {len(cuts)} edges to break:")
        for idx, c in enumerate(cuts, 1):
            ln = f":{c['lines'][0]}" if c["lines"] else ""
            tag = " [hidden]" if c["hidden"] else ""
            effect = (
                f"frees {c['nodes_freed']}/{c['scc_size']} nodes (max SCC {c['scc_size']}->{c['new_max_scc']})"
                if c["nodes_freed"] > 0 or c["new_max_scc"] < c["scc_size"]
                else f"weakens SCC#{c['scc_index']} (size {c['scc_size']}, multi-cut needed)"
            )
            print(f"  {idx:2d}. {c['src']}{ln} -> {c['dst']}{tag}")
            print(f"      effect: {effect} | refs={c['ref_count']}")

    dups = presc["duplicate_prescriptions"]
    if not dups:
        print("\n[Duplicate canonicals] No duplicate code groups found.")
    else:
        print(f"\n[Duplicate canonicals] Top {len(dups)} groups:")
        if presc.get("is_shallow"):
            print("  (note: shallow clone detected; git commit counts are partial)")
        for idx, d in enumerate(dups, 1):
            kind = "EXACT" if d["identical"] else "DIVERGED"
            can = d["canonical"]
            print(f"  {idx:2d}. {d['name']} [{kind}, total={d['total_lines']} lines]")
            print(
                f"      KEEP : {can['path']} (lines={can['lines']}, callers={can['callers']}, "
                f"test={can['has_test']}, commits={can['git_commits']}, last={can['last_date'] or '-'})"
            )
            for o in d["others"]:
                print(
                    f"      CHECK: {o['path']} (lines={o['lines']}, callers={o['callers']}, "
                    f"test={o['has_test']}, commits={o['git_commits']}, last={o['last_date'] or '-'})"
                )


def cmd_budget_init(
    repo: Path, out_path: Path, ratchet: bool, large_file: int, large_func: int
) -> int:
    analysis = analyze_repo(repo, large_file_lines=large_file, large_func_lines=large_func)
    current = {k: analysis["metrics"][k] for k in BUDGET_KEYS}
    if ratchet and out_path.exists():
        old_ceilings = json.loads(out_path.read_text(encoding="utf-8")).get("ceilings", {})
        ceilings = {
            k: min(current[k], old_ceilings[k]) if k in old_ceilings else current[k]
            for k in BUDGET_KEYS
        }
    else:
        ceilings = current

    payload = {
        "schema": "keeper.budget.v1",
        "repo_name": analysis["repo_name"],
        "options": {"large_file_lines": large_file, "large_func_lines": large_func},
        "ceilings": ceilings,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote budget: {out_path}")
    for k in BUDGET_KEYS:
        print(f"  {k:24s}: {ceilings[k]}")
    return 0


def cmd_budget_check(repo: Path, budget_path: Path) -> int:
    if not budget_path.exists():
        print(f"ERROR: budget file not found: {budget_path}", file=sys.stderr)
        return 2
    budget = json.loads(budget_path.read_text(encoding="utf-8"))
    opts = budget.get("options", {})
    analysis = analyze_repo(
        repo,
        large_file_lines=int(opts.get("large_file_lines", 1000)),
        large_func_lines=int(opts.get("large_func_lines", 150)),
    )
    ceilings: Dict[str, int] = budget.get("ceilings", {})
    metrics = analysis["metrics"]
    violations = [
        f"  FAIL {k:24s}: {metrics.get(k, 0)} > ceiling {c} (+{metrics.get(k, 0) - c})"
        for k, c in ceilings.items()
        if metrics.get(k, 0) > c
    ]
    improvements = [
        f"  DOWN {k:24s}: {metrics.get(k, 0)} < ceiling {c} ({metrics.get(k, 0) - c})"
        for k, c in ceilings.items()
        if metrics.get(k, 0) < c
    ]
    if violations:
        print(f"BUDGET FAILED: {analysis['repo_name']}")
        for line in violations:
            print(line)
        return 1
    print(f"BUDGET OK: {analysis['repo_name']}")
    if improvements:
        print("Improvements (can lower ceiling with `budget init --ratchet`):")
        for line in improvements:
            print(line)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Repository diagnostics, prescription, and budget guard.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_diag = sub.add_parser("diagnose", help="Measure tangle and bloat metrics")
    p_diag.add_argument("repo", type=Path)
    p_diag.add_argument("--json", action="store_true")
    p_diag.add_argument("--include-tests", action="store_true")
    p_diag.add_argument("--large-file", type=int, default=1000)
    p_diag.add_argument("--large-func", type=int, default=150)

    p_presc = sub.add_parser("prescribe", help="Rank cycle cuts and duplicate canonicals")
    p_presc.add_argument("repo", type=Path)
    p_presc.add_argument("--json", action="store_true")
    p_presc.add_argument("--top", type=int, default=10)
    p_presc.add_argument("--large-file", type=int, default=1000)
    p_presc.add_argument("--large-func", type=int, default=150)

    p_bud = sub.add_parser("budget", help="Initialize or enforce metric ceilings")
    bud_sub = p_bud.add_subparsers(dest="budget_cmd", required=True)

    p_binit = bud_sub.add_parser("init", help="Write current metrics as budget ceiling")
    p_binit.add_argument("repo", type=Path)
    p_binit.add_argument("-o", "--output", type=Path, default=None)
    p_binit.add_argument("--ratchet", action="store_true")
    p_binit.add_argument("--large-file", type=int, default=1000)
    p_binit.add_argument("--large-func", type=int, default=150)

    p_bchk = bud_sub.add_parser("check", help="Fail if current metrics exceed budget")
    p_bchk.add_argument("repo", type=Path)
    p_bchk.add_argument("-b", "--budget", type=Path, default=None)

    args = parser.parse_args(argv)
    if args.cmd == "diagnose":
        res = analyze_repo(
            args.repo,
            include_tests=args.include_tests,
            large_file_lines=args.large_file,
            large_func_lines=args.large_func,
        )
        if args.json:
            print(json.dumps(_strip_internal(res), indent=2, ensure_ascii=False, sort_keys=True))
        else:
            _print_diagnose(res)
        return 0

    if args.cmd == "prescribe":
        res = analyze_repo(args.repo, large_file_lines=args.large_file, large_func_lines=args.large_func)
        presc = prescribe_repo(res, top_n=args.top)
        if args.json:
            print(json.dumps(presc, indent=2, ensure_ascii=False, sort_keys=True))
        else:
            _print_prescribe(presc)
        return 0

    if args.cmd == "budget":
        default_file = args.repo.resolve() / "keeper_budget.json"
        if args.budget_cmd == "init":
            return cmd_budget_init(args.repo, args.output or default_file, args.ratchet, args.large_file, args.large_func)
        if args.budget_cmd == "check":
            return cmd_budget_check(args.repo, args.budget or default_file)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
