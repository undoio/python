# setup.py
import os

from setuptools import Extension, setup

try:
    include_dirs = os.environ["PYTHON_INCLUDE_DIRS"].split(os.pathsep)
except KeyError:
    raise RuntimeError(
        "PYTHON_INCLUDE_DIRS environment variable not set."
    )

module = Extension(
    "ubeacon",
    sources=[
        "src/ubeacon/lib/ubeacon.c",
        "src/ubeacon/lib/interact.c",
        "src/ubeacon/lib/trace.c",
        "src/ext/cJSON/cJSON.c",
    ],
    extra_compile_args=["-O0", "-Isrc/ext/cJSON", "-std=c99"],
    include_dirs=include_dirs,
)

setup(
    name="ubeacon",
    version="1.0",
    description="Module for tracing function calls.",
    ext_modules=[module],
)
