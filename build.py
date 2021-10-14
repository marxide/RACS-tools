# Use numpy.distutils to compile Fortran extensions. When configured, Poetry will load
# this script to create a temporary setup.py file for installation. Importing
# numpy.distutils.core.setup here overrides the default setuptools.setup function used
# by Poetry later.
from numpy.distutils.core import Extension, setup  # noqa: F401

source_files = [
    "racs_tools/gaussft.f",
]

extensions = [
    Extension(
        name="racs_tools.gaussft",
        sources=[
            "racs_tools/gaussft.f",
        ],
        # extra_f90_compile_args=["-ffixed-form"],
        extra_compile_args=["-ffixed-form"],
    ),
]


def build(setup_kwargs):
    """Build extension modules."""
    setup_kwargs.update(dict(ext_modules=extensions))
