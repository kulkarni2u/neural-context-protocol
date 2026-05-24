from ncp import __version__


def test_version_exposed() -> None:
    assert __version__
