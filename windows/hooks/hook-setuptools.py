"""Do not collect setuptools into the frozen Windows agent."""

hiddenimports: list[str] = []
excludedimports = [
    "setuptools",
    "setuptools._vendor",
    "_distutils_hack",
    "pkg_resources",
]
