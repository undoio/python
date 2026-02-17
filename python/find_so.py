# find_so.py
import sys
from pathlib import Path

from setuptools import Distribution, Extension
from setuptools.command.build_ext import build_ext

cache_dir = sys.argv[-1]

module = Extension('ubeacon', sources=[])  # sources don't matter for path calculation

dist = Distribution({'ext_modules': [module]})
dist.command_options["build"] = {
    "build_base": ("setup.py", cache_dir),
}
cmd = build_ext(dist)
cmd.ensure_finalized()

so_path = Path(cmd.get_ext_fullpath('ubeacon')).resolve()
print(str(so_path))

