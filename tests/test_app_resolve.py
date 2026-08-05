from axstream.driver import match_app_name

INSTALLED = ["Maps", "Google Chrome", "Visual Studio Code", "System Settings",
             "Mail", "Notes", "Firefox", "Blender", "Music"]


def test_exact_case_insensitive():
    assert match_app_name("mail", INSTALLED) == "Mail"


def test_spoken_superset_resolves_apple_prefix():
    assert match_app_name("apple maps", INSTALLED) == "Maps"


def test_spoken_subset_resolves_partial_name():
    assert match_app_name("chrome", INSTALLED) == "Google Chrome"
    assert match_app_name("settings", INSTALLED) == "System Settings"


def test_typo_fuzzy():
    assert match_app_name("blendr", INSTALLED) == "Blender"


def test_nonsense_returns_none():
    assert match_app_name("flurbotron", INSTALLED) is None
    assert match_app_name("", INSTALLED) is None
