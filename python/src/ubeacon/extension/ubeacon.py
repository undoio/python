"""
Functions and classes for interacting with the UBeacon record time library.

The UBeacon library is a Python library written with the CPython C API. This library needs
to be loaded into the debuggee at record time, and it inserts hooks at various points into
CPython using the `PyEval_SetTrace()` API. The idea is that UDB can then set conditional
breakpoints on these trace functions which correspond to 'normal' debugging operations in
Python code (next/step/finish etc.).
"""

import contextlib
import functools
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator, Type, TypeVar

import gdb  # pyright: ignore[reportMissingModuleSource]
import pydantic
import pygments
import pygments.formatters
import pygments.lexers
from src.udbpy import locations, report  # pyright: ignore[reportMissingModuleSource]
from src.udbpy.gdb_extensions import gdbutils  # pyright: ignore[reportMissingModuleSource]

from . import debuggee, messages

PREFIX = "s_ubeacon"
STATE_STRUCT = PREFIX
TRACE_PREFIX = f"{PREFIX}_trace"
LINE_FN = f"{TRACE_PREFIX}_line"
CALL_FN = f"{TRACE_PREFIX}_call"
RET_FN = f"{TRACE_PREFIX}_ret"
EXCEPTION_FN = f"{TRACE_PREFIX}_exception"


@functools.cache
def build() -> Path:
    """
    Build the UBeacon library for the current version of Python.
    """
    assert debuggee.is_python()
    progspace = gdb.current_progspace()
    assert progspace is not None
    assert progspace.executable_filename is not None  # type: ignore[attr-defined]
    python_executable = progspace.executable_filename  # type: ignore[attr-defined]
    root = Path(__file__).resolve().parent.parent.parent.parent

    # location of built library
    cache_dir = locations.get_undo_cache_path("ubeacon")

    # See if it's already built
    output = subprocess.check_output(
        [python_executable, "find_so.py", cache_dir], text=True, cwd=root
    ).strip()
    if output:
        lib_path = Path(output)
        if lib_path.is_file():
            lib_dir = root / "src" / "ubeacon" / "lib"
            # Check the timestamp of the library is newer than the source files
            # if not, we need to rebuild
            lib_mtime = lib_path.stat().st_mtime
            source_files = list(lib_dir.glob("*.c")) + list(lib_dir.glob("*.h"))
            if all(lib_mtime > source.stat().st_mtime for source in source_files):
                return lib_path

    # Not found, build it
    try:
        subprocess.run(
            [
                python_executable,
                "setup.py",
                "build",
                "--quiet",
                f"--build-base={cache_dir}",
            ],
            text=True,
            cwd=root,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.output:
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                tf.write(exc.output)
                tf.flush()
                report.user(
                    f"Saved stdout to {tf.name}.\n"
                )
        if exc.stderr:
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                tf.write(exc.stderr)
                tf.flush()
                report.user(
                    f"Saved stderr to {tf.name}.\n"
                )
        raise report.ReportableError(
            """Error occurred in Python: could not debug this version of Python.
                                     
            You may need to install Python development headers for this version."""
        )
    output = subprocess.check_output(
        [python_executable, "find_so.py", cache_dir], text=True, cwd=root
    )
    lib_path = Path(output.strip())
    assert lib_path.is_file(), f"Cannot find ubeacon library: {lib_path}"
    return lib_path


def require() -> None:
    """
    Guard function that raises exception if the UBeacon library is not loaded.
    """
    if not debuggee.symbol_exists(STATE_STRUCT):
        raise report.ReportableError(messages.UBEACON_REQUIRED)


@contextlib.contextmanager
def startup_file() -> Iterator[Path]:
    ubeacon_path = build()
    startup_file_content = f"""
\"\"\"
This code is injected into a running Python program by UDB.

The `ubeacon` Python library must be present at record time, and this file is responsible for
loading it.
\"\"\"

import importlib
import importlib.util
import os

from pathlib import Path

module_name = "ubeacon"
module_path = \"{ubeacon_path}\"

if not module_path:
   raise FileNotFoundError("Can't find the ubeacon library. Is the $UBEACON environment variable set?")

if not Path(module_path).exists():
   raise FileNotFoundError(f"Can't find the ubeacon library. {{module_path}} does not exist.")

spec = importlib.util.spec_from_file_location(module_name, module_path)
assert spec
ubeacon = importlib.util.module_from_spec(spec)
try:
    ubeacon.start()
    pass
except Exception as e:
    print(f"Failed to start UBeacon library: {{e}}")
"""

    with tempfile.NamedTemporaryFile() as tf:
        temp_file = Path(tf.name)
        temp_file.write_text(startup_file_content)
        yield temp_file


T = TypeVar("T", bound=pydantic.BaseModel)
"""
A generic type for use with `_call_dump_function`
"""


def _call_dump_function(func_name: str, model_type: Type[T]) -> T:
    """
    Call any ubeacon function that writes a temporary file filed with JSON data.

    The UBeacon library contains an assortment of functions that dump interesting Python
    interpreter state to a file. This function calls one of these functions - provided
    it's name follows the correct convention, reads the file and loads the JSON into an
    appropriate Pydantic model.
    """
    require()

    # TODO: we should probably take the GIL here.
    with tempfile.NamedTemporaryFile() as temp_file, debuggee.disable_volatile_warning_maybe():
        cmd = f'call {PREFIX}_interact_{func_name}("{temp_file.name}")'
        gdb.execute(cmd)
        content = Path(temp_file.name).read_text()

    model = model_type(**json.loads(content))
    return model


def evaluate(code: str) -> str:
    with tempfile.NamedTemporaryFile() as temp_file, debuggee.disable_volatile_warning_maybe():
        cmd = f'call {PREFIX}_interact_eval("{temp_file.name}", "{code}")'
        gdbutils.execute_to_string(cmd)
        return Path(temp_file.name).read_text()


class Frame(pydantic.BaseModel):
    """
    Represents a single frame in a Python backtrace.

    This Pydantic model is used to deserialize the JSON object dumped by the
    `s_ubeacon_frame_json()` function in the UBeacon C library.
    """

    frame_no: int
    func_name: str
    file_name: Path
    line: int

    def __str__(self) -> str:
        """
        Stringifies this `Frame` object in a way familiar to Python developers.
        """
        try:
            contents = get_source_file_content(
                self.file_name, line_nos=False, highlight=True
            )
            source_lines = contents.splitlines()
            source_line = source_lines[self.line - 1]
        except Exception:
            source_line = "<no source available>"

        return "\n".join(
            [
                f'  #{self.frame_no} File "{self.file_name}",'
                f' line {self.line}, in {self.func_name}',
                f"    {source_line.lstrip()}",
            ]
        )


class Backtrace(pydantic.BaseModel):
    """
    Represents a Python backtrace.

    This Pydantic model is used to deserialize the JSON object delivered by the
    `s_ubeacon_backtrace_json()` function in the UBeacon C library.
    """

    frames: list[Frame]

    def __str__(self) -> str:
        """
        Stringifies this `Backtrace` object in a way familiar to Python developers.
        """
        if len(self) == 0:
            return "No Python traceback available."
        else:
            return "\n".join(
                [
                    "Traceback (most recent call first):",
                    *[str(frame) for frame in self.frames],
                ]
            )

    def __len__(self) -> int:
        return len(self.frames)

    @classmethod
    def from_gdb(cls) -> "Backtrace":
        return _call_dump_function("backtrace_json", model_type=cls)


@functools.cache
def get_cached_source_file_content(file_name: Path) -> str:
    return file_name.read_text()


@functools.cache
def get_source_file_content(
    file_name: Path, line_nos: bool = False, highlight: bool = False
) -> str:
    """
    Opens, reads and returns a file from the local machine.

    Args:
        file_name: The file to be loaded.
        line_nos: If true, each line wil be prefixed by a one-indexed line number.
        highlight: If true, this file will be highlighed as a Python source file for
                   printing in a terminal
    """
    content = get_cached_source_file_content(file_name)
    if not line_nos and not highlight:
        return content

    lexer = pygments.lexers.TextLexer()  # pylint: disable=no-member
    if highlight:
        lexer = pygments.lexers.PythonLexer(stripnl=False)  # pylint: disable=no-member

    return pygments.highlight(
        content, lexer, pygments.formatters.TerminalFormatter(linenos=line_nos)  # pylint: disable=no-member
    )


class Local(pydantic.BaseModel):
    name: str
    value: str

    def __str__(self) -> str:
        return f"{self.name} = {self.value}"


class LocalList(pydantic.BaseModel):
    locals: list[Local]

    def __str__(self) -> str:
        if len(self) == 0:
            return "No locals."
        else:
            return "\n".join(
                [
                    "Locals:",
                    *[f" {local}" for local in self.locals],
                ]
            )

    def __len__(self) -> int:
        return len(self.locals)

    @classmethod
    def from_gdb(cls) -> "LocalList":
        """
        Get a list of local variables from the current Python frame.
        """
        return _call_dump_function("locals_json", model_type=cls)


def stop_message() -> str:
    """
    Generates a message describing the current location in Python source.
    """
    if not state.backtrace.frames:
        return "No Python frame."
    return str(state.backtrace.frames[0])


def one_frame_up() -> str:
    current_frame = int(gdb.parse_and_eval("s_ubeacon")["current_frame"])
    return f"{STATE_STRUCT}.returned_from == {current_frame}"


def stay_in_frame() -> str:
    current_frame = int(gdb.parse_and_eval("s_ubeacon")["current_frame"])
    return f"{STATE_STRUCT}.current_frame == {current_frame}"


def exception_origin(exception_name: str | None) -> str:
    exception_origin = f"(uint32_t){STATE_STRUCT}.exception_origin == 1"

    if exception_name:
        exception_type = (
            f"{STATE_STRUCT}.exception_type_id == {_simple_hash(exception_name)}"
        )
        return f"{exception_type} && {exception_origin}"
    else:
        return exception_origin


def first_line_of_file() -> str:
    return f"{STATE_STRUCT}.current_line == 1"


def _simple_hash(data_str: str) -> int:
    """
    An implementation of the FNV-1 hash. Must match that in ubeacon.c's s_ubeacon_simple_hash().

    This function is not intended to be cryptographically secure, rather to map a string onto an
    integer in a way that's reasonably unlikely to collide. We do this as we can't set
    conditional breakpoints on string comparisons.
    """
    hash_value = 0xCBF29CE484222325
    prime = 0x100000001B3
    for c in data_str:
        hash_value ^= ord(c)
        hash_value *= prime
        hash_value &= 0xFFFFFFFFFFFFFFFF
    return hash_value


class _BreakpointInternal(gdb.Breakpoint):
    def __init__(self, location: str, condition: str | None = None) -> None:
        with debuggee.allow_pending():
            super().__init__(location, internal=True)
            self.silent = True

            if condition:
                self.condition = condition

    @property
    def hit(self) -> bool:
        return self.hit_count > 0


@contextlib.contextmanager
def internal_breakpoint(
    show_message: bool = True,
    condition: str | None = None,
    location: str = LINE_FN,
) -> Iterator[_BreakpointInternal]:
    """
    Context manager for breakpoints used for programatic interaction with Python code.

    This context manager sets a silent, internal breakpoint on the requested location before
    returning control to the caller. Once the context manager is exited, the breakpoint is deleted.
    """
    bp = _BreakpointInternal(location, condition)
    try:
        yield bp
    finally:
        hit = bp.hit
        bp.delete()
        if hit and show_message:
            report.user(stop_message())


class ExternalBreakpoint(gdb.Breakpoint):
    """
    Represents a Python breakpoint that will be visible to the user.
    """

    INDEX = 1

    def __init__(self) -> None:
        super().__init__(LINE_FN, internal=True)
        self.index = self.INDEX
        ExternalBreakpoint.INDEX += 1

    @property
    def stop_message(self) -> str:
        """
        A message to be printed when this breakpoint is hit.
        """
        ubeacon = gdb.parse_and_eval("s_ubeacon")
        current_func = ubeacon["current_func"].string()
        current_file = ubeacon["current_file"].string()
        current_line = int(ubeacon["current_line"])
        return f"Python breakpoint {self.index}, {current_func} () at {current_file}:{current_line}"

    def stop(self) -> bool:
        report.user(self.stop_message)
        return True

    @property
    def set_message(self) -> str:
        """
        A message to be printed when this breakpoint is set.

        This message is not printed automatically, the calling code is responsible for printing it
        if required.
        """
        return f"Python breakpoint {self.index} at {self}"


class FileLineBreakpoint(ExternalBreakpoint):
    """
    Describes a Python breakpoint on a file and line number.
    """

    def __init__(self, file: str, line: int) -> None:
        # Setup the GDB breakpoint class
        super().__init__()
        self.silent = True

        self._file = file
        self._line = line
        self.condition = self._build_condition()

    def _build_condition(self) -> str:
        ubeacon = gdb.parse_and_eval("s_ubeacon")
        file_hash = _simple_hash(self._file)
        line_cond = (
            f"*(uint64_t *){int(ubeacon['current_line'].address)} == {self._line}"
        )
        file_cond = (
            f"*(uint64_t *){int(ubeacon['current_file_id'].address)} == {file_hash}"
        )
        return f"{line_cond} && {file_cond}"

    def stop(self) -> bool:
        ubeacon = gdb.parse_and_eval("s_ubeacon")
        file_correct = str(ubeacon["current_file"].string()).endswith(self._file)
        line_correct = int(ubeacon["current_line"]) == self._line
        if file_correct and line_correct:
            # TODO: should this be in a stop handler?
            report.user(self.stop_message)
        return file_correct and line_correct

    def __str__(self) -> str:
        return f"{self._file}:{self._line}"


class FunctionBreakpoint(ExternalBreakpoint):
    """
    Describes a Python breakpoint on a function.
    """

    def __init__(self, func: str) -> None:
        # Setup the GDB breakpoint class
        super().__init__()
        self.silent = True

        self._func = func
        self.condition = self._build_condition()

    def _build_condition(self) -> str:
        ubeacon = gdb.parse_and_eval("s_ubeacon")
        func_hash = _simple_hash(self._func)
        func_cond = (
            f"*(uint64_t *){int(ubeacon['current_func_id'].address)} == {func_hash}"
        )
        first_line_cond = f"*(uint64_t *){int(ubeacon['first_line'].address)} == 1"
        return f"{func_cond} && {first_line_cond}"

    def stop(self) -> bool:
        ubeacon = gdb.parse_and_eval("s_ubeacon")
        func_correct = str(ubeacon["current_func"].string()).endswith(self._func)
        first_correct = int(ubeacon["first_line"]) == 1
        if func_correct and first_correct:
            # TODO: should this be in a stop handler?
            report.user(self.stop_message)
        return func_correct and first_correct

    def __str__(self) -> str:
        return f"{self._func} ()"


def ready() -> None:
    gdb.events.stop.connect(_stop_handler)
    global active
    active = True


def clear() -> None:
    gdb.events.stop.disconnect(_stop_handler)
    global active
    active = False


def _stop_handler(event: gdb.StopEvent) -> None:
    """GDB event handler called when execution stops."""

    # The cached debuggee state may no longer be valid, so clear it.
    state.clear()


breakpoints: list[ExternalBreakpoint] = []
active: bool = False


class DebuggeeState:
    """(Cached) state of the debuggee."""

    _backtrace: Backtrace | None = None
    """Latest Python backtrace.
    If not currently executing Python code, the list of frames will be empty.
    Lazily updated on request."""

    locals: LocalList = LocalList(locals=[])
    """Latest list of local variables in the current Python frame."""

    @property
    def backtrace(self) -> Backtrace:
        """
        The current Python backtrace.

        This is updated on every stop event, so will reflect the current state of the debuggee.
        """
        if self._backtrace is None:
            if debuggee.symbol_exists(STATE_STRUCT):
                self._backtrace = Backtrace.from_gdb()
                self.locals = LocalList.from_gdb()
            else:
                self._backtrace = Backtrace(frames=[])
                self.locals = LocalList(locals=[])

        return self._backtrace

    def clear(self) -> None:
        """
        Clear all cached debuggee state.
        """
        self._backtrace = None
        self.locals = LocalList(locals=[])


state = DebuggeeState()
