# setup.py
from setuptools import Extension, setup

module = Extension(
    'ubeacon',
    sources=[
        'src/ubeacon/lib/ubeacon.c',
        'src/ubeacon/lib/interact.c',
        'src/ubeacon/lib/trace.c',
        'src/ext/cJSON/cJSON.c'
    ],
    extra_compile_args=["-O0", "-Isrc/ext/cJSON"],
)

setup(
    name='ubeacon',
    version='1.0',
    description='Module for tracing function calls.',
    ext_modules=[module],
)

