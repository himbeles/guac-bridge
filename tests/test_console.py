"""Version mismatches are highlighted without forcing ANSI into plain output."""
import io
import logging
import os
import unittest
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
