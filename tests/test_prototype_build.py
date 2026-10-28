"""The standalone design must follow screen presentation without changing its mock actions."""

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('prototype_build', ROOT / 'scripts/build-prototype.py')
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class PrototypeBuildTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='r1-prototype-build-')
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / 'ui/assets').mkdir(parents=True)
        (self.root / 'docs/prototypes').mkdir(parents=True)
        self.css = self.root / 'ui/assets/panel.css'
        self.js = self.root / 'ui/assets/panel.js'
        self.shell = self.root / 'docs/prototypes/shell.css'
        self.target = self.root / 'docs/prototypes/r1-small-screen.html'
        self.css.write_text('.screen-nav{gap:0}\n', encoding='utf-8')
        self.shell.write_text('body{display:block;overflow:auto}\n', encoding='utf-8')
        self.js.write_text('  const icons = {\n    thermal: \'<path d="M12 4v16"/>\',\n  };\n'
                           '  const icon = name => icons[name];\n', encoding='utf-8')
        self.original = ('<!doctype html><meta http-equiv="Content-Security-Policy" content="connect-src \'none\'">'
                         '<style>.old{display:none}</style>\n<script>\n'
                         '    const icons={thermal:\'<path d="M4 4v4"/>\'};\n'
                         '    const icon=name=>icons[name];\n'
                         '    const state={offline:true};\n</script>')
        self.target.write_text(self.original, encoding='utf-8')

    def test_current_styles_and_temperature_icon_replace_stale_copies(self):
        self.assertTrue(BUILD.sync_prototype(self.root))
        actual = self.target.read_text(encoding='utf-8')
        self.assertIn(self.css.read_text(encoding='utf-8').strip(), actual)
        self.assertIn(self.shell.read_text(encoding='utf-8').strip(), actual)
        self.assertIn('M12 4v16', actual)
        self.assertNotIn('M4 4v4', actual)
        self.assertNotIn('.old', actual)
        self.assertIn('const icon = name => icons[name];', actual)
        self.assertIn('const state={offline:true};', actual)
        self.assertIn("connect-src 'none'", actual)

    def test_check_reports_drift_without_rewriting(self):
        with self.assertRaisesRegex(ValueError, 'build-prototype.py'):
            BUILD.sync_prototype(self.root, check=True)
        self.assertEqual(self.target.read_text(encoding='utf-8'), self.original)

    def test_regeneration_is_stable_and_new_source_changes_are_detected(self):
        BUILD.sync_prototype(self.root)
        first = self.target.read_bytes()
        self.assertFalse(BUILD.sync_prototype(self.root))
        self.assertFalse(BUILD.sync_prototype(self.root, check=True))
        self.assertEqual(first, self.target.read_bytes())
        self.css.write_text('.screen-nav{gap:4px}\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            BUILD.sync_prototype(self.root, check=True)
        self.assertTrue(BUILD.sync_prototype(self.root))
        self.assertIn('gap:4px', self.target.read_text(encoding='utf-8'))

    def test_ambiguous_or_missing_source_icons_do_not_rewrite_the_prototype(self):
        valid = self.js.read_text(encoding='utf-8')
        for source in ('const absent = {};', valid + '\n' + valid):
            with self.subTest(source=source):
                self.js.write_text(source, encoding='utf-8')
                with self.assertRaises(ValueError):
                    BUILD.sync_prototype(self.root)
                self.assertEqual(self.target.read_text(encoding='utf-8'), self.original)

    def test_legacy_style_copies_are_consolidated_without_leaving_old_overrides(self):
        self.target.write_text(self.original + '\n  <style>.old-override{gap:99px}</style>\n', encoding='utf-8')
        BUILD.sync_prototype(self.root)
        result = self.target.read_text(encoding='utf-8')
        self.assertEqual(result.count('<style>'), 1)
        self.assertNotIn('old-override', result)
        self.assertNotRegex(result, r'(?m)^[ \t]+$')
        self.assertIn('const state={offline:true};', result)

    def test_missing_target_blocks_do_not_partially_rewrite(self):
        for target in (self.original.replace('<style>', '<style media="screen">'),
                       self.original.replace('const icons=', 'const other=')):
            with self.subTest(target=target):
                self.target.write_text(target, encoding='utf-8')
                with self.assertRaises(ValueError):
                    BUILD.sync_prototype(self.root)
                self.assertEqual(self.target.read_text(encoding='utf-8'), target)


if __name__ == '__main__':
    unittest.main()
