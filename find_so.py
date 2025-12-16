# find_so.py
from pathlib import Path

from setuptools import Distribution, Extension
from setuptools.command.build_ext import build_ext

module = Extension('ubeacon', sources=[])  # sources don't matter for path calculation

dist = Distribution({'ext_modules': [module]})
cmd = build_ext(dist)
cmd.ensure_finalized()

so_path = Path(cmd.get_ext_fullpath('ubeacon')).resolve()
print(str(so_path))

