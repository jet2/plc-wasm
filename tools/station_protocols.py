"""Generate a private gateway configuration from a station folder and compiled map."""
import argparse
import configparser
import ipaddress
import json
from pathlib import Path
import re

from scl_audit import read_source, symbols


def station_files(folder):
    files = list(folder.iterdir())
    mprs = [p for p in files if p.is_file() and p.suffix.lower() == '.mpr']
    if len(mprs) != 1:
        raise ValueError(f'ALARM: expected exactly one .mpr in {folder}, found {len(mprs)}')
    usos = [p for p in files if p.is_file() and p.suffix.lower() == '.uso'
            and p.stem.lower() == mprs[0].stem.lower()]
    if len(usos) != 1:
        raise ValueError('ALARM: expected one .uso with the same basename as .mpr')
    return mprs[0], usos[0]


def parse_uso(text):
    ini = configparser.ConfigParser(interpolation=None, strict=True, delimiters=('=',),
                                    comment_prefixes=(';',), empty_lines_in_values=False)
    ini.optionxform = str
    ini.read_string(text)
    result = {}
    for section in ini.sections():
        key = section.casefold()
        if key in result:
            raise ValueError('duplicate USO section')
        result[key] = dict(ini.items(section))
    return result


def devices_from_uso(ini, table, mapping):
    if 'slaves' not in ini:
        raise ValueError('missing [Slaves]')
    if len(ini['slaves']) > 64:
        raise ValueError('too many slaves')
    # Match the emulator: first symbol wins; a unique 24-character prefix is accepted.
    by_name = {}
    for symbol in table:
        by_name.setdefault(symbol['name'].upper(), symbol)
    compiled = {f['name'].upper().removeprefix('IO.'): f for f in mapping.get('signals', [])}
    result, targets = [], set()
    for key in ini['slaves']:
        number = int(key)
        if str(number) != key or not 1 <= number <= 255:
            raise ValueError('slave number must be a canonical Unit ID in 1..255')
        host = str(ipaddress.ip_address(ini.get('slaves_ip', {}).get(key, '127.0.0.1').strip()))
        port = int(ini.get('slaves_port', {}).get(key, 15000 + number))
        if not 1 <= port <= 65535 or ipaddress.ip_address(host).is_unspecified:
            raise ValueError('invalid slave endpoint')
        if key not in ini:
            raise ValueError(f'missing module section [{key}]')
        regs = [0, 0]
        requests = []
        for module, value in ini[key].items():
            int(module)
            spec = [int(p.strip()) for p in value.split(',')]
            if len(spec) < 4:
                raise ValueError('invalid module description')
            contacts = ini.get(f'{key}.{module}', {})
            direction = spec[0]
            if direction not in (0, 1):
                if contacts:
                    raise ValueError('unsupported module with contacts')
                continue
            analog = spec[1] == 1
            count = spec[2 if direction == 0 else 3]
            if not 0 <= count <= 65535:
                raise ValueError('invalid channel count')
            base = regs[direction]
            regs[direction] += count if analog else (count + 15) // 16
            if regs[direction] > 65535:
                raise ValueError('register image too large')
            points = []
            for channel, description in contacts.items():
                index = int(channel)
                if not 0 <= index < count:
                    raise ValueError('contact outside module')
                tag = description.split(',', 1)[0].strip().upper()
                if not tag:
                    continue
                symbol = by_name.get(tag)
                if symbol is None:
                    candidates = [s for n, s in by_name.items() if len(n) == 24 and tag.startswith(n)]
                    if len(candidates) == 1:
                        symbol = candidates[0]
                if symbol is None:
                    raise ValueError(f'contact has no unique SymbolTable entry: {tag}')
                field = compiled.get(symbol['name'].upper())
                if field is None:
                    raise ValueError(f'contact is absent from compiled process signals: {tag}; rebuild with current compiler')
                address = re.sub(r'\s+', '', symbol['address']).upper()
                area = address[:1]
                if area not in ({'I', 'E'} if direction == 0 else {'Q', 'A'}):
                    raise ValueError(f'wrong contact direction: {tag}')
                allowed = {'WORD', 'INT', 'BYTE'} if analog else {'BOOL'}
                if field['kind'] not in allowed or field['kind'] != symbol['type'].upper():
                    raise ValueError(f'incompatible module channel type: {tag}')
                if field['offset'] in targets:
                    raise ValueError(f'duplicate bus target: {tag}')
                targets.add(field['offset'])
                point = base + index if analog else base * 16 + index
                if point > 65535:
                    raise ValueError('contact exceeds Modbus address range')
                points.append((point, field['name']))
            operation = ('read_input_registers' if analog else 'read_discrete_inputs') if direction == 0 else (
                'write_registers' if analog else 'write_coils')
            limit = (125 if direction == 0 else 123) if analog else (2000 if direction == 0 else 1968)
            current = None
            for point, name in sorted(points):
                if current is None or point != current['address'] + len(current['fields']) or len(current['fields']) == limit:
                    current = dict(operation=operation, address=point, fields=[])
                    requests.append(current)
                current['fields'].append(name)
        if len(requests) > 256:
            raise ValueError('too many requests for slave')
        result.append(dict(name=f'shm-{number}', enabled=bool(requests), address=host, port=port,
                           unit=number, poll_ms=100, timeout_ms=1000, requests=requests))
    known = set(ini['slaves'])
    for section in ('slaves_ip', 'slaves_port'):
        if set(ini.get(section, {})) - known:
            raise ValueError(f'unknown slave in [{section}]')
    return result


def generate(folder, tables, mapping, output):
    _, uso = station_files(folder)
    devices = devices_from_uso(parse_uso(read_source(uso)), symbols(tables / 'SymbolTable.asc'), mapping)
    pki = Path(__file__).resolve().parents[1] / '.local-validation' / 'pki'
    return dict(period_ms=100, opcua=dict(port=4841, pki_dir=str(pki), tags=[]),
                snap7=None, modbus=devices)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('station', type=Path)
    parser.add_argument('--tables', required=True, type=Path)
    parser.add_argument('--map', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--force', action='store_true', help='replace an existing local configuration')
    args = parser.parse_args()
    try:
        if args.output.exists() and not args.force:
            raise ValueError('output exists; use --force to replace it')
        config = generate(args.station, args.tables, json.loads(args.map.read_text(encoding='utf8')), args.output)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        temporary.replace(args.output)
    except (ValueError, OSError, configparser.Error) as error:
        parser.exit(2, f'ALARM: {error}\n')
    print(f'Generated {len(config["modbus"])} slave configurations in {args.output}')


if __name__ == '__main__':
    main()
