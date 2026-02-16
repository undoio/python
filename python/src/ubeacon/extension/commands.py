import gdb # pyright: ignore[reportMissingModuleSource]

import functools
import contextlib

from typing import Callable, Iterator

from src.udbpy.gdb_extensions import command, command_args, udb_base, gdbutils  # pyright: ignore[reportMissingModuleSource]
from src.udbpy import engine  # pyright: ignore[reportMissingModuleSource]
from src.udbpy import report  # pyright: ignore[reportMissingModuleSource]

from . import debuggee, messages, ubeacon, ui

command.register_prefix(
    "uexperimental python",
    gdb.COMMAND_NONE,
    """
    Commands for reversible debugging on Python code.

    Python support in UDB is currently in the engineering preview stage, and is not
    officially supported.
    """,
    aliases=["upy"],
)

command.register_prefix(
    "uexperimental python go",
    gdb.COMMAND_NONE,
    """
    Jump to a specific point in the Python execution history.
    """,
    aliases=["upy go"],
)

command.register_prefix(
    "uexperimental python go exception",
    gdb.COMMAND_NONE,
    """
    Navigate between Python exceptions.
    """,
    aliases=["uexperimental python go except", "uexperimental python go ex"],
)

command.register_prefix(
    "uexperimental python info",
    gdb.COMMAND_NONE,
    """
    Generic command for showing things about the Python application being debugged.
    """,
    aliases=["upy inf", "upy i"],
)

def check_active() -> None:
    if not ubeacon.active:
        raise report.ReportableError('UDB Python debugging is not enabled. Type "upy start" to start recording.')

@command.register(gdb.COMMAND_STATUS, repeat=False)
def uexperimental__python__status(udb: udb_base.Udb) -> None:
    """
    Inspect the current Python debugging status of the debuggee.

    Reversible Python debugging is not enabled by default. UDB requires additional
    instrumentation in the form of a runtime library injected into the Python interpreter
    at record time. This command looks at the current debuggee to determine if it's a
    initialised Python interpreter or not, and whether the instrumentation library has
    been loaded.
    """

    execution_mode = udb.get_execution_mode()
    recording_or_replaying = execution_mode == engine.ExecutionMode.RECORDING or execution_mode.replaying

    if not recording_or_replaying:
        mode_message = execution_mode.value.message
        report.user(messages.STATUS_BAD_MODE_FMT.format(mode_message=mode_message))
        return

    python_state = debuggee.python_state()
    match python_state:
        case debuggee.PythonState.NOT_PYTHON:
            msg = messages.STATUS_NOT_PYTHON
        case debuggee.PythonState.NOT_INITIALIZED:
            msg = messages.STATUS_PYTHON_NEEDS_INIT
        case debuggee.PythonState.INITIALIZED if not debuggee.symbol_exists("s_ubeacon"):
            msg = messages.STATUS_PYTHON_READY
        case _:
            msg = messages.STATUS_UBEACON_LOADED
    report.user(msg)


@command.register(gdb.COMMAND_STATUS, repeat=False)
def uexperimental__python__record(udb: udb_base.Udb) -> None:
    """
    Enable debugging of Python code.

    Normal UDB recording must already be enabled. Provided that the debuggee is a running and fully
    initialized Python interpreter, this command will inject and initialise the Undo Python record
    time library. This library is a small debugging library that allows UDB to accurately unwind and
    query CPython interpreter state.

    If the debuggee is not a Python interpreter, or is not in a good state, an error
    message will be printed.
    """
    with contextlib.suppress(Exception):
        ubeacon.clear()
    report.dev2("Injecting")

    # We first check that the debuggee is a Python interpreter that is initialized.
    python_state = debuggee.python_state()
    if python_state != debuggee.PythonState.INITIALIZED:
        raise report.ReportableError(messages.STATUS_PYTHON_READY)

    if debuggee.symbol_exists("s_ubeacon"):
        return # ubecaon library already injected, do nothing

    @contextlib.contextmanager
    def aquire_python_gil() -> Iterator[None]:
        PyGILState_Ensure = debuggee.Function.from_symbol("PyGILState_Ensure")
        PyGILState_Release = debuggee.Function.from_symbol("PyGILState_Release")
        lock = PyGILState_Ensure()
        yield
        PyGILState_Release(lock)

    open_args = "rb"
    with (
        gdbutils.breakpoints_suspended(),
        ubeacon.startup_file() as startup_file,
        debuggee.injected_string(str(startup_file)) as filename_ptr,
        debuggee.injected_string(open_args) as open_args_ptr,
        aquire_python_gil(),
    ):
        fopen = debuggee.Function.from_symbol("fopen")
        handle = fopen(filename_ptr, open_args_ptr)
        assert handle > 0, "Failed to open file"
        PyRun_SimpleFileEx = debuggee.Function.from_symbol("PyRun_SimpleFileEx")
        PyRun_SimpleFileEx(handle, filename_ptr, 1) # This closes the open file.

    ubeacon.ready()


@command.register(gdb.COMMAND_RUNNING, repeat=False, arg_parser=command_args.Untokenized())
def uexperimental__python__run(udb: udb_base.Udb, args: str) -> None:
    """
    Run a Python application and enable Python recording.

    USAGE: upy run [args]

    This function is the Python equivalent of UDB's `run` command, it will start the debuggee and
    the `args` will be passed to the application being run.
    """
    gdb.execute(f"upy start {args}", to_string=True)
    gdb.execute("continue")


@command.register(gdb.COMMAND_RUNNING, repeat=False, arg_parser=command_args.Integer())
def uexperimental__python__attach(udb: udb_base.Udb, pid: str) -> None:
    """
    Attach to a Python application and enable Python recording.

    USAGE: upy attach [pid]

    This command is the Python equivalent of UDB's `attach` command, it will attach to the
    interpreter, start recording, and enable Python debugging.
    """
    gdb.execute(f"attach {pid}")
    gdb.execute("upy record")


@command.register(gdb.COMMAND_RUNNING, repeat=False, arg_parser=command_args.Untokenized())
def uexperimental__python__start(udb: udb_base.Udb, args: str) -> None:
    """
    Start a Python application and enable Python recording.

    USAGE: upy start [args]

    This function is the Python equivalent of UDB's `start` command, it will start the debuggee, the
    `args` will be passed to the application being started, and the interpreter will stop once
    initialization has begun.
    """
    init_functions = [
        "Py_Initialize", "Py_InitializeEx", "_Py_InitializeMain", "Py_InitializeFromConfig",
    ]
    init_breakpoints: dict[str, gdb.Breakpoint] = {}

    def enable_init_breakpoints(event: gdb.NewObjFileEvent | None) -> None:
        """
        A function to set breakpoints on entry into Python initialization functions.

        This function is inserted as a GDB new object file event handler (see
        https://sourceware.org/gdb/current/onlinedocs/gdb.html/Events-In-Python.html#Events-In-Python)
        as the Python initialization functions may not be available until the libpython.so
        library has been loaded by the dynamic linker. It's also called directly when the
        `uexperimental python record` is executed so that it gets a chance to set the
        breakpoints immediately if the symbols happen to be available right away.

        Once we have detected that the symbols are available, this event handler is
        removed from GDB and further handling is done by the `complete_initialization`
        stop event handler when one of the breakpoints is hit.
        """
        for init_function in init_functions:
            if not debuggee.symbol_exists(init_function):
                continue # The symbol doesn't exist yet.

            if init_function in init_breakpoints.keys():
                continue # We've already set this init breakpoint

            report.dev2(f"Setting init breakpoint: {init_function}")
            init_breakpoints[init_function] = gdb.Breakpoint(init_function, internal=True)
            init_breakpoints[init_function].silent = True

        all_init_breakpoints_set = len(init_functions) == len(init_breakpoints)
        if all_init_breakpoints_set:
            gdb.events.new_objfile.disconnect(enable_init_breakpoints)

    def complete_initialization(event: gdb.StopEvent) -> None:
        """
        Run the debuggee until Python is initialized.

        This is a GDB stop event handler that waits for one of our Python initialization
        breakpoints (see `enable_init_breakpoints()`) to be hit, before finishing the
        current frame, allowing Python to fully initialize itself.
        """
        if not isinstance(event, gdb.BreakpointEvent):
            # This probably means a signal, just stop and display the signal to the user
            return

        init_breakpoint_hit = list(set(event.breakpoints) & set(init_breakpoints.values()))
        if not init_breakpoint_hit:
            return

        report.dev2("In complete init function")

        gdb.events.stop.disconnect(complete_initialization)
        for init_breakpoint in init_breakpoints.values():
            init_breakpoint.delete()

        finish_breakpoint = gdb.FinishBreakpoint(internal=True)
        finish_breakpoint.silent = True
        udb.execution.cont()

        assert debuggee.python_state() == debuggee.PythonState.INITIALIZED, debuggee.python_state()

    with gdbutils.breakpoints_suspended():
        gdb.events.new_objfile.connect(enable_init_breakpoints)
        gdb.events.stop.connect(complete_initialization)
        enable_init_breakpoints(None)
        gdb.execute(f"run {args}")
        gdb.execute("upy record")


        with ubeacon.InternalBreakpoint(condition="s_ubeacon.current_file[0] != '<'"):
            gdb.execute("continue")

    report.user("Python has been initialized.")


def _goto_boundry_internal(start: bool = True, show_message: bool = True) -> None:
    # TODO: what happens if we can't find any python code?
    with (
            gdbutils.breakpoints_suspended(),
            debuggee.allow_pending(),
            ubeacon.InternalBreakpoint(show_message=show_message)
    ):
        if start:
            gdb.execute("ugo start", to_string=True)
            gdb.execute("continue")
        else:
            gdb.execute("ugo end", to_string=True)
            gdb.execute("reverse-continue")


@command.register(gdb.COMMAND_RUNNING, repeat=False)
def uexperimental__python__go__start(udb: udb_base.Udb) -> None:
    """
    Jump to the first line of Python code executed.
    """
    check_active()
    _goto_boundry_internal(start=True)


@command.register(gdb.COMMAND_RUNNING, repeat=False)
def uexperimental__python__go__end(udb: udb_base.Udb) -> None:
    """
    Jump to the last line of Python code executed.
    """
    check_active()
    _goto_boundry_internal(start=False)


@command.register(gdb.COMMAND_STACK,
                  aliases=["uexperimental python where", "uexperimental python bt"],
                  repeat=False)
def uexperimental__python__backtrace(udb: udb_base.Udb) -> None:
    """
    Print backtrace of all Python stack frames.
    """
    check_active()

    if udb.get_current_tid() != udb.threads.ids(gdb.selected_thread()).tid:
        raise report.ReportableError("Can only backtrace the current thread.")

    report.user(ubeacon.state.backtrace)


@command.register(gdb.COMMAND_DATA, repeat=False)
def uexperimental__python__info__locals(udb: udb_base.Udb) -> None:
    """
    Print local variables of the current Python frame and their values.
    """
    check_active()
    report.user(ubeacon.state.locals)


def _step_internal(move_fn: Callable[[], None]) -> None:
    with ubeacon.InternalBreakpoint():
        move_fn()


@command.register(gdb.COMMAND_RUNNING, aliases=["uexperimental python s"])
def uexperimental__python__step(udb: udb_base.Udb) -> None:
    """
    Step Python code until it reaches a different source line.
    """
    check_active()
    _step_internal(udb.execution.cont)


@command.register(gdb.COMMAND_RUNNING, aliases=["uexperimental python rs"])
def uexperimental__python__reverse_step(udb: udb_base.Udb) -> None:
    """
    Step Python code backwards until it reaches a different source line.
    """
    check_active()
    _step_internal(udb.execution.reverse_cont)


def _finish_internal(move_fn: Callable[[], None], location: str = ubeacon.RET_FN) -> None:
    # In a finish operation we want to run to the next line in the current frame, OR to the
    # first line after this frame (i.e. after function return). We set breakpoints on those two
    # things here.
    stay_in_frame = ubeacon.stay_in_frame()
    step_off_return = False
    with (
        ubeacon.InternalBreakpoint(location=location, condition=stay_in_frame) as next_return,
    ):
        move_fn()
        if next_return.hit:
            step_off_return = True
    if step_off_return:
        # Step to the next source line executed.
        _step_internal(move_fn)


@command.register(gdb.COMMAND_RUNNING, aliases=["uexperimental python fin"])
def uexperimental__python__finish(udb: udb_base.Udb) -> None:
    """
    Execute until current Python stack frame returns.
    """
    check_active()
    _finish_internal(move_fn=udb.execution.cont)


@command.register(gdb.COMMAND_RUNNING, aliases=["uexperimental python rf", "uexperimental python rfin"])
def uexperimental__python__reverse_finish(udb: udb_base.Udb) -> None:
    """
    Execute backward until just before the current Python stack frame was entered.
    """
    check_active()
    _finish_internal(move_fn=udb.execution.reverse_cont, location=ubeacon.CALL_FN)


def _next_internal(move_fn: Callable[[], None], location: str = ubeacon.RET_FN, ) -> None:
    # In a finish operation we want to run to the next line in the current frame, OR to the
    # first line after this frame (i.e. after function return). We set breakpoints on those two
    # things here.
    stay_in_frame = ubeacon.stay_in_frame()
    step_off_return = False
    with (
        ubeacon.InternalBreakpoint(condition=stay_in_frame) as next_line,
        ubeacon.InternalBreakpoint(location=location, condition=stay_in_frame) as next_return,
    ):
        move_fn()
        if next_line.hit:
            return # We hit another line in this function, we're done.
        if next_return.hit:
            step_off_return = True
    if step_off_return:
        # Step to the next source line executed.
        _step_internal(move_fn)


@command.register(gdb.COMMAND_RUNNING, aliases=["uexperimental python n"])
def uexperimental__python__next(udb: udb_base.Udb) -> None:
    """
    Execute backward until just before the current Python stack frame was entered.
    """
    check_active()
    _next_internal(move_fn=udb.execution.cont)

@command.register(gdb.COMMAND_RUNNING, aliases=["uexperimental python rn"])
def uexperimental__python__reverse_next(udb: udb_base.Udb) -> None:
    """
    Execute backward until just before the current Python stack frame was entered.
    """
    check_active()
    _next_internal(move_fn=udb.execution.reverse_cont)


@command.register(gdb.COMMAND_BREAKPOINTS, aliases=[
        "uexperimental python b",
        "uexperimental python br",
        "uexperimental python bre",
        "uexperimental python brea",
    ],
    arg_parser=command_args.Untokenized(),
)
def uexperimental__python__break(udb: udb_base.Udb, location: str) -> None:
    """
    Set a breakpoint in Python code at the specificed location.

    USAGE: upy break [file:line|function]


    If the provided location is in the format `file:line` the `file` portion must be a full,
    absolute path the file in question. For example, fizzbuzz.py:20 will not work, but
    /home/<user>/code/fizzbuzz.py:20 will.
    """

    check_active()
    is_file_line = ":" in location
    if is_file_line:
        file, line_str = location.split(":")
        line = int(line_str)
        ubeacon.breakpoints.append(ubeacon.FileLineBreakpoint(file, line))
    else:
        if not location:
            raise report.ReportableError(
                "This command requires an argument. See `help upy break` for more information"
            )
        ubeacon.breakpoints.append(ubeacon.FunctionBreakpoint(location))

    report.user(ubeacon.breakpoints[-1].set_message)


@command.register(
    gdb.COMMAND_BREAKPOINTS,
    repeat=False,
    aliases=[
        "uexperimental python info b",
        "uexperimental python info break",
    ],
)
def uexperimental__python__info__breakpoints(udb: udb_base.Udb) -> None:
    """
    Lists all Python breakpoints.
    """
    check_active()
    if len(ubeacon.breakpoints) == 0:
        report.user("No Python breakpoints.")

    for breakpoint in ubeacon.breakpoints:
        report.user(f"{breakpoint.index}: {breakpoint}")


@command.register(
    gdb.COMMAND_BREAKPOINTS,
    repeat=False,
    aliases=[
        "uexperimental python del",
        "uexperimental python d",
    ],
    arg_parser=command_args.Integer(default=0),
)
def uexperimental__python__delete(udb: udb_base.Udb, num: int) -> None:
    """
    Delete all or some Python breakpoints.

    USAGE: uexperimental python delete [NUM]
    NUM is the number of the breakpoint, as listed in `upy info break`.
    To delete all breakpoints, give no argument.
    """
    check_active()
    if len(ubeacon.breakpoints) == 0:
        report.user("No Python breakpoints.")

    delete_all = num == 0
    remaining = []
    did_something = False
    for breakpoint in ubeacon.breakpoints:
        if breakpoint.index == num or delete_all:
            breakpoint.delete()
            did_something = True
        else:
            remaining.append(breakpoint)

    if not did_something:
        if delete_all:
            report.user("No Python breakpoints")
        else:
            report.user(f"No Python breakpoint (number {num})")
    ubeacon.breakpoints = remaining


@command.register(gdb.COMMAND_DATA, arg_parser=command_args.Untokenized())
def uexperimental__python__eval(udb: udb_base.Udb, expr: str) -> None:
    """
    Evaluate a Python expression and print the result.

    When given a Python expression, this function will execute it in a fork of the debugged
    process. This means that any side effects othe expression will not persist in the debug session.
    """
    check_active()
    # Try casting the result to a few common types
    report.user(ubeacon.evaluate(expr))


def _exception_internal(
    move_fn: Callable[[], None], udb: udb_base.Udb, exception_type: str | None
) -> None:
    if not debuggee.symbol_exists(ubeacon.STATE_STRUCT):
        _goto_boundry_internal(start=True, show_message=False)

    with (
        gdbutils.breakpoints_suspended(),
        debuggee.allow_pending(),
        ubeacon.InternalBreakpoint(location=ubeacon.EXCEPTION_FN, show_message=False) as next_exception,
    ):
        next_exception.condition = ubeacon.exception_origin(exception_type)
        move_fn()
    type_name= gdb.parse_and_eval("s_ubeacon.exception_type").string()
    report.user(f"Hit exception of type {type_name}")
    report.user(ubeacon.stop_message())



@command.register(gdb.COMMAND_RUNNING,
                  arg_parser=command_args.String(default=None))
def uexperimental__python__go__exception__next(udb: udb_base.Udb, exception_type: str | None) -> None:
    """
    Jump to the next thrown exception.

    This command optionally takes an argument containing the type of exception you wish to navigate
    to. For example, `upy go exception next AssertionError` will jump to the next AssertionError
    that was thrown, skipping over any other exception types. If this argument is ommitted, the
    debugger will stop at every exception that was thrown.
    """
    check_active()
    _exception_internal(udb.execution.cont, udb, exception_type)


@command.register(gdb.COMMAND_RUNNING,
                  arg_parser=command_args.String(default=None))
def uexperimental__python__go__exception__prev(udb: udb_base.Udb, exception_type: str | None) -> None:
    """
    Jump to the previously thrown exception.

    This command optionally takes an argument containing the type of exception you wish to navigate
    to. For example, `upy go exception prev AssertionError` will jump to the last AssertionError
    that was thrown, skipping over any other exception types. If this argument is ommitted, the
    debugger will stop at every exception that was thrown.
    """
    check_active()
    _exception_internal(udb.execution.reverse_cont, udb, exception_type)


@command.register(gdb.COMMAND_RUNNING)
def uexperimental__python__enable(udb: udb_base.Udb) -> None:
    """
    Manually install the UBeacon UDB handlers for use with Undo recordings.

    In order to use UBeacon with a recording, the UBeacon record time library must be loaded
    manually into the recorded code. When the recording is loaded in UDB, run this command to enable
    Python source debugging.
    """
    ubeacon.ready()


@command.register(
    gdb.COMMAND_RUNNING,
    aliases=[
        "uexperimental python c",
        "uexperimental python fg",
    ],
)
def uexperimental__python__continue(udb: udb_base.Udb) -> None:
    """
    Continue Python program execution.

    Execution will continue until a Python breakpoint is hit, a signal is received,
    or the program terminates.
    """
    check_active()
    gdb.execute("continue")
    report.user(ubeacon.stop_message())


@command.register(
    gdb.COMMAND_RUNNING,
    aliases=[
        "uexperimental python rc",
    ],
)
def uexperimental__python__reverse_continue(udb: udb_base.Udb) -> None:
    """
    Continue Python program execution in reverse.

    Execution will continue backwards until a Python breakpoint is hit, a signal is received,
    or the beginning of the execution history is reached.
    """
    check_active()
    gdb.execute("reverse-continue")

