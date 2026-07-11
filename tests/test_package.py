from importlib.metadata import version

import novel_crawler


def test_version_is_exposed():
    assert novel_crawler.__version__ == "0.1.0"


def test_installed_distribution_version_matches_package():
    assert version("novel-crawler") == novel_crawler.__version__
