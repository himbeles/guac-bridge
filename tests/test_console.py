"""Version mismatches are highlighted without forcing ANSI into plain output."""
import io
import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from guac_bridge.console import AMBER, RESET, Console, StatusFormatter


class TTY(io.StringIO):
    def isatty(self):
        return True


class ConsoleTests(unittest.TestCase):
    def render(self, stream):
        record = logging.LogRecord('test', logging.WARNING, '', 0,
            'Server version mismatch: continuing', (), None)
        return StatusFormatter(Console(stream)).format(record)

    def test_entire_version_warning_is_bold_amber_on_color_terminal(self):
        with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), \
                patch('guac_bridge.console._enable_windows_colors', return_value=True):
            message = self.render(TTY())
        self.assertTrue(message.startswith('\x1b[1m' + AMBER))
        self.assertTrue(message.endswith(RESET))
        self.assertIn('Server version mismatch', message)

    def test_no_color_and_nonterminal_output_remain_plain(self):
        with patch.dict(os.environ, {'NO_COLOR': '1', 'TERM': 'xterm'}, clear=True):
            self.assertNotIn('\x1b', self.render(TTY()))
        self.assertIn('[warning]', self.render(io.StringIO()))
        self.assertNotIn('\x1b', self.render(io.StringIO()))

    def test_hook_uses_existing_windows_console_only_for_discarded_output(self):
        import io
        from guac_bridge import console
        discarded = io.StringIO()
        terminal = io.StringIO()
        for kind in (0, 1, 2, 3):
            with patch.object(console.sys, 'platform', 'win32'), \
                 patch.object(console.sys, 'stderr', discarded), \
                 patch.dict('sys.modules', {'msvcrt': SimpleNamespace(get_osfhandle=lambda fd: 1)}), \
                 patch('ctypes.windll', SimpleNamespace(kernel32=SimpleNamespace(GetFileType=lambda h: kind)), create=True), \
                 patch.object(discarded, 'fileno', return_value=1), \
                 patch.object(console, '_windows_console_output', return_value=terminal) as target:
                stream = console.hook_output_stream()
                self.assertIs(stream, terminal if kind in (0, 2) else discarded)
                self.assertEqual(target.called, kind in (0, 2))
