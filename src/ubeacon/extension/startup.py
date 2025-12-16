import sys
import os

from pathlib import Path
path = Path(__file__).resolve().parent.parent
sys.path.append(str(path))
print(f"added: {path}")

#from udb_extension import commands

from undo.debugger_extensions import udb
from src.udbpy.gdb_extensions import command

udb = udb._wrapped_udb  # pylint: disable=protected-access
command.import_commands_module(udb, "extension.commands")
