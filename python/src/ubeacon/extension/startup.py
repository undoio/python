import functools
import hashlib
import logging
import os
import re
import shlex
import subprocess
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

    setup_dependencies(root_dir)
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


def setup_dependencies(root_dir: Path) -> None:
    uv_lock_path = root_dir / "uv.lock"
    assert uv_lock_path.exists(), f"Missing {str(uv_lock_path)!r}"
    uv_lock_content = uv_lock_path.read_text()

    python_package_dir = locations.get_undo_data_path("ubeacon_packages")
    add_path(python_package_dir)

    checksum_path = python_package_dir / "checksum.txt"
    checksum_current = hashlib.sha224(uv_lock_content.encode()).hexdigest()
    try:
        checksum_last = checksum_path.read_text()
    except FileNotFoundError:
        pass
    else:
        if checksum_last == checksum_current:
            return

    run_install_command([sys.executable, "-m", "ensurepip"])
    # First install tomli to parse the lock file, then we install all the correct versions of all
    # the other dependencies.
    run_pip(python_package_dir, ["tomli"])
    run_pip(python_package_dir, parse_lock_dependencies(uv_lock_content))

    checksum_path.write_text(checksum_current)


def parse_lock_dependencies(uv_lock_content: str) -> list[str]:
    """
    Parse uv.lock and return runtime dependencies with exact versions.
    """
    import tomli

    lock_data = tomli.loads(uv_lock_content)
    packages_by_name = {p["name"]: p for p in lock_data.get("package", [])}

    # Collect runtime dependencies recursively.
    needed = set()
    to_process = ["ai-tools"]
    while to_process:
        pkg_name = to_process.pop()
        if pkg_name in needed:
            continue
        needed.add(pkg_name)
        pkg = packages_by_name.get(pkg_name, {})
        for dep in pkg.get("dependencies", []):
            # Skip dependencies that are not for Linux.
            if (
                (marker := dep.get("marker"))
                and (m := re.search(r"sys_platform\s*==\s*'([^']+)'", marker))
                and m[1] != "linux"
            ):
                continue
            to_process.append(dep["name"])

    needed.discard("ai-tools")
    return [f"{name}=={packages_by_name[name]['version']}" for name in sorted(needed)]


def add_path(p: Path) -> None:
    sys.path.insert(0, str(p))

    python_path = f"{os.environ.get('PYTHONPATH', '')}{os.pathsep}{str(p)}".rstrip(os.pathsep)
    os.environ["PYTHONPATH"] = python_path


def run_pip(python_package_dir: Path, dependencies: list[str]) -> None:
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "-q",
        "install",
        "--upgrade",
        "--target",
        ".",
    ] + dependencies

    run_install_command(cmd, cwd=python_package_dir)


def run_install_command(cmd: list[str], *, cwd: Path | None = None) -> None:
    env = os.environ.copy()
    # We're installing locally, for udb's package python: we don't need to worry
    # about virtual env restrictions.
    env["PIP_REQUIRE_VIRTUALENV"] = "false"
    try:
        subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        if "You must give at least one requirement to install" not in exc.output:
            raise RuntimeError(
                f"Failed to install dependencies with command {shlex.join(cmd)}:\n{exc.output}"
            ) from exc


setup()
