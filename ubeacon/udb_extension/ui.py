import functools
import os

import gdb

from . import ubeacon, tui_windows


@tui_windows.register_window("python-source")
class PythonSourceWindow(tui_windows.ScrollableWindow):
    title = "Python Source"

    def get_content(self):
        if len(ubeacon.backtrace) == 0:
            return "No source code available"

        frame = ubeacon.backtrace.frames[0]
        filename = frame.file_name
        line = frame.line
        lines = ubeacon.get_source_file_content(filename, line_nos=True, highlight=True).split("\n")
        prefixed_lines = [(" > " if i == line else "   ") + l for i, l in enumerate(lines, start=1)]

        # Set vertical scroll offset to center the current line
        half_window_height = self._tui_window.height // 2
        self.vscroll_offset = line - half_window_height

        return "\n".join(prefixed_lines)


@tui_windows.register_window("python-backtrace")
class PythonBacktraceWindow(tui_windows.ScrollableWindow):
    title = "Python Backtrace"

    def get_content(self):
        return gdb.execute("upy bt", to_string=True)


@tui_windows.register_window("python-locals")
class PythonLocalsWindow(tui_windows.ScrollableWindow):
    title = "Local Python Variables"

    def get_content(self):
        return gdb.execute("upy info locals", to_string=True)


# Define a layout with all Python windows
gdb.execute(
    " ".join(
        (
            "tui new-layout python",
            "{-horizontal {python-source 2 status 1 cmd 1} 3",
            "             {python-locals 1 python-backtrace 2 timeline 1} 2} 1",
        )
    )
)
