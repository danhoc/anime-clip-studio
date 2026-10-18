import re

from i18n_strings import all_strings

from acs.i18n import set_language, tr
from acs.i18n.ru import RU


def test_every_ui_string_has_russian_translation():
    missing = sorted(s for s in all_strings() if s not in RU)
    assert not missing, "missing RU translations:\n" + "\n".join(repr(m) for m in missing)


def test_no_duplicate_keys():
    """A repeated key silently replaces the earlier translation (e.g. one English word used in two places)."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "acs" / "i18n" / "ru.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    table = next(n.value for n in ast.walk(tree) if isinstance(n, ast.AnnAssign) and isinstance(n.value, ast.Dict))
    keys = [k.value for k in table.keys if isinstance(k, ast.Constant)]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, "duplicate RU keys:\n" + "\n".join(repr(d) for d in dupes)


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
