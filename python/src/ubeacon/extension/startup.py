import functools
import logging
import os
import sys
from pathlib import Path

import gdb  # pyright: ignore[reportMissingModuleSource]

from src.udbpy import locations  # pyright: ignore[reportMissingModuleSource]
from src.udbpy.gdb_extensions import command, udb_base  # pyright: ignore[reportMissingModuleSource]


@functools.cache
def udb() -> udb_base.Udb:
    gdb.execute("python sys._the_udb_for_ai = _udb")
    return sys._the_udb_for_ai  # type: ignore[attr-defined]


def setup() -> None:
    root_dir = Path(__file__).resolve().parent.parent.parent.parent
    assert (root_dir / "src/ubeacon/extension").exists(), f"Invalid root directory {str(root_dir)!r}"

    print(f"Setting up ubeacon extension from {str(root_dir)!r}")
    print("Dependencies set up successfully")
    add_path(root_dir / "src")

    logger = logging.getLogger("ubeacon")
    logger.debug("Started")

    try:
        command.import_commands_module(udb(), "ubeacon.extension.commands")
    except KeyboardInterrupt:
        logger.info("Interrupted")
        raise
    except SystemExit:
        logger.debug("Exiting")
        raise
    except BaseException:
        logger.exception("Unexpected error")
        raise


def add_path(p: Path) -> None:
    sys.path.insert(0, str(p))

    python_path = f"{os.environ.get('PYTHONPATH', '')}{os.pathsep}{str(p)}".rstrip(os.pathsep)
    os.environ["PYTHONPATH"] = python_path


setup()
