"""
Download Python development headers for a given version into a temporary directory.

Works without root on Ubuntu, RHEL, Fedora, and OpenSUSE by downloading (not installing)
the appropriate package and extracting it locally.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import distro


def _run_command(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    """
    Run a command with subprocess.run and check for errors.

    command: The command to run, as a list of arguments.
    kwargs: Additional keyword arguments to pass to subprocess.run.

    Returns the CompletedProcess object returned by subprocess.run.
    """
    return subprocess.run(command, check=True, capture_output=True, text=True, **kwargs)


def _get_download_package_uri(version: str) -> str:
    """
    Return the URL of a distribution package file.
    """

    match distro.id():
        case dist if dist in {"ubuntu", "debian"}:

            return (
                _run_command(
                    ["apt-get", "--print-uris", "download", f"libpython{version}-dev"]
                )
                .stdout.splitlines()[0]
                .split()[0]
                .strip("'")
            )

        case dist if dist in {"fedora", "rhel", "rocky", "centos", "amzn"}:

            def dnf(package_name: str) -> str:
                # TODO support on ARM
                return _run_command(
                    [
                        "dnf",
                        "repoquery",
                        "--location",
                        package_name,
                        "--archlist",
                        "x86_64",
                    ]
                ).stdout.strip()

            rpms = dnf(f"python{version}-devel")
            if not rpms:
                # The package may be named python3-devel if it's the default
                # Python version for the current distro version.
                rpms = dnf("python3-devel")
                if version not in rpms:
                    raise ValueError(
                        f"Could not find a suitable python-devel package for version {version} in dnf output: {rpms}"
                    )
            return rpms.splitlines()[
                0
            ]  # Take the first result if there are multiple matches.

        case _:
            raise ValueError(f"Unsupported distribution: {distro.id()}")


def _extract_deb(package_path: Path, extract_dir: Path) -> None:
    """Extract a .deb package into the given directory."""
    _run_command(["dpkg-deb", "-x", str(package_path), str(extract_dir)])


def _extract_rpm(package_path: Path, extract_dir: Path) -> None:
    """Extract a .rpm package into the given directory."""
    with subprocess.Popen(
        ["rpm2cpio", str(package_path)], stdout=subprocess.PIPE
    ) as rpm2cpio:
        _run_command(["cpio", "-idm"], stdin=rpm2cpio.stdout, cwd=extract_dir)
        rpm2cpio.wait()
        if rpm2cpio.returncode != 0:
            raise subprocess.CalledProcessError(
                rpm2cpio.returncode, ["rpm2cpio", str(package_path)]
            )


def python_dev_headers(
    version: str, storage_dir: Path, uri_override: str | None = None
) -> Path:
    """
    Download the appropriate python-dev/python-devel package for the current Linux
    distribution if necessary, extract it, and return the path to the
    extracted headers.

    Args:
        version: The Python version string, e.g. "3.11".
        storage_dir: The directory to use for downloading and extracting packages.
        uri_override: If provided, this URI will be used instead of determining
        the package URL based on the distribution. This is intended for testing.

    Returns:
        The root of the unpacked headers.

    Raises:
        ValueError: If the current distribution is not supported.
        subprocess.CalledProcessError: If downloading or extracting the package fails.
    """
    download_dir = storage_dir / "packages"
    download_dir.mkdir(exist_ok=True)

    uri = uri_override or _get_download_package_uri(version)

    # Fetch the package file to the download directory.
    package_path = download_dir / uri.split("/")[-1]
    if not package_path.exists():
        _run_command(["curl", "--fail","-L", "-o", str(package_path), uri])

    extract_dir = storage_dir / (package_path.stem + "_extracted")
    if not extract_dir.exists():
        if package_path.suffix == ".deb":
            extract = _extract_deb
        elif package_path.suffix == ".rpm":
            extract = _extract_rpm
        else:
            raise ValueError(
                f"""Unknown package format: {package_path.suffix}. Expected .deb or .rpm."""
            )

        extract_dir.mkdir(exist_ok=True)
        extract(package_path, extract_dir)

    return extract_dir
