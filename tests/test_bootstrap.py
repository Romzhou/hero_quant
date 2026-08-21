def test_package_importable():
    import hero_quant

    assert hero_quant.__version__ == "0.2.0"
