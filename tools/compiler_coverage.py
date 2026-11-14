"""Report parser/codegen acceptance, NOT semantic equivalence with STEP 7."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

from compile_program import load_project
from scl_codegen import Compiler
from scl_frontend import CompileError, Parser


def inspect(path, runtime=None):
    project = load_project(path)
    parsed, parse_errors, generated, codegen_errors = [], {}, [], {}
    validated, validation_errors = [], {}
    for filename, source in project.sources.items():
        if filename.upper() in {"MPR_UDT.SCL", "MPR_DB.SCL"}:
            continue
        name = Path(filename).stem
        try:
            Parser(source, filename).function()
            parsed.append(name)
        except CompileError as error:
            parse_errors[name] = str(error)
            continue
        try:
            manifest, wat, _ = Compiler(project).compile(name)
            generated.append(name)
        except CompileError as error:
            codegen_errors[name] = str(error)
            continue
        if runtime:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
                (output / "program.wat").write_text(wat, encoding="utf8")
                run = subprocess.run([str(runtime), "pack", str(output / "manifest.json"),
                                      str(output / "program.wat"), str(output / "program.pkg")], capture_output=True, text=True)
                if run.returncode == 0:
                    validated.append(name)
                else:
                    validation_errors[name] = run.stderr
    return {"root": str(path), "type_count": len(project.types.specs), "db_count": len(project.dbs),
            "parsed_count": len(parsed), "generated_count": len(generated),
            "parsed": parsed, "generated": generated,
            "parse_errors": parse_errors, "codegen_errors": codegen_errors,
            "validated_count": len(validated), "validated": validated, "validation_errors": validation_errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", action="append", required=True, help="NAME=PATH of a local project")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, help="also validate every emitted module using the native runtime pack command")
    args = parser.parse_args()
    paths = dict(p.split("=", 1) for p in args.project)
    result = {"method": "parse and emit WAT per entry with reachable dependencies; optional pack/load/init validation; no scan execution or PLC comparison", "projects": {}}
    for name, path in paths.items():
        report = inspect(Path(path), args.runtime.resolve() if args.runtime else None)
        result["projects"][name] = report
        print(name, "parsed", report["parsed_count"], "generated", report["generated_count"], "validated", report["validated_count"], flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf8")


if __name__ == "__main__":
    main()
