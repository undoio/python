# **Debugging Python Scripts with UDB**

This guide explains how to use UDB to debug Python scripts using the `python` addon.

---

## **Prerequisites**

You will need:

* A working UDB installation  
* The path to your Python interpreter (e.g. `/usr/bin/python`)  
* The Python script you want to debug

## **Step 1: Start UDB with the Python Interpreter**

Rather than running your Python script directly, launch UDB with your Python interpreter as the target program:

```
udb /path/to/python
	- or -
udb `which python`
```

Note: If using pyenv, use: `` udb `pyenv which python` ``

## **Step 2: Install the Python Addon**

At the UDB prompt, install the Python debugging addon:

```
not running> extend python
```

UDB will download and set up the addon automatically. 

⚠️ **Note:** The `python` addon is experimental and may change in incompatible ways in future releases.

## **Step 3: Start Your Script**

Use the `upy start` command to load and begin recording your Python script:

```
not running> upy start /path/to/your_script.py [arguments]
```

For example:

```
not running> upy start /home/user/scripts/fizzbuzz.py 20
```

UDB will initialise the Python environment and begin recording execution. You'll see a prompt like:

```
Python has been initialized.
recording 5,048,331>
```

## **Navigating the Recording**

Once your script is running, you have two sets of commands available:

### **Standard UDB commands**

Use these to navigate at the **C level** (the underlying interpreter execution). For example:

* `continue`, `next`, `step` — move forward  
* `reverse-continue`, `reverse-next` — move backward  
* `break` — set a C-level breakpoint  
* `layout dashboard` — enable the dashboard TUI layout  
* `Last <expression>` — travel backwards to the last time \<expression\> changed  
  


### 

### **Python-level commands (prefixed with `upy`)**

Use these to navigate at the **Python level** (your script's source code). For example:

| Command | Description |
| ----- | ----- |
| `upy continue` | Run forward until the next breakpoint or end of program |
| `upy reverse-continue` | Run backward until the previous breakpoint |
| `upy next` | Step forward to the next Python line |
| `upy reverse-next` | Step backward to the previous Python line |
| `upy break <function>` | Set a Python breakpoint at a function |
| `upy break <file.py:line>` | Set a Python breakpoint at a line in `file.py` |
| `upy start <script> [args]` | Start a Python script |

**Example session** — running a script, setting a breakpoint, and stepping back through it:

```
recording> upy continue
# ... script output ...

end> upy break fizzbuzz
Python breakpoint 1 at fizzbuzz ()

end> upy reverse-continue
Python breakpoint 1, fizzbuzz () at /home/user/scripts/fizzbuzz.py:4
  #0 File "fizzbuzz.py", line 4, in fizzbuzz
    for i in range(1, max + 1):

> upy next
  #0 File "fizzbuzz.py", line 5, in fizzbuzz
    if i % 3 == 0 and i % 5 == 0:
```

## 

## **TUI Layout**

For a visual, source-level debugging interface, type:

```
layout python
```

This opens a terminal UI showing your Python source code as you step through it, similar to `layout dashboard` for C-level debugging.

## **Getting Help**

To see all available Python debugging commands, type:

```
help upy
```

## **Quick Reference**

```
udb `which python`         # Launch UDB with Python
udb /path/to/python        # Launch UDB with Python
extend python              # Load the Python addon 
upy start script.py [args] # Start and record your script
upy continue               # Run forward
upy reverse-continue       # Run backward
upy next                   # Step to next Python line
upy break <function>       # Set a Python breakpoint
upy break <file.py:line>   # Set a Python breakpoint
upy watch <expression>     # Set a Python watchpoint
upy info locals            # Display Python local variables
upy backtrace              # Display Python backtrace
layout python              # Open Python TUI view
layout dashboard           # Open C/C++ TUI view
help upy                   # Show all upy commands
```

