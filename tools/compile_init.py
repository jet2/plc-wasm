"""Experimental SCL -> Wasm front-end for scalar initialization only.

Rejects every unsupported declaration/statement. Does NOT compile obMain,
produce S7 DB layout, or claim to compile a complete station.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from scl_audit import mask_noncode, read_source

TYPES = {"BOOL": (0, 1), "BYTE": (0, 255), "WORD": (0, 65535),
         "DWORD": (0, 4294967295), "INT": (-32768, 32767), "DINT": (-2147483648, 2147483647)}


def literal(text: str, kind: str) -> int:
    text = text.strip().upper()
    if text in {"TRUE", "FALSE"}:
        if kind != "BOOL":
            raise ValueError(f"boolean literal assigned to {kind}")
        return int(text == "TRUE")
    if not re.fullmatch(r"[+-]?\d+", text):
        raise ValueError(f"unsupported literal: {text}")
    value = int(text)
    lo, hi = TYPES[kind]
    if not lo <= value <= hi:
        raise ValueError(f"literal {value} outside {kind} range")
    return value


def compile_sources(udt: str, db: str, source: str, name: str) -> tuple[dict, str]:
    """Compile the single MPR_Params instance and parInit function."""
    clean_udt = mask_noncode(udt, strings=False)
    type_match = re.search(r"\bTYPE\s+TMPR_Params\s*:\s*STRUCT\b(.*?)\bEND_STRUCT\s*;\s*END_TYPE\b",
                           clean_udt, re.I | re.S)
    if not type_match:
        raise ValueError("TMPR_Params structure not found")
    fields, defaults = [], []
    seen = set()
    for declaration in type_match[1].split(";"):
        if not declaration.strip():
            continue
        m = re.fullmatch(r"\s*(\w+)\s*:\s*(\w+)\s*(?::=\s*(.*?))?\s*", declaration, re.S)
        if not m or m[2].upper() not in TYPES:
            raise ValueError(f"unsupported TMPR_Params declaration: {declaration.strip()}")
        key = m[1].lower()
        if key in seen:
            raise ValueError(f"duplicate field {m[1]}")
        seen.add(key)
        kind = m[2].upper()
        fields.append({"name": f"MPR_Params.{m[1]}", "offset": len(fields) * 4, "scl_type": kind})
        defaults.append(literal(m[3], kind) if m[3] else 0)
    if not fields:
        raise ValueError("empty TMPR_Params")
    # This milestone does not implement DB initial-value overrides.
    clean_db = mask_noncode(db, strings=False)
    instance = re.search(r"\bDATA_BLOCK\s+MPR_Params\b(.*?)\bEND_DATA_BLOCK\b", clean_db, re.I | re.S)
    if not instance or not re.fullmatch(r"\s*TMPR_Params\s*;\s*BEGIN\s*", instance[1], re.I):
        raise ValueError("expected MPR_Params TMPR_Params with no DB overrides")
    clean = mask_noncode(source, strings=False)
    function = re.fullmatch(r"\s*FUNCTION\s+parInit\s*:\s*VOID\s+BEGIN\b(.*?)\bEND_FUNCTION\s*", clean, re.I | re.S)
    if not function:
        raise ValueError("unsupported initialization function declaration or body")
    by_name = {f["name"].lower(): f for f in fields}
    instructions = []
    for field, value in zip(fields, defaults):
        instructions.append(f'(i32.store (i32.const {field["offset"]}) (i32.const {value}))')
    body = function[1]
    position = 0
    while body[position:].strip():
        m = re.match(r"\s*([A-Za-z_]\w*\.[A-Za-z_]\w*)\s*:=\s*([^;]+);", body[position:])
        if not m:
            line = clean.count("\n", 0, function.start(1) + position) + 1
            raise ValueError(f"unsupported statement near source line {line}: {body[position:position+80].strip()}")
        target = by_name.get(m[1].lower())
        if not target:
            raise ValueError(f"unknown assignment target: {m[1]}")
        rhs = m[2].strip()
        other = by_name.get(rhs.lower())
        if other:
            if other["scl_type"] != target["scl_type"]:
                raise ValueError("implicit conversion between fields is not implemented")
            expression = f'(i32.load (i32.const {other["offset"]}))'
        else:
            expression = f'(i32.const {literal(rhs, target["scl_type"])})'
        instructions.append(f'(i32.store (i32.const {target["offset"]}) {expression})')
        position += m.end()
    state_size = len(fields) * 4
    pages = (state_size + 65535) // 65536
    wat = '(module\n'
    wat += f'  (memory (export "memory") {pages} {pages})\n'
    wat += '  (func (export "abi_version") (result i32) (i32.const 1))\n'
    wat += '  (func (export "init") (result i32)\n'
    wat += "\n".join("    " + i for i in instructions)
    wat += '\n    (i32.const 0))\n'
    # Explicit smoke-only no-op scan; the station's cycle is NOT compiled here.
    wat += '  (func (export "scan") (param i32) (result i32) (i32.const 0))\n)\n'
    digest = hashlib.sha256()
    for text in (udt, db, source):
        data = text.encode("utf-8")
        digest.update(len(data).to_bytes(8, "little"))
        digest.update(data)
    manifest = {"abi_version": 1, "name": name, "kind": "initializer-smoke",
                "memory_bytes": pages * 65536, "fuel_per_call": 100000,
                "inputs": {"offset": 0, "size": 0},
                "outputs": {"offset": 0, "size": state_size}, "fields": fields,
                "source_sha256": digest.hexdigest()}
    return manifest, wat


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, help="STEP 7 Program directory")
    parser.add_argument("--name", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--runtime", type=Path, help="built wplc executable; also produce program.pkg")
    args = parser.parse_args()
    directory = args.project / "Source"
    manifest, wat = compile_sources(*(read_source(directory / f) for f in ("MPR_UDT.SCL", "MPR_DB.SCL", "parInit.SCL")), args.name)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    wat_path = args.output / "program.wat"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    wat_path.write_text(wat, encoding="utf-8")
    if args.runtime:
        subprocess.run([str(args.runtime.resolve()), "pack", str(manifest_path), str(wat_path), str(args.output / "program.pkg")], check=True)
    print(f"{args.name}: compiled parInit, {len(manifest['fields'])} scalar fields (smoke only)")


if __name__ == "__main__":
    main()
