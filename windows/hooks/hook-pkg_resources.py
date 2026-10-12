"""Keep pkg_resources out of the frozen Windows agent.

PyInstaller otherwise injects pyi_rth_pkgres.py, which crashed 2.1.21 at boot
even though the agent never imports pkg_resources.
"""

hiddenimports: list[str] = []
excludedimports = [
    "pkg_resources",
    "pkg_resources.extern",
    "setuptools",
    "setuptools._vendor",
    "jaraco",
    "jaraco.text",
    "jaraco.functools",
    "more_itertools",
]
