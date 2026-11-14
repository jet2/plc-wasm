"""Fail-closed publication check of staged blobs or complete outgoing histories.

No matched source text is printed. This is a local defence, not a proof against
arbitrary obfuscation; manual review and the allowlist remain mandatory.
"""
from __future__ import annotations
import argparse
import ast
import base64
import hashlib
import html
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_PATHS = frozenset({
    ".gitignore", ".gitattributes", ".githooks/pre-push", "AGENTS.md", "README.md", "ABI.md",
    "Cargo.toml", "Cargo.lock", "cargo-local.ps1", "ci/github-actions.yml",
    "src/lib.rs", "src/main.rs", "tests/runtime.rs", "tests/test_publish_guard.py",
    "tools/compile_init.py", "tools/compile_program.py", "tools/compiler_coverage.py",
    "tools/scl_audit.py", "tools/scl_codegen.py", "tools/scl_frontend.py", "tools/scl_support.py", "tools/check_publish.py",
    "tools/s7_layout.py", "src/s7.rs", "tests/s7.rs", "S7.md",
})
MAX_BLOB = 2 * 1024 * 1024
WINDOW = 16
TOKEN = re.compile(r"[A-Za-z_]\w*|\d+|:=|=>|<>|<=|>=|\.\.|[^\s]", re.UNICODE)
DECLARATION = re.compile(r"\b(?:FUNCTION(?:_BLOCK)?|ORGANIZATION_BLOCK|DATA_BLOCK|TYPE)\s+[A-Za-z_]\w*\s*[:;]", re.I)
ASSIGNMENT = re.compile(r"\b[A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*|\s*\[[^\]\r\n]{1,200}\])*\s*:=")
FIELD_DECLARATION = re.compile(r"\b[A-Za-z_]\w*\s*:\s*(?:BOOL|BYTE|WORD|DWORD|INT|DINT|REAL|STRING)\s*(?:;|:=)", re.I)
ENCODED = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{128,}={0,2}(?![A-Za-z0-9+/])")


def git(*args, cwd=ROOT):
    return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.PIPE)


def tokens(text):
    return TOKEN.findall(html.unescape(text).upper())


def fingerprints(text):
    parts = tokens(text)
    return {hashlib.sha256("\x1f".join(parts[i:i+WINDOW]).encode("utf8")).digest()
            for i in range(max(0, len(parts) - WINDOW + 1))}


def protected_fingerprints(config):
    from scl_audit import read_source, mask_noncode
    paths = json.loads(config.read_text(encoding="utf8"))
    if not isinstance(paths, list) or not paths or any(not isinstance(p, str) for p in paths):
        raise ValueError("source configuration must be a nonempty array of local directories")
    protected = set()
    count = 0
    for path in paths:
        root = Path(path)
        if not root.is_dir():
            raise ValueError("a protected source directory is unavailable")
        sources = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".scl", ".awl", ".st"})
        if not sources:
            raise ValueError("a protected source directory contains no source files")
        for source in sources:
            text = read_source(source)
            protected.update(fingerprints(text))
            protected.update(fingerprints(mask_noncode(text, strings=False)))
            count += 1
    if not protected:
        raise ValueError("empty source fingerprint database")
    return protected, count


def text_variants(text, path):
    yield text
    yield re.sub(r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2}))", lambda m: chr(int(m[1] or m[2], 16)), text)
    # Python literal values expose escaped strings without executing any code.
    if path.endswith(".py"):
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                yield node.value
            if isinstance(node, ast.Constant) and isinstance(node.value, bytes):
                yield node.value.decode("utf8", errors="replace")
    # Also inspect conventional encodings inside any approved text format.
    for match in re.finditer(r"[A-Za-z0-9+/]{16,}={0,2}", text):
        try:
            raw = base64.b64decode(match[0], validate=True)
            for encoding in ("utf8", "utf-16-le", "cp1251"):
                try:
                    yield raw.decode(encoding)
                except UnicodeError:
                    pass
        except ValueError:
            pass
    for match in re.finditer(r"\b(?:[0-9a-fA-F]{2}){16,}\b", text):
        raw = bytes.fromhex(match[0])
        for encoding in ("utf8", "utf-16-le", "cp1251"):
            try:
                yield raw.decode(encoding)
            except UnicodeError:
                pass


def inspect_blob(path, data, protected, mode="100644"):
    errors = []
    if path not in PUBLIC_PATHS:
        errors.append("path is not explicitly approved for publication")
    if mode not in {"100644", "100755"}:
        errors.append("symlinks/submodules are not permitted")
    if len(data) > MAX_BLOB or b"\0" in data:
        return errors + ["binary or oversized payload is not permitted"]
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        return errors + ["only reviewable UTF-8 text is permitted"]
    if ENCODED.search(text):
        errors.append("long encoded payload requires removal or separate review")
    try:
        for variant in text_variants(text, path):
            if DECLARATION.search(variant) or ASSIGNMENT.search(variant) or FIELD_DECLARATION.search(variant):
                errors.append("embedded SCL syntax detected")
                break
            if protected and fingerprints(variant) & protected:
                errors.append("fragment matches protected local source")
                break
    except (SyntaxError, ValueError):
        errors.append("text/literal analysis failed")
    return sorted(set(errors))


def index_entries(cwd=ROOT):
    entries = []
    for row in git("ls-files", "--stage", "-z", cwd=cwd).split(b"\0"):
        if not row:
            continue
        metadata, path = row.split(b"\t", 1)
        mode, oid, stage = metadata.decode().split()
        if stage != "0":
            raise ValueError("unmerged index")
        entries.append((path.decode("utf8"), oid, mode))
    return entries


def revision_entries(revision, history=False, cwd=ROOT):
    commit = git("rev-parse", "--verify", revision + "^{commit}", cwd=cwd).decode().strip()
    commits = git("rev-list", commit, cwd=cwd).decode().splitlines() if history else [commit]
    entries = set()
    for commit in commits:
        for row in git("ls-tree", "-r", "-z", commit, cwd=cwd).split(b"\0"):
            if row:
                metadata, path = row.split(b"\t", 1)
                mode, kind, oid = metadata.decode().split()
                if kind != "blob":
                    raise ValueError("non-blob tree entry is not permitted")
                entries.add((path.decode("utf8"), oid, mode))
    return sorted(entries)


def check_entries(entries, protected, cwd=ROOT):
    failed = []
    for path, oid, mode in sorted(set(entries)):
        size = int(git("cat-file", "-s", oid, cwd=cwd))
        errors = ["oversized blob"] if size > MAX_BLOB else inspect_blob(path, git("cat-file", "blob", oid, cwd=cwd), protected, mode)
        if errors:
            failed.append((path, errors))
    return failed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--index", action="store_true")
    mode.add_argument("--revision")
    mode.add_argument("--pre-push", action="store_true")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--structural-only", action="store_true", help="CI only; not sufficient to authorize a push")
    args = parser.parse_args()
    if args.pre_push and args.structural_only:
        parser.error("push requires protected-source verification")
    try:
        if args.index:
            entries = index_entries()
        elif args.pre_push:
            entries = []
            for line in sys.stdin:
                local_ref, local_oid, remote_ref, remote_oid = line.split()
                if set(local_oid) != {"0"}:
                    entries.extend(revision_entries(local_oid, history=True))
        else:
            entries = revision_entries(args.revision, args.history)
        protected, source_count = (set(), 0) if args.structural_only else protected_fingerprints(ROOT / ".publish-sources.json")
        failures = check_entries(entries, protected)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("Publication blocked: verification could not complete (" + type(error).__name__ + ").", file=sys.stderr)
        return 1
    if failures:
        for path, errors in failures:
            print(f"BLOCKED {path}: {'; '.join(errors)}", file=sys.stderr)
        return 1
    label = "CI structural-only" if args.structural_only else f"strict, {source_count} protected source files"
    print(f"Publication check passed: {len(set(entries))} blob/path versions ({label}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
