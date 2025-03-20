# setup.py
from setuptools import setup, Extension

module = Extension(
    'ubeacon',
    sources=['ubeacon/ubeacon.c', 'ubeacon/interact.c', 'ubeacon/trace.c', 'ext/cJSON/cJSON.c'],
    extra_compile_args=["-O0", "-Iext/cJSON"],
)

setup(
    name='ubeacon',
    version='1.0',
    description='Module for tracing function calls.',
    ext_modules=[module],
)

