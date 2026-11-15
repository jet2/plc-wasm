"""Generate local S7 DB maps; generated maps are protected project artifacts."""
import re
import csv
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
    scalar_symbols = {}
    for symbol in symbols(table_directory / 'SymbolTable.asc'):
        scalar_symbols.setdefault(symbol['name'].upper(), symbol)
    signals = []
    blocks, by_address, used = [], {}, set()
    for name, (offset, layout) in compiler.globals.items():
        if name not in compiler.project.dbs:
            symbol = scalar_symbols.get(name)
            if symbol is None:
                raise CompileError(f'missing process symbol: {name}')
            location = re.fullmatch(r'([IQMEAB])(?:[BWD])?\s*\d+(?:\.\d+)?', symbol['address'], re.I)
            if location is None or layout.kind not in {'BOOL', 'BYTE', 'WORD', 'INT', 'DWORD', 'DINT', 'REAL'}:
                raise CompileError(f'unsupported process symbol: {name}')
            signals.append(dict(name=name, offset=offset, bit=0, kind=layout.kind, capacity=0,
                                writable=location[1].upper() in {'I', 'E'}, aliases=[symbol['name']]))
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
    references = [('DSP_Read_Table.asc', False), ('DSP_Write_Table.asc', True)]
    if (table_directory / 'RouteTags.asc').is_file():
        references.append(('RouteTags.asc', False))
    for filename, writable in references:
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
    controls = {'SEConnected', 'StationView_StationCode', 'StationView_WatchDog', 'StationView_Connected'}
    control_addresses = {}
    for path in sorted(table_directory.glob('*_OPC_Config.csv')):
        for row in csv.reader(read_source(path).splitlines(), delimiter=';'):
            if len(row) < 4 or row[2] not in controls:
                continue
            match = re.search(r'DB(\d+),(INT|X)(\d+)(?:\.(\d+))?,1$', row[3])
            if not match:
                raise CompileError(f'unsupported control address for {row[2]}')
            db, kind, byte, bit = match.groups()
            location = (int(db), int(byte) * 8 + int(bit or 0))
            if row[2] in control_addresses and control_addresses[row[2]] != location:
                raise CompileError(f'ambiguous control address for {row[2]}')
            control_addresses[row[2]] = location
            field = by_address.get(location)
            if field is None or field['kind'] != ('INT' if kind == 'INT' else 'BOOL'):
                raise CompileError(f'control address disagrees with layout: {row[2]}')
            aliases = field.setdefault('aliases', [])
            if row[2] not in aliases:
                aliases.append(row[2])
            field['writable'] |= row[2] != 'StationView_Connected'
    db_names = {n.upper() for block in blocks for field in block['fields']
                for n in [field['name'], *field.get('aliases', [])]}
    for field in signals:
        original = field['aliases'][0]
        field['name'] = 'IO.' + field['name']
        if original.upper() in db_names:
            # Existing DB tag names remain stable; bus mappings use the qualified process name.
            field['aliases'] = ['IO.' + original]
    return dict(version=1, module_sha256=manifest['module_sha256'],
                output_offset=manifest['outputs']['offset'], output_size=manifest['outputs']['size'],
                blocks=blocks, signals=signals), checked
