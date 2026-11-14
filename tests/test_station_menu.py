"""Generic menu tests; no PLC source or station artifacts."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from station_menu import build_command, discover


class StationMenuTests(unittest.TestCase):
    def test_discovery_nested_and_malformed_packages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / 'nested folder'
            folder.mkdir()
            manifest = json.dumps({'name': 'generic'}).encode()
            data = b'SWP1' + struct.pack('<II', len(manifest), 8) + manifest + b'\0asm\x01\0\0\0'
            valid = folder / 'valid.PKG'
            valid.write_bytes(data)
            (root / 'truncated.pkg').write_bytes(data[:-1])
            (root / 'trailing.pkg').write_bytes(data + b'x')
            (root / 'other.pkg').write_bytes(b'other format')
            self.assertEqual(discover(root), [(valid.resolve(), {'name': 'generic'})])

    def test_rebuild_preserves_options_and_handles_paths_with_spaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / 'program.pkg'
            report = dict(entry='entry', init=['first', 'second'], presets=['value=1'],
                          watches=['value'], system_profile='cyclic-sim', fuel_per_call=30000000)
            package.with_name('build.json').write_text(json.dumps(report))
            package.with_name('s7-map.json').touch()
            source = root / 'source folder'
            command = build_command(package, {'name': 'generic'}, source, root / 'output', root / 'runtime')
            self.assertEqual(command[2], str(source))
            self.assertEqual(command[command.index('--entry') + 1], 'entry')
            self.assertEqual(command[command.index('--fuel-per-call') + 1], '30000000')
            self.assertEqual(command[command.index('--system-profile') + 1], 'cyclic-sim')
            tail = command[command.index('--init'):]
            self.assertEqual(tail, ['--init', 'first', '--init', 'second', '--set', 'value=1',
                                    '--watch', 'value', '--s7-map'])
            package.with_name('s7-map.json').unlink()
            self.assertNotIn('--s7-map', build_command(package, {'name': 'generic'}, source, root, root / 'runtime'))


if __name__ == '__main__':
    unittest.main()
