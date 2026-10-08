def test_package_imports():
    import livetest
    assert isinstance(livetest.__version__, str)
    assert livetest.__version__
