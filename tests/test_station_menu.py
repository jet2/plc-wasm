"""Generic menu tests; no PLC source or station artifacts."""
import json
import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from station_menu import build_command, discover
import station_menu as menu


class StationMenuTests(unittest.TestCase):
    def test_saved_lists_do_not_search_or_drop_missing_packages(self):
        settings = {'compiled_stations': [{'package': '/missing/generic.pkg', 'name': 'saved'}]}
        with patch.object(menu, 'discover', side_effect=AssertionError('Unexpected search')):
            self.assertEqual(menu.saved_packages(settings), [(Path('/missing/generic.pkg'), {'name': 'saved'})])
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                menu.show(menu.saved_packages(settings))
            self.assertIn('saved', output.getvalue())

    def test_launch_subsection_with_empty_catalog_does_not_search(self):
        with patch.object(menu, 'search_folder', return_value=Path('/missing/search-folder')), \
                patch.object(menu, 'load_settings', return_value={}), \
                patch.object(menu, 'refresh_catalog', side_effect=AssertionError('Unexpected search')), \
                patch('sys.argv', ['station_menu.py']), \
                patch('builtins.input', side_effect=['3', '0']), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            menu.main()
        self.assertIn('В настройках нет скомпилированных станций', output.getvalue())

    def test_source_search_requires_nonempty_folders_and_accepts_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for station, spelling in [('one', 'Source'), ('two', 'Soure'), ('empty', 'Source')]:
                program = root / station / 'Program'
                (program / spelling).mkdir(parents=True)
                (program / 'Tables').mkdir()
                (program / spelling / 'marker.txt').write_text('generic')
                if station != 'empty':
                    (program / 'Tables/marker.txt').write_text('generic')
            found = menu.discover_sources(root)
            self.assertEqual({item['name'] for item in found}, {'one', 'two'})
            self.assertEqual(len(menu.discover_sources(root / 'one/Program')), 1)

    def test_catalog_is_saved_reloaded_and_marks_only_linked_builds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            package = root / 'build/one/program.pkg'
            program = root / 'one/Program'
            config = {'sources': {str(package): str(program)}}
            sources = [dict(name='one', program=str(program)), dict(name='two', program=str(root / 'two/Program'))]
            with patch.object(menu, 'ROOT', root), patch.object(menu, 'SETTINGS', root / 'menu-settings.json'), \
                    patch.object(menu, 'discover', return_value=[(package, {'name': 'generic'})]), \
                    patch.object(menu, 'discover_sources', return_value=sources):
                menu.save_search_folder(root)
                settings = menu.refresh_catalog(root, config, include_sources=True)
                self.assertEqual(settings, menu.read_json(menu.SETTINGS))
                self.assertEqual(settings['compiled_stations'][0]['package'], str(package))
                self.assertTrue(settings['source_stations'][0]['compiled'])
                self.assertFalse(settings['source_stations'][1]['compiled'])
                with patch.object(menu, 'discover', return_value=[]):
                    settings = menu.refresh_catalog(root, config)
                self.assertFalse(settings['source_stations'][0]['compiled'])
                self.assertEqual(settings['compiled_stations'], [])

    def test_failed_source_compilation_does_not_register_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'Source').mkdir()
            (root / 'Source/marker.txt').write_text('generic')
            (root / 'Tables').mkdir()
            config = {}
            with patch('builtins.input', side_effect=['entry', '-', 'strict', '10000000']), \
                    patch.object(menu, 'runtime_path', return_value=root / 'runtime'), \
                    patch.object(menu.subprocess, 'run', side_effect=menu.subprocess.CalledProcessError(1, 'compiler')), \
                    patch.object(menu, 'save_config') as save:
                with self.assertRaises(menu.subprocess.CalledProcessError):
                    menu.compile_source(dict(program=str(root), name='generic'), config)
                save.assert_not_called()
                self.assertNotIn('sources', config)

    def test_settings_persist_folder_and_override_is_temporary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tools = root / 'tools'
            tools.mkdir()
            with patch.object(menu, 'ROOT', root), patch.object(menu, 'SETTINGS', tools / 'menu-settings.json'):
                self.assertEqual(menu.search_folder(), root / 'build')
                menu.SETTINGS.write_text(json.dumps({'stations_folder': 'nested', 'other': True}), encoding='utf-8-sig')
                self.assertEqual(menu.search_folder(), root / 'nested')
                self.assertEqual(menu.search_folder(root / 'override'), root / 'override')
                self.assertEqual(menu.search_folder(), root / 'nested')
                menu.save_search_folder(root / 'saved')
                self.assertEqual(menu.search_folder(), root / 'saved')
                self.assertTrue(menu.read_json(menu.SETTINGS)['other'])

    def test_stop_refuses_unmanaged_executable(self):
        with patch.object(menu, 'windows_process') as opener:
            with self.assertRaises(ValueError):
                menu.stop_station(dict(pid=123, identity=(str(menu.ROOT / 'other/wplc.exe'), 1)))
            opener.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows handle identity check')
    def test_stop_refuses_reused_pid_and_closes_handle(self):
        executable = str(menu.ROOT / 'build/live/test/wplc.exe')
        kernel = MagicMock()
        record = dict(pid=123, identity=(executable, 1))
        with patch.object(menu, 'windows_process', return_value=(kernel, 456, (executable, 2))):
            with self.assertRaises(ValueError):
                menu.stop_station(record)
        kernel.TerminateProcess.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(456)

    def test_stop_menu_cancel_does_not_signal(self):
        with patch.object(menu, 'running_stations', return_value=[dict(pid=123, command='generic')]), \
                patch('builtins.input', return_value='0'), patch.object(menu, 'stop_station') as stop:
            menu.stop_menu()
            stop.assert_not_called()

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
