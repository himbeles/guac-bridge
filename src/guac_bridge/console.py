"""Human-facing client output. Never used by the server protocol helpers."""
import logging
import os
import sys

GREEN = '\x1b[38;2;34;197;94m'
AMBER = '\x1b[33m'
RED = '\x1b[31m'
RESET = '\x1b[0m'


def _enable_windows_colors(stream):
    if sys.platform != 'win32':
        return True
    try:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetConsoleMode.restype = wintypes.BOOL
        kernel.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.SetConsoleMode.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = wintypes.DWORD()
        return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode))
                    and kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError, ValueError):
        return False


class Console:
    def __init__(self, stream=None):
        self.stream = (sys.stderr if stream is None else stream) or sys.stdout or open(os.devnull, 'w')
        self.encoding = getattr(self.stream, 'encoding', None) or 'utf-8'
        self.interactive = self.stream.isatty() and os.environ.get('TERM') != 'dumb'
        self.color = (self.interactive and 'NO_COLOR' not in os.environ
                      and _enable_windows_colors(self.stream))
        try:
            '🐸 ✓ ● × ·'.encode(self.encoding)
            self.unicode = self.interactive
        except (UnicodeEncodeError, LookupError):
            self.unicode = False

    def paint(self, text, color):
        return color + text + RESET if self.color else text

    def compatible(self, text):
        # Older Windows terminals may use an encoding without emoji/glyph support.
        try:
            return text.encode(self.encoding, errors='replace').decode(self.encoding)
        except LookupError:
            return text.encode('ascii', errors='replace').decode('ascii')

    def write(self, text):
        self.stream.write(self.compatible(text) + '\n')
        self.stream.flush()

    def banner(self, mode):
        title = f'🐸 Guac Bridge · {mode}' if self.unicode else f'Guac Bridge - {mode}'
        self.write(self.paint(title, '\x1b[1m' + GREEN))
        self.write('')

    def hint(self):
        self.write('\nPress Ctrl+C to stop.')


class StatusFormatter(logging.Formatter):
    def __init__(self, console):
        super().__init__('%(message)s')
        self.console = console

    def format(self, record):
        message = super().format(record)
        if record.levelno >= logging.ERROR:
            symbol, plain, color = '×', '[error]', RED
        elif record.levelno >= logging.WARNING:
            symbol, plain, color = '!', '[warning]', AMBER
        elif message.startswith(('Connected to Guacamole', 'Server helper ready')):
            symbol, plain, color = '✓', '[ok]', GREEN
        elif message.startswith(('Listening on', 'API available at')):
            symbol, plain, color = '●', '[ready]', GREEN
        else:
            symbol, plain, color = '·', '[info]', ''
        prefix = symbol if self.console.unicode else plain
        if record.levelno == logging.WARNING and message.startswith('Server version mismatch:'):
            return self.console.compatible(self.console.paint(prefix + ' ' + message, '\x1b[1m' + AMBER))
        if color:
            prefix = self.console.paint(prefix, color)
        return self.console.compatible(prefix + ' ' + message)


def _windows_console_output():
    try:
        return open('CONOUT$', 'w', encoding='utf-8', buffering=1)
    except OSError:
        import ctypes
        try:
            if ctypes.windll.kernel32.AttachConsole(0xFFFFFFFF):
                return open('CONOUT$', 'w', encoding='utf-8', buffering=1)
        except (AttributeError, OSError):
            pass
    return None


def hook_output_stream():
    stream = sys.stderr
    if sys.platform != 'win32' or (stream and stream.isatty()):
        return stream
    # Respect intentional redirection to files/pipes. A non-TTY character
    # handle is usually NUL in a Windows SSH Match exec child.
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        get_type = ctypes.windll.kernel32.GetFileType
        get_type.argtypes, get_type.restype = (wintypes.HANDLE,), wintypes.DWORD
        kind = get_type(msvcrt.get_osfhandle(stream.fileno())) if stream else 0
        if kind not in (0, 2):
            return stream
    except (AttributeError, OSError, ValueError):
        return stream
    return _windows_console_output() or stream


def setup_console(mode, *, hook=False):
    console = Console(hook_output_stream() if hook else None)
    handler = logging.StreamHandler(console.stream)
    handler.setFormatter(StatusFormatter(console))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    console.banner(mode)
    return console


def stop_hint():
    Console().hint()
