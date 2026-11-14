"""Selected SCL constructs -> Core Wasm, using experimental ABI v1.

Internal layout is deliberately NOT S7 DB layout. No external calls are stubbed.
"""
from __future__ import annotations
from dataclasses import dataclass, field, replace
import hashlib
import re
import struct
import math
from scl_frontend import CompileError, Node, Parser, TypeSpec, constant, walk
from scl_support import SUPPORT_WAT

RANGES = {"BOOL": (0, 1), "BYTE": (0, 255), "WORD": (0, 65535),
          "DWORD": (0, 4294967295), "INT": (-32768, 32767), "DINT": (-2147483648, 2147483647)}
SIGNED = {"INT", "DINT"}
BITS = {"BYTE", "WORD", "DWORD"}
MAX_MEMORY = 16 * 1024 * 1024


def string_bytes(text, capacity, token):
    if '$' in text:
        token.fail('SCL dollar escapes in STRING literals are not implemented')
    try:
        raw = text.encode('cp1251')
    except UnicodeEncodeError:
        token.fail('STRING character is not representable in Windows-1251')
    if len(raw) > capacity:
        token.fail('STRING literal exceeds capacity')
    size = (capacity + 5) // 4 * 4
    return bytes([capacity, len(raw)]) + raw + bytes(size - len(raw) - 2)


def real_constant(node):
    if node.op == 'typed' and node.args[0] == 'REAL':
        return real_constant(node.args[1])
    if node.op == 'unary' and node.args[0] in {'+', '-'}:
        value = real_constant(node.args[1])
        return value if node.args[0] == '+' else -value
    if node.op == 'literal':
        try:
            value = float(node.args[0].replace('_', ''))
            value = struct.unpack('<f', struct.pack('<f', value))[0]
            if math.isfinite(value):
                return value
        except (ValueError, OverflowError):
            pass
    node.token.fail('expected finite REAL constant')


def packed_layout(layout, token):
    """Byte-addressable S7 big-endian layout for local AT groups only."""
    widths = {'BYTE': 1, 'WORD': 2, 'INT': 2, 'DWORD': 4, 'DINT': 4, 'REAL': 4}
    if layout.kind in widths:
        size = widths[layout.kind]
        return replace(layout, size=size, data=bytes(size))
    if layout.kind == 'ARRAY':
        element = packed_layout(layout.element, token)
        size = element.size * (layout.high - layout.low + 1)
        if size % 2:
            token.fail('AT overlay with odd-sized arrays is not implemented')
        return replace(layout, element=element, size=size, data=bytes(size))
    if layout.kind == 'STRUCT':
        fields, size = {}, 0
        for key, (name, _, child) in layout.fields.items():
            child = packed_layout(child, token)
            # S7 aligns non-byte scalar fields and aggregates to word boundaries.
            if child.kind != 'BYTE':
                size = (size + 1) // 2 * 2
            fields[key] = (name, size, child)
            size += child.size
        size = (size + 1) // 2 * 2
        return replace(layout, fields=fields, size=size, data=bytes(size))
    token.fail('AT overlay supports byte-addressable numeric types only')


@dataclass
class Layout:
    kind: str
    size: int
    data: bytes
    fields: dict = field(default_factory=dict)
    element: Layout | None = None
    low: int = 0
    high: int = 0


def scalar_bytes(value, kind, token):
    lo, hi = RANGES[kind]
    if not lo <= value <= hi:
        token.fail(f"constant {value} is outside {kind} range")
    return struct.pack("<I", value & 0xffffffff)


class Types:
    def __init__(self, specs):
        self.specs, self.cache, self.active = specs, {}, set()

    def resolve(self, spec):
        if spec.kind == "named":
            name = spec.args[0]
            if name in RANGES:
                return Layout(name, 4, bytes(4))
            if name == "REAL":
                return Layout("REAL", 4, bytes(4))
            if name in self.cache:
                return self.cache[name]
            if name in self.active:
                spec.token.fail(f"recursive type {name}")
            if name not in self.specs:
                spec.token.fail(f"unsupported or unknown type {name}")
            self.active.add(name)
            try:
                layout = self.resolve(self.specs[name])
                self.cache[name] = layout
                return layout
            finally:
                self.active.remove(name)
        if spec.kind == "struct":
            fields, data = {}, bytearray()
            for declaration in spec.args[0]:
                if declaration.overlay:
                    declaration.token.fail("AT overlay code generation is not implemented")
                name = declaration.name.upper()
                if name in fields:
                    declaration.token.fail(f"duplicate field {name}")
                layout = self.with_initial(self.resolve(declaration.spec), declaration.initial)
                if len(data) + layout.size > MAX_MEMORY:
                    declaration.token.fail("structure exceeds memory limit")
                fields[name] = (declaration.name, len(data), layout)
                data.extend(layout.data)
            return Layout("STRUCT", len(data), bytes(data), fields=fields)
        if spec.kind == "array":
            bounds, element_spec = spec.args
            element = self.resolve(element_spec)
            for low_expr, high_expr in reversed(bounds):
                low, high = constant(low_expr), constant(high_expr)
                if not -2147483648 <= low <= high <= 2147483647:
                    spec.token.fail("invalid array bounds")
                size = (high - low + 1) * element.size
                if size > MAX_MEMORY:
                    spec.token.fail("array exceeds memory limit")
                element = Layout("ARRAY", size, element.data * (high - low + 1), element=element, low=low, high=high)
            return element
        if spec.kind == "string":
            maximum = constant(spec.args[0]) if spec.args[0] else 254
            if not 0 <= maximum <= 254:
                spec.token.fail("invalid STRING capacity")
            size = (maximum + 2 + 3) // 4 * 4
            return Layout("STRING", size, bytes([maximum]) + bytes(size - 1))
        spec.token.fail(f"code generation for {spec.kind} is not implemented")

    def with_initial(self, layout, expressions):
        if not expressions:
            return layout
        if layout.kind in RANGES:
            if len(expressions) != 1:
                expressions[0].token.fail("scalar requires one initializer")
            return replace(layout, data=scalar_bytes(constant(expressions[0]), layout.kind, expressions[0].token))
        if layout.kind == 'REAL' and len(expressions) == 1:
            return replace(layout, data=struct.pack('<f', real_constant(expressions[0])))
        if layout.kind == "STRING" and len(expressions) == 1 and expressions[0].op == "string":
            return replace(layout, data=string_bytes(expressions[0].args[0], layout.data[0], expressions[0].token))
        if layout.kind == "ARRAY" and layout.element.kind in RANGES:
            if len(expressions) > layout.high - layout.low + 1:
                expressions[0].token.fail("too many array initializers")
            data = bytearray(layout.data)
            for i, expr in enumerate(expressions):
                data[i*4:i*4+4] = scalar_bytes(constant(expr), layout.element.kind, expr.token)
            return replace(layout, data=bytes(data))
        expressions[0].token.fail("aggregate initializer is not supported for this type")


@dataclass
class Place:
    kind: str
    value: str
    layout: Layout
    token: object
    valid: str | None = None
    packed: bool = False


def normalize(code, kind):
    if kind == "INT":
        return f"(i32.shr_s (i32.shl {code} (i32.const 16)) (i32.const 16))"
    if kind in {"BYTE", "WORD"}:
        return f"(i32.and {code} (i32.const {RANGES[kind][1]}))"
    return code


class Project:
    def __init__(self, sources, scalar_symbols=None):
        self.sources = sources
        self.by_file = {name.upper(): (name, text) for name, text in sources.items()}
        self.types = Types(Parser(sources["MPR_UDT.SCL"], "MPR_UDT.SCL").types())
        self.dbs = Parser(sources["MPR_DB.SCL"], "MPR_DB.SCL").data_blocks()
        self.scalars = scalar_symbols or {}
        self.functions = {}

    def function(self, name, token=None):
        key = name.upper()
        if key not in self.functions:
            source = self.by_file.get(key + ".SCL")
            if not source:
                if token:
                    token.fail(f"unresolved external function {name}; no stub is allowed")
                raise CompileError(f"function source not found: {name}")
            function = Parser(source[1], source[0]).function()
            if function.name.upper() != key:
                function.token.fail("filename does not match function name")
            if function.result != "VOID":
                function.token.fail("only VOID user functions are implemented")
            self.functions[key] = function
        return self.functions[key]


CONVERSIONS = {"INT_TO_DINT": ("INT", "DINT"), "DINT_TO_INT": ("DINT", "INT"),
               "DWORD_TO_DINT": ("DWORD", "DINT")}
BUILTINS = set(CONVERSIONS) | {'LEN', 'CONCAT', 'DINT_TO_STRING', 'SHR', 'DIS_IRT'}


class Compiler:
    def __init__(self, project, system_profile='strict'):
        if system_profile not in {'strict', 'cyclic-sim'}:
            raise ValueError('unknown system profile')
        self.system_profile = system_profile
        self.project = project
        self.functions = {}
        self.globals = {}
        self.initial_data = bytearray()
        self.needed_roots = {}

    def reserve(self, data):
        offset = (len(self.initial_data) + 3) // 4 * 4
        if offset + len(data) > MAX_MEMORY:
            raise CompileError('program and scratch storage exceed memory limit')
        self.initial_data.extend(bytes(offset - len(self.initial_data)))
        self.initial_data.extend(data)
        return offset

    def dependencies(self, name, active=None, token=None):
        key = name.upper()
        active = set() if active is None else active
        if key in active:
            self.project.function(key).token.fail("recursive call graph is not supported")
        if key in self.functions:
            return
        function = self.project.function(key, token)
        active.add(key)
        local_names = {d.name.upper() for d in function.locals}
        for node in walk(function.body):
            if node.op == "ref" and node.args[0].upper() not in local_names:
                self.needed_roots[node.args[0].upper()] = node.token
            if node.op == "call" and len(node.args) == 2:
                called, args = node.args
                if called.upper() not in BUILTINS:
                    if args:
                        node.token.fail(f"parameterized external call {called} is not implemented")
                    self.dependencies(called, active, node.token)
        active.remove(key)
        self.functions[key] = function

    def allocate(self):
        for name, token in sorted(self.needed_roots.items()):
            if name in self.project.dbs:
                _, spec, overrides = self.project.dbs[name]
                if overrides:
                    overrides[0].token.fail("DB value overrides are not implemented")
                layout = self.project.types.resolve(spec)
            elif name in self.project.scalars:
                layout = self.project.types.resolve(TypeSpec("named", (self.project.scalars[name],), token))
            else:
                token.fail(f"unresolved global variable {name}")
            if len(self.initial_data) + layout.size > MAX_MEMORY:
                token.fail("program state exceeds memory limit")
            self.globals[name] = (len(self.initial_data), layout)
            self.initial_data.extend(layout.data)

    def compile(self, entry, init=(), presets=(), watches=(), name="program", fuel_per_call=10000000):
        if type(fuel_per_call) is not int or not 1 <= fuel_per_call <= 100000000:
            raise CompileError('fuel_per_call must be between 1 and 100000000')
        # A Compiler instance is deliberately single-use.
        if self.functions or self.globals:
            raise ValueError("create a new Compiler for each build")
        for function in [*init, entry]:
            self.dependencies(function)
        parsed_sets = []
        for text in presets:
            parser = Parser(text + ";", "<preset>")
            stmt = parser.statement()
            parser.expect("<EOF>")
            if stmt.op != "assign":
                stmt.token.fail("preset must be an assignment")
            constant(stmt.args[1])
            parsed_sets.append(stmt)
            self.needed_roots[stmt.args[0].args[0].upper()] = stmt.token
        parsed_watches = []
        for text in watches:
            parser = Parser(text, "<watch>")
            node = parser.expression()
            parser.expect("<EOF>")
            if node.op != "ref":
                node.token.fail("watch must be a variable")
            self.needed_roots[node.args[0].upper()] = node.token
            parsed_watches.append((text, node))
        self.allocate()
        self.system_offset = self.reserve(bytes(4)) if self.system_profile == 'cyclic-sim' else None
        state_size = len(self.initial_data)
        self.ids = {name: f"$f{i}" for i, name in enumerate(self.functions)}
        generated = [FunctionEmitter(self, fn).emit() for fn in self.functions.values()]
        # Presets are explicit test state, applied AFTER source initialization calls.
        helper = FunctionEmitter(self, None)
        set_code = "\n".join(helper.statement(s) for s in parsed_sets)
        fields = []
        if self.system_offset is not None:
            fields.append({'name': 'runtime.async_errors_disabled', 'offset': self.system_offset, 'scl_type': 'DWORD'})
        for text, node in parsed_watches:
            place = helper.place(node, static=True)
            if place.layout.kind not in RANGES:
                node.token.fail("watch must select a supported scalar")
            fields.append({"name": text, "offset": int(place.value), "scl_type": place.layout.kind})
        size = len(self.initial_data)
        pages = max(1, (size + 65535) // 65536)
        data = "".join(f"\\{b:02x}" for b in self.initial_data)
        initializer = "\n".join(f"(call {self.ids[n.upper()]})" for n in init)
        wat = f'''(module
          (memory (export "memory") {pages} {pages})
          (data (i32.const 0) "{data}")
          (func $index (param $i i32) (param $lo i32) (param $hi i32) (result i32)
            (if (i32.or (i32.lt_s (local.get $i) (local.get $lo))
                        (i32.gt_s (local.get $i) (local.get $hi))) (then unreachable))
            (i32.sub (local.get $i) (local.get $lo)))
          (func (export "abi_version") (result i32) (i32.const 1))
          {SUPPORT_WAT}
          {chr(10).join(generated)}
          (func (export "init") (result i32) {helper.local_declarations()}
            {initializer} {set_code} (i32.const 0))
          (func (export "scan") (param i32) (result i32)
            (call {self.ids[entry.upper()]}) (i32.const 0)))
        '''
        digest = hashlib.sha256()
        used = ["MPR_UDT.SCL", "MPR_DB.SCL"] + [self.project.by_file[n + ".SCL"][0] for n in self.functions]
        for filename in sorted(used):
            raw = (filename + "\0" + self.project.sources[filename]).encode("utf8")
            digest.update(len(raw).to_bytes(8, "little")); digest.update(raw)
        manifest = {"abi_version": 1, "name": name, "kind": "cycle-smoke",
                    "memory_bytes": pages * 65536, "fuel_per_call": fuel_per_call,
                    "inputs": {"offset": 0, "size": 0}, "outputs": {"offset": 0, "size": state_size},
                    "fields": fields, "source_sha256": digest.hexdigest()}
        build = {"entry": entry, "init": list(init), "functions": sorted(self.functions),
                 "globals": {n: {"offset": o, "size": l.size} for n, (o, l) in self.globals.items()},
                 "state_bytes": state_size, "scratch_bytes": size - state_size,
                 "system_profile": self.system_profile, "presets": list(presets), "watches": list(watches),
                 "fuel_per_call": fuel_per_call,
                 "layout": "internal-i32-slots-not-S7", "source_sha256": digest.hexdigest()}
        return manifest, wat, build


class FunctionEmitter:
    def __init__(self, compiler, function):
        self.compiler, self.function = compiler, function
        self.locals, self.extra, self.loops = {}, [], []
        self.prologue = []
        if function:
            targets = {d.overlay.upper() for d in function.locals if d.overlay}
            for i, declaration in enumerate(function.locals):
                layout = compiler.project.types.resolve(declaration.spec)
                if declaration.initial:
                    declaration.token.fail("VAR_TEMP initializer semantics are not implemented")
                key = declaration.name.upper()
                if key in self.locals:
                    declaration.token.fail("duplicate local variable")
                if declaration.overlay:
                    base = self.locals.get(declaration.overlay.upper())
                    if base is None or not base.packed:
                        declaration.token.fail('AT overlay requires a preceding local storage target')
                    layout = packed_layout(layout, declaration.token)
                    if layout.size > base.layout.size:
                        declaration.token.fail('AT overlay exceeds target storage')
                    self.locals[key] = replace(base, layout=layout, token=declaration.token)
                elif key in targets or layout.kind not in {*RANGES, 'REAL'}:
                    packed = key in targets
                    if packed:
                        layout = packed_layout(layout, declaration.token)
                    offset = compiler.reserve(layout.data)
                    valid = compiler.reserve(bytes(layout.size))
                    self.prologue.append(f'(memory.fill (i32.const {valid}) (i32.const 0) (i32.const {layout.size}))')
                    self.locals[key] = Place('memory', f'(i32.const {offset})', layout, declaration.token,
                                             f'(i32.const {valid})', packed)
                else:
                    self.locals[key] = Place("local", f"$v{i}", layout, declaration.token, f"$valid{i}")

    def temp(self, kind="i32"):
        name = f"$t{len(self.extra)}"
        self.extra.append((name, kind))
        return name

    def local_declarations(self):
        return " ".join([f"(local {p.value} {'f32' if p.layout.kind == 'REAL' else 'i32'}) (local {p.valid} i32)" for p in self.locals.values() if p.kind == 'local']
                        + [f"(local {n} {t})" for n, t in self.extra])

    def emit(self):
        body = self.statements(self.function.body)
        return f"(func {self.compiler.ids[self.function.name.upper()]} {self.local_declarations()} {' '.join(self.prologue)} {body})"

    def place(self, node, static=False):
        if node.op != "ref":
            node.token.fail("expected variable reference")
        root, selectors = node.args
        valid, packed = None, False
        if root.upper() in self.locals:
            base = self.locals[root.upper()]
            if static:
                node.token.fail('local cannot be watched')
            if base.kind == 'local':
                if selectors:
                    node.token.fail('scalar local cannot have selectors')
                return base
            offset, address, layout, valid, packed = 0, base.value, base.layout, base.valid, base.packed
        else:
            if root.upper() not in self.compiler.globals:
                node.token.fail(f"unknown global {root}")
            offset, layout = self.compiler.globals[root.upper()]
            address = f"(i32.const {offset})"
        for kind, selector in selectors:
            if kind == "field":
                if layout.kind != "STRUCT" or selector.upper() not in layout.fields:
                    node.token.fail(f"unknown field {selector}")
                _, delta, layout = layout.fields[selector.upper()]
                offset += delta
                address = f"(i32.add {address} (i32.const {delta}))"
                if valid:
                    valid = f'(i32.add {valid} (i32.const {delta}))'
            else:
                if layout.kind != "ARRAY":
                    node.token.fail("index on non-array")
                if static or selector.op in {"literal", "unary", "typed"} and not any(n.op == "ref" for n in walk(selector)):
                    _, index_kind = self.expr(selector)
                    if index_kind not in SIGNED:
                        selector.token.fail("array index must be INT or DINT")
                    value = constant(selector)
                    if not layout.low <= value <= layout.high:
                        selector.token.fail(f"array index {value} outside {layout.low}..{layout.high}")
                    delta = (value - layout.low) * layout.element.size
                    offset += delta
                    address = f"(i32.add {address} (i32.const {delta}))"
                    if valid:
                        valid = f'(i32.add {valid} (i32.const {delta}))'
                else:
                    value, value_kind = self.expr(selector)
                    if value_kind not in SIGNED:
                        selector.token.fail("array index must be INT or DINT")
                    delta_code = f'(i32.mul (call $index {value} (i32.const {layout.low}) (i32.const {layout.high})) (i32.const {layout.element.size}))'
                    address = f"(i32.add {address} {delta_code})"
                    if valid:
                        valid = f'(i32.add {valid} {delta_code})'
                layout = layout.element
        return Place("memory", str(offset) if static else address, layout, node.token, valid, packed)

    def load(self, place):
        kind = place.layout.kind
        if kind not in {*RANGES, 'REAL', 'STRING'}:
            place.token.fail("aggregate expression is not implemented")
        wasm_type = 'f32' if kind == 'REAL' else 'i32'
        if place.kind == "local":
            return f"(block (result {wasm_type}) (if (i32.eqz (local.get {place.valid})) (then unreachable)) (local.get {place.value}))"
        if kind == 'STRING':
            code = place.value
        elif place.packed:
            code = f'(call $read_be {place.value} (i32.const {place.layout.size}))'
            code = f'(f32.reinterpret_i32 {code})' if kind == 'REAL' else normalize(code, kind)
        else:
            code = f'({wasm_type}.load {place.value})'
        if kind == 'REAL':
            code = f'(call $finite {code})'
        if place.valid:
            code = f'(block (result {wasm_type}) (call $defined {place.valid} (i32.const {place.layout.size})) {code})'
        return code

    def assignment(self, place, code, kind, node):
        target = place.layout.kind
        if target == 'STRING':
            if kind != 'STRING':
                node.token.fail('STRING assignment requires STRING')
            return self.store(place, code)
        if target == 'REAL':
            return self.store(place, f'(call $finite {self.to_real(code, kind, node)})')
        if target not in RANGES:
            node.token.fail("aggregate assignment is not implemented")
        if kind not in RANGES:
            node.token.fail('implicit conversion to integer is not implemented')
        if node.op in {"literal", "typed"} or node.op == "unary" and node.args[0] in {"+", "-"} and node.args[1].op == "literal":
            scalar_bytes(constant(node), target, node.token)
        if target == "BOOL" and kind != "BOOL":
            if node.op not in {"literal", "typed"} or constant(node) not in {0, 1}:
                node.token.fail("BOOL assignment requires BOOL or literal 0/1")
        if kind == "BOOL" and target != "BOOL":
            node.token.fail("implicit BOOL to numeric assignment is not implemented")
        # Numeric integer assignments narrow to their target width.
        code = normalize(code, target)
        return self.store(place, code)

    def store(self, place, code):
        kind = place.layout.kind
        if place.kind == 'local':
            return f'(local.set {place.value} {code}) (local.set {place.valid} (i32.const 1))'
        # Evaluate a possibly indexed destination once, before the value expression.
        address = self.temp()
        prefix = f'(local.set {address} {place.value})'
        valid_address = self.temp() if place.valid else None
        if valid_address:
            # A store may change a value used by its own index expression.
            prefix += f'(local.set {valid_address} {place.valid})'
        if kind == 'STRING':
            write = f'(call $strcopy (local.get {address}) {code})'
        elif place.packed:
            if kind == 'REAL':
                code = f'(i32.reinterpret_f32 {code})'
            write = f'(call $write_be (local.get {address}) (i32.const {place.layout.size}) {code})'
        else:
            write = f'({"f32" if kind == "REAL" else "i32"}.store (local.get {address}) {code})'
        mark = f'(memory.fill (local.get {valid_address}) (i32.const 1) (i32.const {place.layout.size}))' if valid_address else ''
        return f'{prefix} {write} {mark}'

    @staticmethod
    def to_real(code, kind, node):
        if kind == 'REAL':
            return code
        if kind in SIGNED:
            return f'(f32.convert_i32_s {code})'
        node.token.fail('REAL operation requires REAL or signed integer')

    def expr(self, node):
        op, args = node.op, node.args
        if op == 'string':
            offset = self.compiler.reserve(string_bytes(args[0], 254, node.token))
            return f'(i32.const {offset})', 'STRING'
        if (op == 'typed' and args[0] == 'REAL' or
            op == 'literal' and '#' not in args[0] and re.search(r'[.eE]', args[0]) and args[0].upper() not in {'TRUE', 'FALSE'}):
            return f'(f32.const {real_constant(node).hex()})', 'REAL'
        if op in {"literal", "typed"}:
            value = constant(node)
            raw = args[0].upper()
            kind = {"L": "DINT", "DW": "DWORD", "W": "WORD", "B": "BYTE"}.get(raw, raw) if op == "typed" else None
            if kind is None:
                kind = "BOOL" if raw in {"TRUE", "FALSE"} else "DWORD" if "#" in raw else "INT" if value <= 32767 else "DINT"
            if kind not in RANGES:
                node.token.fail(f"unsupported literal type {kind}")
            scalar_bytes(value, kind, node.token)
            return f"(i32.const {value})", kind
        if op == "ref":
            place = self.place(node)
            return self.load(place), place.layout.kind
        if op == "unary":
            operator, child = args
            # Signed minimum literals need to be checked after applying the sign.
            if operator in {"+", "-"} and child.op == "literal" and re.fullmatch(r'[0-9_]+', child.args[0]):
                value = constant(node)
                kind = "INT" if -32768 <= value <= 32767 else "DINT"
                scalar_bytes(value, kind, node.token)
                return f"(i32.const {value})", kind
            value, kind = self.expr(child)
            if kind == 'REAL' and operator in {'+', '-'}:
                return (f'(f32.neg {value})' if operator == '-' else value), kind
            if operator == "NOT":
                if kind == "BOOL":
                    return f"(i32.eqz {value})", kind
                if kind not in BITS:
                    node.token.fail("NOT requires BOOL or bit-string type")
                return normalize(f"(i32.xor {value} (i32.const -1))", kind), kind
            if kind not in SIGNED:
                node.token.fail("unary sign requires signed integer")
            return (normalize(f"(i32.sub (i32.const 0) {value})", kind) if operator == "-" else value), kind
        if op == "binary":
            operator, a, b = args
            left, lk = self.expr(a); right, rk = self.expr(b)
            compare = operator in {"=", "<>", "<", ">", "<=", ">="}
            if 'STRING' in {lk, rk}:
                if lk != rk or not compare:
                    node.token.fail('STRING supports comparisons with STRING only')
                instruction = {'=': 'eq', '<>': 'ne', '<': 'lt_s', '>': 'gt_s', '<=': 'le_s', '>=': 'ge_s'}[operator]
                return f'(i32.{instruction} (call $strcmp {left} {right}) (i32.const 0))', 'BOOL'
            if 'REAL' in {lk, rk}:
                left = self.to_real(left, lk, a)
                right = self.to_real(right, rk, b)
                instruction = {'=': 'eq', '<>': 'ne', '<': 'lt', '>': 'gt', '<=': 'le', '>=': 'ge', '+': 'add', '-': 'sub', '*': 'mul', '/': 'div'}.get(operator)
                if instruction is None:
                    node.token.fail('unsupported REAL operation')
                code = f'(f32.{instruction} {left} {right})'
                return (code, 'BOOL') if compare else (f'(call $finite {code})', 'REAL')
            if lk == "BOOL" or rk == "BOOL":
                if compare:
                    for k, n in [(lk, a), (rk, b)]:
                        if k != "BOOL" and (n.op != "literal" or constant(n) not in {0, 1}):
                            n.token.fail("BOOL comparison only accepts BOOL or literal 0/1")
                    kind = "BOOL"
                elif lk == rk == "BOOL" and operator in {"AND", "OR", "XOR"}:
                    kind = "BOOL"
                else:
                    node.token.fail("invalid boolean operation")
            elif lk in SIGNED and rk in SIGNED:
                kind = "DINT" if "DINT" in {lk, rk} else "INT"
            elif lk in BITS and rk in BITS:
                kind = max((lk, rk), key=lambda k: RANGES[k][1])
            else:
                node.token.fail(f"mixed signed/bit-string operation {lk} {operator} {rk} needs explicit conversion")
            if compare:
                instruction = {"=": "eq", "<>": "ne", "<": "lt", ">": "gt", "<=": "le", ">=": "ge"}[operator]
                if instruction not in {"eq", "ne"}:
                    instruction += "_s" if kind in SIGNED else "_u"
                return f"(i32.{instruction} {left} {right})", "BOOL"
            if operator in {"AND", "OR", "XOR"}:
                if kind in SIGNED:
                    node.token.fail("bitwise operations require BOOL or bit-string types")
                return normalize(f"(i32.{operator.lower()} {left} {right})", kind), kind
            if kind not in SIGNED:
                node.token.fail("arithmetic requires signed integer")
            instruction = {"+": "add", "-": "sub", "*": "mul", "/": "div_s", "DIV": "div_s", "MOD": "rem_s"}.get(operator)
            if not instruction:
                node.token.fail(f"unsupported operator {operator}")
            return normalize(f"(i32.{instruction} {left} {right})", kind), kind
        if op == "call":
            name, parameters = args
            name = name.upper()
            if name in BUILTINS - set(CONVERSIONS):
                return self.builtin(name, parameters, node)
            if name not in CONVERSIONS:
                node.token.fail(f"value-returning function {name} is not implemented")
            if len(parameters) != 1 or parameters[0][0] is not None:
                node.token.fail("conversion requires one positional argument")
            code, kind = self.expr(parameters[0][1])
            expected, target = CONVERSIONS[name]
            if kind != expected:
                node.token.fail(f"{name} requires {expected}, got {kind}")
            return normalize(code, target), target
        node.token.fail(f"expression {op} is not implemented")

    def builtin(self, name, parameters, node):
        labels = {'LEN': ('S',), 'DINT_TO_STRING': ('IN',), 'CONCAT': ('IN1', 'IN2'),
                  'SHR': ('IN', 'N'), 'DIS_IRT': ('MODE', 'OB_NR')}[name]
        if len(parameters) != len(labels):
            node.token.fail(f'{name} requires {len(labels)} arguments')
        if all(label is None for label, _ in parameters):
            values = [value for _, value in parameters]
        else:
            supplied = {}
            for label, value in parameters:
                if label is None or label[1] != ':=' or label[0].upper() not in labels or label[0].upper() in supplied:
                    node.token.fail(f'invalid or duplicate {name} argument')
                supplied[label[0].upper()] = value
            values = [supplied[label] for label in labels]
        if name == 'DIS_IRT':
            if self.compiler.system_profile != 'cyclic-sim':
                node.token.fail('DIS_IRT requires explicit cyclic-sim system profile')
            if tuple(constant(value) for value in values) != (1, 80):
                node.token.fail('cyclic-sim only models DIS_IRT class disable MODE=1 OB_NR=80')
            if self.expr(values[0])[1] not in {'BYTE', 'INT'} or self.expr(values[1])[1] != 'INT':
                node.token.fail('DIS_IRT requires BYTE-compatible MODE and INT OB_NR constants')
            return f'(block (result i32) (i32.store (i32.const {self.compiler.system_offset}) (i32.const 1)) (i32.const 0))', 'INT'
        expressions = [self.expr(value) for value in values]
        if name == 'LEN':
            code, kind = expressions[0]
            if kind != 'STRING':
                node.token.fail('LEN requires STRING')
            return f'(call $strlen {code})', 'INT'
        if name in {'CONCAT', 'DINT_TO_STRING'}:
            expected = 'STRING' if name == 'CONCAT' else 'DINT'
            if any(kind != expected for _, kind in expressions):
                node.token.fail(f'{name} requires {expected}')
            offset = self.compiler.reserve(bytes([254]) + bytes(255))
            helper = '$concat' if name == 'CONCAT' else '$dint_string'
            return f'(call {helper} (i32.const {offset}) {" ".join(code for code, _ in expressions)})', 'STRING'
        (value, kind), (count, count_kind) = expressions
        if kind not in BITS or count_kind not in SIGNED:
            node.token.fail('SHR requires bit-string IN and signed integer N')
        v, n = self.temp(), self.temp()
        width = {'BYTE': 8, 'WORD': 16, 'DWORD': 32}[kind]
        return (f'(block (result i32) (local.set {v} {value}) (local.set {n} {count}) '
                f'(if (i32.lt_s (local.get {n}) (i32.const 0)) (then unreachable)) '
                f'(if (result i32) (i32.ge_u (local.get {n}) (i32.const {width})) '
                f'(then (i32.const 0)) (else (i32.shr_u (local.get {v}) (local.get {n})))))', kind)

    def condition(self, node):
        code, kind = self.expr(node)
        if kind != "BOOL":
            node.token.fail("condition must be BOOL")
        return code

    def statements(self, body):
        return "\n".join(self.statement(s) for s in body)

    def statement(self, stmt):
        op, args = stmt.op, stmt.args
        if op == "assign":
            place = self.place(args[0]); code, kind = self.expr(args[1])
            return self.assignment(place, code, kind, args[1])
        if op == "call":
            call = args[0]; name, parameters = call.args
            if parameters or name.upper() not in self.compiler.ids:
                call.token.fail("unsupported statement call")
            return f"(call {self.compiler.ids[name.upper()]})"
        if op == "return":
            return "(return)"
        if op == "exit":
            if not self.loops:
                stmt.token.fail("EXIT outside a loop")
            return f"(br {self.loops[-1]})"
        if op == "if":
            branches, otherwise = args
            result = self.statements(otherwise)
            for condition, body in reversed(branches):
                result = f"(if {self.condition(condition)} (then {self.statements(body)}) (else {result}))"
            return result
        if op == "case":
            selector, arms, otherwise = args
            value, kind = self.expr(selector)
            if kind not in SIGNED:
                selector.token.fail("CASE selector must be INT or DINT")
            temporary = self.temp(); ranges = []
            compiled = []
            for labels, body in arms:
                conditions = []
                for low_node, high_node in labels:
                    low, high = constant(low_node), constant(high_node)
                    if low > high or not RANGES[kind][0] <= low <= high <= RANGES[kind][1]:
                        low_node.token.fail("invalid CASE range")
                    if any(low <= h and high >= l for l, h in ranges):
                        low_node.token.fail("overlapping CASE labels")
                    ranges.append((low, high))
                    conditions.append(f"(i32.and (i32.ge_s (local.get {temporary}) (i32.const {low})) (i32.le_s (local.get {temporary}) (i32.const {high})))")
                condition = conditions[0]
                for other in conditions[1:]:
                    condition = f"(i32.or {condition} {other})"
                compiled.append((condition, self.statements(body)))
            result = self.statements(otherwise)
            for condition, body in reversed(compiled):
                result = f"(if {condition} (then {body}) (else {result}))"
            return f"(local.set {temporary} {value}) {result}"
        if op in {"while", "for"}:
            label = self.temp()
            end, loop = label + "_end", label + "_loop"
            self.loops.append(end)
            try:
                if op == "while":
                    condition, body = args
                    return f"(block {end} (loop {loop} (br_if {end} (i32.eqz {self.condition(condition)})) {self.statements(body)} (br {loop})))"
                variable, first, last, step_node, body = args
                place = self.place(variable)
                if place.layout.kind not in SIGNED:
                    variable.token.fail("FOR variable must be INT or DINT")
                if any(k == "index" for k, _ in variable.args[1]):
                    variable.token.fail("indexed FOR control variable is not implemented")
                step = constant(step_node)
                if step == 0 or not -2147483648 <= step <= 2147483647:
                    step_node.token.fail("FOR step must be a nonzero signed i32 constant")
                first_code, first_kind = self.expr(first)
                last_code, last_kind = self.expr(last)
                if first_kind not in SIGNED or last_kind not in SIGNED:
                    stmt.token.fail("FOR bounds must be signed integers")
                bound = self.temp(); next_value = self.temp("i64")
                start = self.assignment(place, first_code, first_kind, first)
                compare = "gt_s" if step > 0 else "lt_s"
                low, high = RANGES[place.layout.kind]
                increment = f"(local.set {next_value} (i64.add (i64.extend_i32_s {self.load(place)}) (i64.const {step})))"
                increment += f"(if (i32.or (i64.lt_s (local.get {next_value}) (i64.const {low})) (i64.gt_s (local.get {next_value}) (i64.const {high}))) (then unreachable))"
                increment += self.assignment(place, f"(i32.wrap_i64 (local.get {next_value}))", place.layout.kind, variable)
                return f"{start} (local.set {bound} {last_code}) (block {end} (loop {loop} (br_if {end} (i32.{compare} {self.load(place)} (local.get {bound}))) {self.statements(body)} {increment} (br {loop})))"
            finally:
                self.loops.pop()
        stmt.token.fail(f"statement {op} is not implemented")
