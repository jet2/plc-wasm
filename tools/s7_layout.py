"""Generate local S7 DB maps; generated maps are protected project artifacts."""
import re
from scl_frontend import CompileError
from scl_audit import symbols, read_source


def exported_symbols(path):
    result = []
    for line in read_source(path).splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'(?:126|136),(.*?)\s*(DB\d+\.D(?:B)?[XBWD]\d+(?:\.\d+)?)\s+(BOOL|BYTE|WORD|DWORD|INT|DINT|REAL|STRING)(?:\s.*|\[.*)', line, re.I)
        if not match:
            raise CompileError(f'malformed exported address record in {path.name}')
        name, address_text, kind = match.groups()
        result.append(dict(name=name.strip(), address=address_text, type=kind))
    return result


def align(value, boundary):
    return (value + boundary - 1) // boundary * boundary


def describe(layout):
    """Return bit size, alignment and relative scalar leaves for classic S7 DBs."""
    kind = layout.kind
    if kind in {'BOOL', 'BYTE', 'WORD', 'INT', 'DWORD', 'DINT', 'REAL'}:
        bits = {'BOOL': 1, 'BYTE': 8, 'WORD': 16, 'INT': 16,
                'DWORD': 32, 'DINT': 32, 'REAL': 32}[kind]
        return bits, min(bits, 16), [('', 0, 0, kind, 0)]
    if kind == 'STRING':
        capacity = layout.data[0]
        return align((capacity + 2) * 8, 16), 16, [('', 0, 0, kind, capacity)]
    if kind == 'STRUCT':
        end, leaves = 0, []
        for original, offset, child in layout.fields.values():
            size, boundary, fields = describe(child)
            end = align(end, boundary)
            leaves.extend(('.' + original + name, offset + wasm, end + bit, typ, cap)
                          for name, wasm, bit, typ, cap in fields)
            end += size
        return align(end, 16), 16, leaves
    if kind == 'ARRAY':
        size, _, fields = describe(layout.element)
        count = layout.high - layout.low + 1
        # Each multidimensional row ends at a byte for BOOL/BYTE, otherwise a word.
        base = layout.element
        while base.kind == 'ARRAY':
            base = base.element
        boundary = 8 if base.kind in {'BOOL', 'BYTE'} else 16
        leaves = []
        for index in range(count):
            leaves.extend((f'[{index + layout.low}]' + name,
                           index * layout.element.size + wasm, index * size + bit, typ, cap)
                          for name, wasm, bit, typ, cap in fields)
        return align(count * size, boundary), 16, leaves
    raise CompileError(f'unsupported external S7 type {kind}')


def address(text):
    match = re.fullmatch(r'DB\s*(\d+)\.D(?:B)?([XBWD])(\d+)(?:\.(\d+))?', text, re.I)
    if not match:
        raise CompileError(f'unsupported exported DB address: {text}')
    number, width, byte, bit = match.groups()
    if (width.upper() == 'X') != (bit is not None) or bit is not None and int(bit) > 7:
        raise CompileError(f'invalid exported DB address: {text}')
    return int(number), int(byte) * 8 + int(bit or 0), width.upper()


def make_map(compiler, manifest, table_directory):
    numbers = {}
    for symbol in symbols(table_directory / 'SymbolTable.asc'):
        match = re.fullmatch(r'DB\s*(\d+)', symbol['address'], re.I)
        if match:
            key = symbol['name'].upper()
            if key in numbers:
                raise CompileError('duplicate DB symbol')
            numbers[key] = int(match[1])
    blocks, by_address, used = [], {}, set()
    for name, (offset, layout) in compiler.globals.items():
        if name not in compiler.project.dbs:
            continue
        if name not in numbers or numbers[name] in used:
            raise CompileError(f'missing or duplicate DB number for {name}')
        number = numbers[name]
        used.add(number)
        size, _, leaves = describe(layout)
        if not 1 <= number <= 65535 or not 0 < size <= 65536 * 8:
            raise CompileError(f'DB number or size exceeds classic S7 limit: {name}')
        original = compiler.project.dbs[name][0].text
        fields = []
        for suffix, wasm, bit, kind, capacity in leaves:
            item = dict(name=original + suffix, offset=offset + wasm, bit=bit,
                        kind=kind, capacity=capacity, writable=False)
            fields.append(item)
            by_address[number, bit] = item
        blocks.append(dict(number=number, size=size // 8, fields=fields))
    checked = {}
    for filename, writable in [('DSP_Read_Table.asc', False), ('DSP_Write_Table.asc', True)]:
        path = table_directory / filename
        if not path.is_file():
            raise CompileError(f'required address reference missing: {filename}')
        entries = exported_symbols(path)
        if not entries:
            raise CompileError(f'empty address reference: {filename}')
        for symbol in entries:
            number, bit, width = address(symbol['address'])
            field = by_address.get((number, bit))
            expected = {'BOOL':'X','BYTE':'B','INT':'W','WORD':'W','DINT':'D','DWORD':'D','REAL':'D','STRING':'B'}
            if field is None or field['kind'] != symbol['type'].upper() or expected[field['kind']] != width:
                raise CompileError(f'exported tag disagrees with generated layout: {symbol["name"]} at {symbol["address"]}')
            # Exported equipment tags are aliases for fields inside indexed arrays.
            aliases = field.setdefault('aliases', [])
            if symbol['name'] not in aliases:
                aliases.append(symbol['name'])
            field['writable'] |= writable
        checked[filename] = len(entries)
    return dict(version=1, module_sha256=manifest['module_sha256'],
                output_offset=manifest['outputs']['offset'], output_size=manifest['outputs']['size'],
                blocks=blocks), checked
