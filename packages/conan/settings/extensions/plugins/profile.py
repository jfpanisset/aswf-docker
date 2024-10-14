# This file was generated by Conan. Remove this comment if you edit this file or Conan
# will destroy your changes.


def profile_plugin(profile):
    settings = profile.settings
    if settings.get("compiler") in ("msvc", "clang") and settings.get(
        "compiler.runtime"
    ):
        if settings.get("compiler.runtime_type") is None:
            runtime = "Debug" if settings.get("build_type") == "Debug" else "Release"
            try:
                settings["compiler.runtime_type"] = runtime
            except ConanException:
                pass
    _check_correct_cppstd(settings)
    _check_correct_cstd(settings)


def _check_correct_cppstd(settings):
    from conan.tools.scm import Version

    def _error(compiler, cppstd, min_version, version):
        from conan.errors import ConanException

        raise ConanException(
            f"The provided compiler.cppstd={cppstd} requires at least {compiler}"
            f">={min_version} but version {version} provided"
        )

    cppstd = settings.get("compiler.cppstd")
    version = settings.get("compiler.version")

    if cppstd and version:
        cppstd = cppstd.replace("gnu", "")
        version = Version(version)
        mver = None
        compiler = settings.get("compiler")
        if compiler == "gcc":
            mver = {"20": "8", "17": "5", "14": "4.8", "11": "4.3"}.get(cppstd)
        elif compiler == "clang":
            mver = {"20": "6", "17": "3.5", "14": "3.4", "11": "2.1"}.get(cppstd)
        elif compiler == "apple-clang":
            mver = {"20": "10", "17": "6.1", "14": "5.1", "11": "4.5"}.get(cppstd)
        elif compiler == "msvc":
            mver = {"23": "193", "20": "192", "17": "191", "14": "190"}.get(cppstd)
        if mver and version < mver:
            _error(compiler, cppstd, mver, version)


def _check_correct_cstd(settings):
    from conan.tools.scm import Version

    def _error(compiler, cstd, min_version, version):
        from conan.errors import ConanException

        raise ConanException(
            f"The provided compiler.cstd={cstd} requires at least {compiler}"
            f">={min_version} but version {version} provided"
        )

    cstd = settings.get("compiler.cstd")
    version = settings.get("compiler.version")

    if cstd and version:
        cstd = cstd.replace("gnu", "")
        version = Version(version)
        mver = None
        compiler = settings.get("compiler")
        if compiler == "gcc":
            # TODO: right versions
            mver = {}.get(cstd)
        elif compiler == "clang":
            # TODO: right versions
            mver = {}.get(cstd)
        elif compiler == "apple-clang":
            # TODO: Right versions
            mver = {}.get(cstd)
        elif compiler == "msvc":
            mver = {"17": "192", "11": "192"}.get(cstd)
        if mver and version < mver:
            _error(compiler, cppstd, mver, version)
