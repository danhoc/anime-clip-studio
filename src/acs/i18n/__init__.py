"""Tiny translation layer: English source strings, Russian dictionary in ru.py.

Language is chosen at startup (Settings → Language) and applied after a restart.
"""

from __future__ import annotations

_lang = "en"
_table: dict[str, str] = {}

LANGUAGES = {"en": "English", "ru": "Русский"}


def set_language(lang: str) -> None:
    global _lang, _table
    _lang = lang if lang in LANGUAGES else "en"
    if _lang == "ru":
        from acs.i18n.ru import RU
        _table = RU
    else:
        _table = {}


def language() -> str:
    return _lang


def tr(text: str) -> str:
    return _table.get(text, text)


# Qt's own standard-button strings (QDialogButtonBox / QMessageBox) that qtbase_ru lacks or
# translates with the wrong meaning for our dialogs (e.g. Discard -> "Отклонить").
_QT_OVERRIDES = {
    "ru": {
        "Save": "Сохранить", "&Save": "&Сохранить", "Save All": "Сохранить все",
        "Discard": "Не сохранять", "Don't Save": "Не сохранять",
        "OK": "OK", "&OK": "&OK", "Cancel": "Отмена", "Close": "Закрыть", "Apply": "Применить",
        "&Yes": "&Да", "&No": "&Нет", "Yes to &All": "Да для &всех", "N&o to All": "Н&ет для всех",
        "Open": "Открыть", "Reset": "Сбросить", "Restore Defaults": "По умолчанию", "Help": "Справка",
        "Abort": "Прервать", "Retry": "Повторить", "Ignore": "Пропустить",
    },
}


def install_qt_translations(app, lang: str | None = None) -> None:
    """qtbase translations + our overrides for standard buttons. Call before creating windows."""
    from PySide6.QtCore import QLibraryInfo, QTranslator

    lang = lang or _lang
    if lang == "en":
        return
    base = QTranslator(app)
    if base.load(f"qtbase_{lang}", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(base)
    overrides = _QT_OVERRIDES.get(lang)
    if overrides:
        class _Buttons(QTranslator):
            def translate(self, context, source, disambiguation=None, n=-1):  # noqa: ARG002
                if context in ("QPlatformTheme", "QDialogButtonBox", "QMessageBox"):
                    return overrides.get(source, "")
                return ""

        ov = _Buttons(app)
        app.installTranslator(ov)  # installed last => consulted first
        app._acs_translators = (base, ov)  # keep Python references alive
