"""
This file contains functions and classes for inspecting and modifying the UDB debuggee.

TODO: talk about whether functions modify or not
"""
import contextlib
import functools
from enum import Enum, auto
from typing import Iterator

import gdb  # ignore: mypy[import-untyped]
from src.udbpy import ctrl_c, engine, report
from src.udbpy.gdb_extensions import gdbutils
from undo.debugger_extensions import udb


def symbol_exists(symbol_name: str) -> bool:
    try:
        gdb.parse_and_eval(symbol_name)
        return True
    except gdb.error:
        return False

def disable_volatile_warning_maybe():
    """
    Context manager to disable the volatile mode warning in GDB when evaluating expressions.

    The context manager udb.volatile_warning_disabled was added in version 9.2,
    so the message cannot be suppressed in older versions.
    """
    _udb = udb._wrapped_udb
    if hasattr(_udb, "volatile_warning_disabled"):
        return _udb.volatile_warning_disabled
    else:
        return contextlib.nullcontext()

class PythonState(Enum):
    """
    Describes the python-relevant state of a debuggee.

    When injecting our runtime `ubeacon` Python library into a record-time debuggee, we need the
    debuggee to be a CPython interpreter, and for that interpreter to be initialised. This `Enum`
    tracks those states. The initialization state is determined by calling the Py_IsInitialized()
    function from the Python C API, but see the documentation
    (https://docs.python.org/3/c-api/init.html) for more specific information.
    """

    NOT_PYTHON = auto()
    """
    The debuggee is not a CPython interpreter.
    """

    NOT_INITIALIZED = auto()
    """
    The debuggee is a CPython interpreter, but isn't initialised yet.
    """

    INITIALIZED = auto()
    """
    The debuggee is a CPython interpreter, and is also initialised.
    """

def python_state() -> PythonState:
    """
    Determine the state of the Python interpreter that is currently being debugged.

    This is the primary heuristic function that is used to identify whether a UDB debuggee is (or
    contains) a Python interpreter. It achieves this by looking for and calling various Python
    symbols.

    Returns:
        The determined state of the Python interpreter.
    """
    if not symbol_exists("Py_BytesMain") and not symbol_exists("Py_IsInitialized"):
        return PythonState.NOT_PYTHON

    try:
        with disable_volatile_warning_maybe():
            is_initialised = (gdb.parse_and_eval("(int)Py_IsInitialized()") == 1)
    except gdb.error:
        # Most of Python's functionality is implemented in the form of a library. If we are at the
        # very early stages of startup in the Python interpreter (or it's not yet started) it's
        # possible that the library hasn't yet been loaded so the Py_IsInitialized function won't
        # yet be present in the debuggee. We can infer that Python is not yet initialized in this
        # case.
        is_initialised = False
    if not is_initialised:
        return PythonState.NOT_INITIALIZED

    return PythonState.INITIALIZED


@contextlib.contextmanager
def allow_pending():
    output = gdb.execute("show breakpoint pending", to_string=True)
    was_on = "on" in output.lower()

    gdb.execute("set breakpoint pending on")
    try:
        yield
    finally:
        if was_on:
            gdb.execute("set breakpoint pending on")
        else:
            gdb.execute("set breakpoint pending off")


def is_python() -> bool:
    """
    Determines if the current debuggee is a Python interpreter.

    Returns:
        `True` if the debuggee is a Python interpreter, otherwise `False`.
    """
    return python_state() != PythonState.NOT_PYTHON


def general_registers() -> dict[str, int]:
    """
    Get debuggee's the general purpose registers.

    Returns:
        A dictionary of register names to their values. The names of the registers are the same
        as reported by GDB's `info registers` command.
    """
    arch = gdb.selected_inferior().architecture()
    reg_names = [x.name for x in arch.registers("general")]
    frame = gdb.newest_frame()
    reg_values = {}
    for reg_name in reg_names:
        reg_value = frame.read_register(reg_name)
        uint64_t = gdb.lookup_type("uintptr_t") # TODO arch specific
        if  int(reg_value) < 0:
            reg_value = reg_value.cast(uint64_t)
        reg_values[reg_name] = int(reg_value)
    return reg_values


@contextlib.contextmanager
def injected_string(data: str) -> Iterator[int]:
    """
    A context manager for temporarily injecting strings into an Undo recorded process.

    This function calls malloc in the debuggee, then copies the provided string into the
    newly allocated memory. The context manager variable contains a pointer in the
    debuggee's address space that points to the provided string. When the context manager
    ends, the allocated memory is freed. All the effects of this context manager on the
    debuggee will be recorded in the event log, and will be reproduced at record time.

    Args:
        data: A string to be copied into the debuggee.
    """
    report.dev2(f"Injecting string: {data!r}")
    c_str_len = len(data) + 1 # +1 for the NULL terminator

    malloc = Function.from_symbol("malloc")
    free = Function.from_symbol("free")

    result = malloc(c_str_len)
    report.dev2(f"Malloc done: {data!r}, {result}")
    gdb.execute(f"set {{char[{c_str_len}]}}{result} = \"{data}\"", to_string=True)
    yield result
    free(result)
    report.dev2(f"Free done: {data!r}")

class _GeneralRegisters:
    """
    A simple helper class for setting and restoring general purpose debuggee registers.
    """

    def __init__(self) -> None:
        self._initial_regs = general_registers()

    def __getitem__(self, key: str) -> int:
        """
        Get the current value of a register.
        """
        assert key in self._initial_regs.keys(), f"Unknown key: {key}"
        value = gdb.newest_frame().read_register(key)
        report.dev2(f"Read register {key}={value}")
        return int(value)

    def __setitem__(self, key: str, value: int) -> None:
        """
        Set the current value of a register.
        """
        report.dev2(f"Setting register {key} to 0x{value:x}")
        assert key in self._initial_regs.keys(), f"Unknown key: {key}"
        gdb.execute(f"set ${key}=0x{value:x}", to_string=True)

    @property
    def initial_pc(self) -> int:
        return self._initial_regs["rip"]

    def restore(self):
        """
        Restore the registers to the state they were in when this class instance was initialized.
        """
        report.dev2(f"Restoring registers to {self._initial_regs}")
        for name, value in self._initial_regs.items():
            report.dev2(f"setting register {name}={value}")
            gdb.execute(f"set ${name}={value}", to_string=True)


@contextlib.contextmanager
def temporary_registers() -> Iterator[_GeneralRegisters]:
    """
    A context manager that saves and restores the debuggee's general purpose registers.

    In order to be able to call functions in the debuggee that are recorded in the event log, we
    need to be able to set registers, run the debuggee for a bit, then restore the original
    registers so the debuggee can continue as if nothing happened. This context manager ensures that
    we take care of restoring registers during such an operation.

    Returns:
        A helper class for getting/setting the registers. See `_GeneralRegisters`
    """
    regs = _GeneralRegisters()
    yield regs
    regs.restore()


@contextlib.contextmanager
def temporary_memory(addr: int , data: list[int]) -> Iterator[None]:
    """
    A context manager that temporarily writes some data into memory

    TODO: explain why this is needed more
    """

    read_cmd = f"print/d *(unsigned char*){hex(addr)}@{len(data)}"
    orig_data_str = gdb.execute(read_cmd, to_string=True)
    orig_data = [int(x) for x in orig_data_str.split('=')[-1].strip()[1:-1].split(',')]
    write_cmd = f"set {{char[{len(data)}]}} {hex(addr)} = {{" + ", ".join(map(str, data)) + "}"
    gdb.execute(write_cmd, to_string=True)
    yield
    restore_cmd = f"set {{char[{len(orig_data)}]}} {hex(addr)} = {{" + ", ".join(map(str, orig_data)) + "}"
    gdb.execute(restore_cmd, to_string=True)


class Function:
    @classmethod
    def from_symbol(cls, name: str) -> "Function":
        """
        Initialise a Function object from a symbol rather than an address.

        Args:
            name: The name of the function to be called in the debuggee.
        """
        try:
            report.dev1(f"Setting up recorded call to: {name}")
            addr = int(gdb.parse_and_eval(name).address)
            return cls(addr, name)
        except Exception:
            print(f"Couldn't find symbol: {name}")
            raise

    def __init__(self, addr: int, name: str | None = None) -> None:
        self._addr = addr
        self._name = name
    
    def _call_rax_indirect(self, addr: int) -> list[int]:
        # The nop is important here as this code will be injected at the top of an executable
        # map. Without the nop the debuggee will return to a SIGSEGV as it runs over the end of the
        # map. With the nop we can set a breakpoint and move the PC elsewhere.
        return [
            0xff, 0xd0, # call rax
            0x90,       # nop
        ]

    def _code(self, *args: int) -> tuple[list[int], int]:
        code = [
            *self._call_rax_indirect(self._addr),
        ]
        return code, len(code)

    @functools.cache
    def _find_executable_space(self, len: int) -> int:
        # this is lifted straigt from out engnie implementation of info prog maps
        maps = []
        udb_obj = udb._wrapped_udb
        with ctrl_c.deferred():
            udb_obj.gdbserial.send("vUDB;get_debuggee_maps")
            with udb_obj.gdbserial.receive_packet():
                n = udb_obj.gdbserial.receive_number()
                for _ in range(n):
                    m = engine.MemoryMap(
                        begin=udb_obj.gdbserial.receive_number(),
                        end=udb_obj.gdbserial.receive_number(),
                        offset=udb_obj.gdbserial.receive_number(),
                        dev_major=udb_obj.gdbserial.receive_number(),
                        dev_minor=udb_obj.gdbserial.receive_number(),
                        inode=udb_obj.gdbserial.receive_number(),
                        path=udb_obj.gdbserial.receive_hexstr(),
                        read=bool(udb_obj.gdbserial.receive_number()),
                        write=bool(udb_obj.gdbserial.receive_number()),
                        execute=bool(udb_obj.gdbserial.receive_number()),
                        shared=bool(udb_obj.gdbserial.receive_number()),
                    )
                    maps.append(m)
        for map in maps:
            if map.path.startswith('['):
                continue
            if map.execute:
                # does this map have enough free space at the top end?
                data = gdb.selected_inferior().read_memory(map.end - len, len)
                if list(data) == [b"\x00" for _ in range(len)]:
                    return map.end - len
        assert False, "Can't find inject location"

    def __call__(self, *args: int) -> int:
        code, offset = self._code(*args)
        addr = self._find_executable_space(len(code))
        with (
                temporary_memory(addr, code),
                temporary_registers() as regs,
                gdbutils.breakpoints_suspended()
        ):
            assert 0 <= len(args) <= 6, f"Only 0-6 args supported in debuggee calls: {args}"
            arg_regs = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]
            for i, reg_name in enumerate(arg_regs):
                if i >= len(args):
                    break
                regs[reg_name] = args[i]
            regs["rax"] = self._addr
            
            x = gdb.Breakpoint(f"*{hex(addr + offset)}", internal=True)
            x.silent = True

            gdb.execute(f"set $pc = {hex(addr)}")
            gdb.execute("continue", to_string=True)

            assert x.hit_count == 1, f"Call failed: 0x{self._addr:x}"
            x.delete()

            return regs["rax"]
