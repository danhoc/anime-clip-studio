import re

from i18n_strings import all_strings

from acs.i18n import set_language, tr
from acs.i18n.ru import RU


def test_every_ui_string_has_russian_translation():
    missing = sorted(s for s in all_strings() if s not in RU)
    assert not missing, "missing RU translations:\n" + "\n".join(repr(m) for m in missing)


def test_placeholders_preserved():
    for en, ru in RU.items():
        assert set(re.findall(r"\{\w*\}", en)) == set(re.findall(r"\{\w*\}", ru)), en


def test_switching_language():
    set_language("ru")
    try:
        assert tr("Export") == "Экспорт"
        assert tr("not translated at all") == "not translated at all"
    finally:
        set_language("en")
    assert tr("Export") == "Export"
