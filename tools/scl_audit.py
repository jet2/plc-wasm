"""Reproducible lexical inventory, not a full SCL parser or proof of completeness."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import re


def read_source(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251")


def mask_noncode(text: str, strings: bool = True) -> str:
    """Preserve offsets/newlines; understand nested comments and doubled quotes."""
    out = list(text)
    i = 0
    while i < len(text):
        start = i
        if text.startswith("(*", i):
            depth = 1
            i += 2
            while i < len(text) and depth:
                if text.startswith("(*", i):
                    depth += 1
                    i += 2
                elif text.startswith("*)", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                raise ValueError("unterminated SCL comment")
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
        elif text[i] in "'\"":
            quote = text[i]
            i += 1
            while i < len(text):
                if text[i] == quote:
                    i += 1
                    if i < len(text) and text[i] == quote:
                        i += 1
                    else:
                        break
                else:
                    i += 1
            else:
                raise ValueError("unterminated SCL string/quoted identifier")
            if not strings:
                continue
        else:
            i += 1
            continue
        for j in range(start, i):
            if text[j] not in "\r\n":
                out[j] = " "
    return "".join(out)


DECL = re.compile(r"\b(FUNCTION_BLOCK|FUNCTION|ORGANIZATION_BLOCK|DATA_BLOCK|TYPE)\s+(\w+)", re.I)
CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
NOT_CALLS = set("IF ELSIF WHILE UNTIL CASE FOR NOT AND OR XOR MOD RETURN ARRAY STRING".split())
BUILTINS = set("ABS SQRT EXP LN LOG SIN COS TAN ASIN ACOS ATAN TRUNC ROUND CEIL FLOOR SHL SHR ROL ROR SEL LIMIT MIN MAX".split())
SCALARS = set("BOOL BYTE WORD DWORD INT DINT REAL CHAR STRING TIME DATE TIME_OF_DAY DATE_AND_TIME S5TIME".split())
SYSTEM = {"DIS_IRT", "EN_IRT", "READ_CLK", "SET_CLK", "TIME_TCK", "BLKMOV", "DPRD_DAT", "DPWR_DAT"}
FEATURES = {
    "if": r"\bIF\b", "for": r"\bFOR\b", "while": r"\bWHILE\b",
    "repeat": r"\bREPEAT\b", "case": r"\bCASE\b", "return": r"\bRETURN\b",
    "exit": r"\bEXIT\b", "continue": r"\bCONTINUE\b", "goto": r"\bGOTO\b",
    "overlay_at": r"\b\w+\s+AT\s+\w+\s*:", "array": r"\bARRAY\s*\[",
    "struct": r"\bSTRUCT\b", "var_temp": r"\bVAR_TEMP\b",
    "var_in_out": r"\bVAR_IN_OUT\b", "absolute_pointer": r"\bP#",
    "named_argument_or_assignment": r":=", "output_argument": r"=>",
    "hex_literal": r"\b16#[0-9a-f_]+", "binary_literal": r"\b2#[01_]+",
}


def location(code: str, pos: int, file: str) -> dict:
    return {"file": file, "line": code.count("\n", 0, pos) + 1}


def symbols(path: Path) -> list[dict]:
    if not path.exists():
        return []
    result = []
    # STEP 7 ASC uses fixed-width symbol (24), address (12), type (10).
    for line in read_source(path).splitlines():
        if not line.startswith(("126,", "136,")) or len(line) < 40:
            continue
        result.append({"name": line[4:28].strip(), "address": line[28:40].strip(),
                       "type": line[40:50].strip()})
    return result


def project_report(root: Path) -> dict:
    files = sorted((root / "Source").glob("*"), key=lambda p: p.name.lower())
    files = [p for p in files if p.suffix.lower() == ".scl"]
    if not files:
        raise ValueError(f"no SCL sources: {root}")
    declarations, calls, features, types, records = [], [], defaultdict(list), Counter(), []
    for path in files:
        raw = path.read_bytes()
        code = mask_noncode(read_source(path))
        records.append({"file": path.name, "bytes": len(raw), "lines": len(code.splitlines()),
                        "sha256": hashlib.sha256(raw).hexdigest()})
        for m in DECL.finditer(code):
            declarations.append({"kind": m[1].upper(), "name": m[2], **location(code, m.start(), path.name)})
        for m in CALL.finditer(code):
            if m[1].upper() not in NOT_CALLS:
                calls.append({"name": m[1].upper(), **location(code, m.start(), path.name)})
        for name, pattern in FEATURES.items():
            features[name].extend(location(code, m.start(), path.name) for m in re.finditer(pattern, code, re.I))
        types.update(t.upper() for t in re.findall(r"\b\w+\b", code) if t.upper() in SCALARS)
    defined = {d["name"].upper() for d in declarations if d["kind"] in {"FUNCTION", "FUNCTION_BLOCK"}}
    external = defaultdict(list)
    for call in calls:
        name = call["name"]
        if name not in defined:
            external[name].append({k: v for k, v in call.items() if k != "name"})
    def category(name):
        parts = name.split("_TO_")
        if name in BUILTINS or (len(parts) == 2 and all(p in SCALARS for p in parts)):
            return "intrinsic_candidate"
        return "system_function" if name in SYSTEM else "external_or_unresolved"
    inp = root / "Source" / "zSCL_CompileFile.INP"
    compile_list = []
    if inp.exists():
        compile_list = [s.strip() for s in mask_noncode(read_source(inp)).splitlines()
                        if re.fullmatch(r"\s*[A-Za-z_]\w*\s*", s)]
    file_names = {p.stem.lower() for p in files}
    sym = symbols(root / "Tables" / "SymbolTable.asc")
    table_reports = {}
    for path in sorted((root / "Tables").glob("*.csv")):
        rows = list(csv.reader(read_source(path).splitlines(), delimiter=";" if "OPC" in path.name else ","))
        addresses = []
        for row in rows:
            for cell in row:
                if re.match(r"DB\d+,", cell, re.I) or cell.startswith("S7:["):
                    addresses.append(cell)
        table_reports[path.name] = {"rows": len(rows), "address_count": len(addresses),
                                   "address_examples": addresses[:8]}
    return {
        "root": str(root), "source_count": len(files), "total_lines": sum(f["lines"] for f in records),
        "files": records, "declarations": declarations,
        "declaration_counts": dict(Counter(d["kind"] for d in declarations)),
        "features": {k: {"count": len(v), "sites": v} for k, v in features.items()},
        "scalar_token_counts": dict(sorted(types.items())),
        "calls": calls,
        "external_calls": {k: {"category": category(k), "sites": v} for k, v in sorted(external.items())},
        "compile_list": compile_list,
        "missing_compile_sources": [n for n in compile_list if n.lower() not in file_names],
        "sources_not_in_compile_list": [p.name for p in files if p.stem.lower() not in {n.lower() for n in compile_list}],
        "symbols": sym, "tables": table_reports,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", action="append", metavar="NAME=PATH", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--details", type=Path, help="optional full call/site/symbol inventory")
    args = parser.parse_args()
    projects = dict(item.split("=", 1) for item in args.project)
    reports = {name: project_report(Path(path)) for name, path in projects.items()}
    by_name = defaultdict(dict)
    for name, report in reports.items():
        for f in report["files"]:
            by_name[f["file"].lower()][name] = f["sha256"]
    common = {name: hashes for name, hashes in by_name.items() if len(hashes) == len(reports)}
    result = {"schema_version": 1, "method": "lexical inventory; candidates require semantic resolution",
              "projects": reports,
              "comparison": {"common_filenames": len(common),
                             "identical_in_all": sorted(n for n, h in common.items() if len(set(h.values())) == 1),
                             "different_in_all_or_some": sorted(n for n, h in common.items() if len(set(h.values())) > 1)}}
    if args.details:
        args.details.parent.mkdir(parents=True, exist_ok=True)
        args.details.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for report in reports.values():
        report["call_count"] = len(report.pop("calls"))
        symbol_list = report.pop("symbols")
        report["symbol_count"] = len(symbol_list)
        report["symbol_type_counts"] = dict(Counter(s["type"] for s in symbol_list))
        report["db_symbols"] = [s for s in symbol_list if re.match(r"DB\s+\d+$", s["address"], re.I)]
        for feature in report["features"].values():
            feature["examples"] = feature.pop("sites")[:5]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, r in reports.items():
        print(name, r["source_count"], "sources;", r["total_lines"], "lines;", r["declaration_counts"])
        print("  external calls:", ", ".join(r["external_calls"]))
        print("  missing compile sources:", r["missing_compile_sources"])
    print("Common filenames:", len(common), "identical:", len(result["comparison"]["identical_in_all"]))


if __name__ == "__main__":
    main()
