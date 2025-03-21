# Undo Beacon Library #

## Introduction ##

The Undo Beacon library - "UBeacon" - is a Python library that attempts to expose Python interpreter
state to the Undo Engine in a reasonably performant and stable way. This allows basic time travel
debugging of Python code using special commands - implemented in the `udb` tool.

> [!NOTE]
> The UBeacon library *must* be loaded and started at record time.

## Usage ##

1. The UBeacon library is written in C, and must be built before it can be used. Run the `setup.py`
   script to build it. Bear in mind that the library will be built to target the version of Python
   used to execute the `setup.py` script.

```
gfg@nog:~/git/python-debugging/ubeacon$ python setup.py build
running build
running build_ext
building 'ubeacon' extension
creating build
creating build/temp.linux-x86_64-cpython-310
creating build/temp.linux-x86_64-cpython-310/ubeacon
gcc -Wno-unused-result -Wsign-compare -DNDEBUG -g -fwrapv -O3 -Wall -fPIC -I/home/gfg/.pyenv/versions/3.10.13/include/python3.10 -c ubeacon/interact.c -o build/temp.linux-x86_64-cpython-310/ubeacon/interact.o -O0
gcc -Wno-unused-result -Wsign-compare -DNDEBUG -g -fwrapv -O3 -Wall -fPIC -I/home/gfg/.pyenv/versions/3.10.13/include/python3.10 -c ubeacon/trace.c -o build/temp.linux-x86_64-cpython-310/ubeacon/trace.o -O0
gcc -Wno-unused-result -Wsign-compare -DNDEBUG -g -fwrapv -O3 -Wall -fPIC -I/home/gfg/.pyenv/versions/3.10.13/include/python3.10 -c ubeacon/ubeacon.c -o build/temp.linux-x86_64-cpython-310/ubeacon/ubeacon.o -O0
creating build/lib.linux-x86_64-cpython-310
gcc -shared -L/home/gfg/.pyenv/versions/3.10.13/lib -Wl,-rpath,/home/gfg/.pyenv/versions/3.10.13/lib -L/home/gfg/.pyenv/versions/3.10.13/lib -Wl,-rpath,/home/gfg/.pyenv/versions/3.10.13/lib build/temp.linux-x86_64-cpython-310/ubeacon/interact.o build/temp.linux-x86_64-cpython-310/ubeacon/trace.o build/temp.linux-x86_64-cpython-310/ubeacon/ubeacon.o -L/home/gfg/.pyenv/versions/3.10.13/lib -o build/lib.linux-x86_64-cpython-310/ubeacon.cpython-310-x86_64-linux-gnu.so
```

2. Set the `UBEACON` environment variable is set to the path of the library shared object you just
   built:

```
gfg@nog:~/git/python-debugging/ubeacon$ export UBEACON=$(realpath $(find -name 'ubeacon.*.so'))
gfg@nog:~/git/python-debugging/ubeacon$ echo $UBEACON
/home/gfg/git/python-debugging/ubeacon/build/lib.linux-x86_64-cpython-310/ubeacon.cpython-310-x86_64-linux-gnu.so
```

3. Start UDB and load the replay time UBeacon extension using the command `source
   <path-to-ubeacon>/ubeacon/udb_extension/startup.py`:

```
gfg@nog:~/git/python-debugging/ubeacon$ /home/gfg/git/core/release-x64/udb /home/gfg/.pyenv/versions/3.10.13/bin/python
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

4. Start recording Python with the `upy start <args>` command:

```
not running> upy start ~/scratch/ubeacon_tests/examples/fizzbuzz.py
Python has been initialized.
  #0 File "/home/gfg/scratch/ubeacon_tests/examples/fizzbuzz.py", line 1, in <module>
    import sys
recording 3,820,812> 
```

> [!NOTE]
> There is currently no equivalent to UDB's `run` and `attach` commands. To mimic run use `upy
> start` followed by `continue`. To mimic attach use `attach` followed by `upy record`.

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
