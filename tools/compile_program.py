"""Compile a reachable integer SCL function graph to an offline ABI v1 package."""
import argparse
import json
from pathlib import Path
import re
import subprocess

from scl_audit import read_source, symbols
from scl_codegen import Compiler, Project
from scl_frontend import CompileError


def load_project(directory):
    sources = {p.name: read_source(p) for p in sorted((directory / "Source").iterdir()) if p.suffix.lower() == ".scl"}
    scalar_symbols = {s["name"].upper(): s["type"].upper() for s in symbols(directory / "Tables" / "SymbolTable.asc")
                      if re.fullmatch(r"[IQMEAB](?:[BWD])?\s*\d+(?:\.\d+)?", s["address"], re.I)}
    return Project(sources, scalar_symbols)


def write_build(compiler, output, runtime=None, **options):
    manifest, wat, report = compiler.compile(**options)
    output.mkdir(parents=True, exist_ok=True)
    for filename, data in [("manifest.json", manifest), ("build.json", report)]:
        (output / filename).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf8")
    (output / "program.wat").write_text(wat, encoding="utf8")
    if runtime:
        subprocess.run([str(runtime.resolve()), "pack", str(output / "manifest.json"),
                        str(output / "program.wat"), str(output / "program.pkg")], check=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--entry", required=True, help="function called on every scan")
    parser.add_argument("--init", action="append", default=[], help="initialization function, repeat to preserve call order")
    parser.add_argument("--set", dest="presets", action="append", default=[], help="local test state after initialization")
    parser.add_argument("--watch", action="append", default=[], help="scalar path to expose in offline JSON results")
    parser.add_argument("--name", default="scl-cycle")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--runtime", type=Path)
    args = parser.parse_args()
    try:
        report = write_build(Compiler(load_project(args.project)), args.output, args.runtime,
                             entry=args.entry, init=args.init, presets=args.presets, watches=args.watch, name=args.name)
    except CompileError as error:
        parser.exit(2, str(error) + "\n")
    print(f"Compiled {len(report['functions'])} functions, {len(report['globals'])} globals, {report['state_bytes']} state bytes")


if __name__ == "__main__":
    main()
