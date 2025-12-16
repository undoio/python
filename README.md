# Undo Beacon Library #

## Introduction ##

The Undo Beacon library - "UBeacon" - is a Python library that attempts to expose Python interpreter
state to the Undo Engine in a reasonably performant and stable way. This allows basic time travel
debugging of Python code using special commands - implemented in the `udb` tool.

### Improvements over `libpython` ###

This is the [second iteration](https://git.undoers.io/Undo/python-debugging) of Undo's Python
debugging capability. This engineering preview was designed specifically to address the main
limitations of the previous approach. Namely:

- Python interpreter symbols are no longer required. This allows debugging of Python from a wide
  range of sources including distro package managers, `pyenv`, custom builds, and more.

- The versions supported have been expanded. Python debugging is now supported on Python 3.9+.

- Python navigation and information commands have been reworked and extended. To debug Python code,
  prefix a normal UDB command with `upy`, for example: `upy step`, `upy reverse-finish`, `upy
  backtrace`. For a full list of available Python commands type `help upy`.

- Breakpoints in Python code are now supported, see `upy break` and `upy info break`, `upy delete`
  for more information.

## Example Usage ##

> [!NOTE]
> The UBeacon library *must* be loaded and started at record time. You can use `upy attach`,
> `upy run` or `upy start` to do this.

1. Start UDB and load the replay time UBeacon extension using the command `source
   <path-to-ubeacon>/src/ubeacon/extension/startup.py`:

```
gfg@nog:~/git/ubeacon$ /home/gfg/git/core-ai/release-x64/udb /usr/bin/python3.12 -ex "source /home/gfg/git/ubeacon/src/ubeacon/extension/startup.py"
UDB 8.3.0-dev.g3a673bb4a019. Copyright 2025 Undo.
Licensed to: Testfarm User <noreply@undo.io>
Using GNU gdb (GDB) 13.2:
  Copyright (C) 2023 Free Software Foundation, Inc.
  License GPLv3+: GNU GPL version 3 or later <http://gnu.org/licenses/gpl.html>
  This is free software: you are free to change and redistribute it.
  There is NO WARRANTY, to the extent permitted by law.
  Type "show copying" and "show warranty" for details.
For help, type "help".
For quick-start help on UDB, type "help udb".

Reading symbols from /home/gfg/.pyenv/versions/3.10.13/bin/python...
not running> source /home/gfg/git/python-debugging/ubeacon/ubeacon/udb_extension/startup.py
added: /home/gfg/git/python-debugging/ubeacon/ubeacon
not running>
```

2. Start recording Python with the `upy start <args>` command:

```
not running> upy start /home/gfg/scratch/fizzbuzz.py 15

This GDB supports auto-downloading debuginfo from the following URLs:
  <https://debuginfod.ubuntu.com>
Debuginfod has been disabled.
To make this setting permanent, add 'set debuginfod enabled off' to .gdbinit.
NOTE: The inferior call was executed in "volatile mode", meaning that changes
      to program state were made to a temporary copy of the debugged program,
      which was discarded when the command completed.
Python has been initialized.
Failed checking if argv[0] is an import path entry
  #0 File "/usr/lib/python3.12/contextlib.py", line 1, in <module>
    """Utilities for with-statement contexts.  See PEP 343."""
recording 5,611,714>
```

5. Python debugging is now running, so you can use normal UDB commands to move around, or commands
   prefixed with `upy` to debug Python code. See `help upy` for more information on the available
   Python debugging commands. For example, to set a breakpoint and continue to it:

```
recording 8,072,892> upy break fizzbuzz
Python breakpoint 1 at fizzbuzz ()
recording 8,072,892> upy info breakpoints
1: fizzbuzz ()
recording 8,072,892> continue
Continuing.
Starting Fizzbuzz
Python breakpoint 1, fizzbuzz () at /home/gfg/scratch/ubeacon_tests/examples/fizzbuzz.py:4
recording 8,192,208> upy backtrace
Traceback (most recent call first):
  #0 File "/home/gfg/scratch/ubeacon_tests/examples/fizzbuzz.py", line 4, in fizzbuzz
    for i in range(1, max + 1):
  #1 File "/home/gfg/scratch/ubeacon_tests/examples/fizzbuzz.py", line 18, in main
    fizzbuzz(max)
  #2 File "/home/gfg/scratch/ubeacon_tests/examples/fizzbuzz.py", line 23, in <module>
    main()
recording 8,192,208> upy reverse-step
  #0 File "/home/gfg/scratch/ubeacon_tests/examples/fizzbuzz.py", line 18, in main
    fizzbuzz(max)
99% 8,191,822> upy step
Python breakpoint 1, fizzbuzz () at /home/gfg/scratch/ubeacon_tests/examples/fizzbuzz.py:4
Have switched to record mode.
  #0 File "/home/gfg/scratch/ubeacon_tests/examples/fizzbuzz.py", line 4, in fizzbuzz
    for i in range(1, max + 1):
recording 8,192,208>
```

Where possible, the commands for debugging python have been designed to match UDB's normal debugging
commands, but prefixed with `uexperimental python` (or `upy` for short). For example GDB's `where`
command - which is an alias for `backtrace` - has a Python equivalent `upy where` (or `upy
backtrace`).

A full list of commands can be seen with the `help upy` command.

## TUI Interface ##

In order to make debugging Python more user friendly, a TUI interface specifically tailored to
Python debugging is provided. This is accesible with the `layout python` command.
