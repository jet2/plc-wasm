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
    def test_gateway_launch_creates_local_settings_and_uses_gateway_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / 'program.pkg'
            package.with_name('s7-map.json').touch()
            runtime = root / 'runtime.exe'
            config = {}
            with patch.object(menu, 'ROOT', root), patch.object(menu, 'runtime_path', return_value=runtime), \
                    patch.object(menu, 'save_config'), patch.object(menu.shutil, 'copy2'), \
                    patch.object(menu.shutil, 'which', return_value='/terminal'), \
                    patch.object(menu.subprocess, 'Popen') as start, \
                    patch('builtins.input', side_effect=['3', '']):
                menu.launch(package, config)
            settings = root / '.local-validation/protocols.json'
            self.assertEqual(menu.read_json(settings)['modbus'], [])
            self.assertEqual(menu.read_json(settings)['opcua']['port'], 4841)
            self.assertEqual(config['protocols'], str(settings.resolve()))
            command = start.call_args.args[0]
            # A Linux terminal emulator may prefix the command.
            gateway = command.index('gateway')
            self.assertEqual(command[gateway + 1:], [str(package), str(package.with_name('s7-map.json')), str(settings.resolve()), '0'])

    def test_batch_selection_ranges_all_and_invalid_input(self):
        self.assertEqual(menu.batch_selection('1,3-5,3', 5), [0, 2, 3, 4])
        self.assertEqual(menu.batch_selection('все', 3), [0, 1, 2])
        self.assertEqual(menu.batch_selection('0', 3), [])
        for text in ('0,1', '3-1', '4', '1,,2', '-1'):
            with self.assertRaises(ValueError):
                menu.batch_selection(text, 3)

    def test_batch_delete_preflight_and_catalog_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / 'Station.mpr').touch()
            packages = []
            for number in (1, 2):
                folder = root / 'wplc-build' / f'20260101-000000-00000{number}'
                folder.mkdir(parents=True)
                (folder / 'program.pkg').write_bytes(b'generic')
                (folder / 'build.json').write_text('{}')
                packages.append(folder / 'program.pkg')
            with patch.object(menu, 'SETTINGS', root / 'menu-settings.json'), \
                    patch.object(menu, 'CONFIG', root / 'config.json'), \
                    patch.object(menu, 'running_stations', return_value=[]):
                menu.save_settings({'compiled_stations': [dict(package=str(p)) for p in packages]})
                menu.save_config({'sources': {str(p): 'generic' for p in packages}})
                foreign = packages[1].with_name('keep.txt')
                foreign.write_text('keep')
                with self.assertRaises(ValueError):
                    menu.delete_builds(packages)
                self.assertTrue(all(p.exists() for p in packages))
                foreign.unlink()
                with patch.object(menu, 'running_stations', return_value=[dict(pid=123, command=f'wplc serve "{packages[1]}"')]):
                    with self.assertRaises(ValueError):
                        menu.delete_builds(packages)
                self.assertTrue(all(p.exists() for p in packages))
                menu.delete_builds(packages)
                self.assertTrue(all(not p.parent.exists() for p in packages))
                self.assertTrue((root / 'Station.mpr').exists())
                self.assertEqual(menu.load_settings()['compiled_stations'], [])
                self.assertEqual(menu.read_json(menu.CONFIG)['sources'], {})

    def test_delete_menu_cancel_does_not_delete(self):
        with patch.object(menu, 'load_settings', return_value={}), \
                patch.object(menu, 'saved_packages', return_value=[(Path('/generic/program.pkg'), {'name': 'generic'})]), \
                patch.object(menu, 'build_files'), patch.object(menu, 'show'), \
                patch('builtins.input', side_effect=['1', 'нет']), \
                patch.object(menu, 'delete_builds') as delete:
            menu.delete_menu()
        delete.assert_not_called()

    def test_build_output_is_beside_nearest_mpr(self):
        with tempfile.TemporaryDirectory() as temporary:
            station = Path(temporary).resolve()
            program = station / 'project/Program'
            program.mkdir(parents=True)
            (station / 'Station.MPR').touch()
            output = menu.build_output(program)
            self.assertEqual(output.parent, station / 'wplc-build')
            self.assertFalse(output.exists())
            (program.parent / 'Nested.mpr').touch()
            self.assertEqual(menu.build_output(program).parent, program.parent / 'wplc-build')

    def test_build_output_requires_mpr(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(menu.Path, 'iterdir', return_value=iter([])):
                with self.assertRaisesRegex(ValueError, '.mpr'):
                    menu.build_output(Path(temporary) / 'Program')

    def test_register_build_adds_output_without_search_or_clearing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(menu, 'SETTINGS', root / 'menu-settings.json'), \
                    patch.object(menu, 'package_manifest', return_value={'name': 'new'}), \
                    patch.object(menu, 'discover', side_effect=AssertionError('Unexpected search')):
                old = dict(package=str(root / 'old.pkg'), program=None)
                menu.save_settings(dict(compiled_stations=[old]))
                menu.register_build(root / 'new.pkg', root / 'Program')
                records = menu.load_settings()['compiled_stations']
                self.assertEqual(records[0], old)
                self.assertEqual(records[1]['package'], str(root / 'new.pkg'))

    def test_search_replaces_only_its_list_and_errors_preserve_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            previous = dict(compiled_stations=[dict(package=str(root.parent / 'outside.pkg'), program=None)],
                            source_stations=[dict(name='old', program=str(root / 'old/Program'))])
            with patch.object(menu, 'SETTINGS', root / 'menu-settings.json'):
                for sources in (False, True):
                    menu.save_settings(previous)
                    before = menu.SETTINGS.read_bytes()
                    name = 'discover_sources' if sources else 'discover'
                    with patch.object(menu, name, side_effect=PermissionError('denied')):
                        with self.assertRaises(PermissionError):
                            menu.refresh_catalog(root, {}, include_sources=sources)
                    self.assertEqual(menu.SETTINGS.read_bytes(), before)
                    with patch.object(menu, name, return_value=[]):
                        current = menu.refresh_catalog(root, {}, include_sources=sources)
                    key = 'source_stations' if sources else 'compiled_stations'
                    other = 'compiled_stations' if sources else 'source_stations'
                    self.assertEqual(current[key], [])
                    self.assertEqual(len(current[other]), 1)

    def test_walk_and_package_io_errors_are_not_suppressed(self):
        def failing_walk(root, onerror):
            onerror(PermissionError('denied'))
            return []
        with patch.object(menu.os, 'walk', side_effect=failing_walk):
            for search in (menu.discover, menu.discover_sources):
                with self.assertRaises(PermissionError):
                    search(Path('generic'))
        with patch.object(menu.os, 'walk', return_value=[('generic', [], ['test.pkg'])]), \
                patch.object(menu, 'package_manifest', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                menu.discover(Path('generic'))

    def test_search_uses_only_configured_tree_and_rereads_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first, second = root / 'first', root / 'second'
            first.mkdir()
            second.mkdir()
            with patch.object(menu, 'ROOT', root), patch.object(menu, 'SETTINGS', root / 'settings.json'), \
                    patch.object(menu, 'CONFIG', root / 'config.json'), \
                    patch.object(menu, 'discover', return_value=[]) as search, \
                    patch('sys.argv', ['station_menu.py']), patch('sys.stdout', new_callable=io.StringIO):
                menu.save_search_folder(first)
                def choose(_):
                    if search.called:
                        return '0'
                    menu.save_search_folder(second)
                    return '1'
                with patch('builtins.input', side_effect=choose):
                    menu.main()
                search.assert_called_once_with(second)

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
                menu.refresh_catalog(root, config)
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
                    patch.object(menu, 'build_output', return_value=root / 'output'), \
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
            direct = root / 'top.pkg'
            direct.write_bytes(data)
            (root / 'truncated.pkg').write_bytes(data[:-1])
            (root / 'trailing.pkg').write_bytes(data + b'x')
            (root / 'other.pkg').write_bytes(b'other format')
            self.assertEqual(discover(root), [(valid.resolve(), {'name': 'generic'}),
                                              (direct.resolve(), {'name': 'generic'})])

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
