libmpv-2.dll — third-party component bundled with Anime Clip Studio
====================================================================

File        vendor\libmpv-2.dll (64-bit), the video player library used by Anime Clip Studio
SHA-256     39347ea11617550a2c9e8c9d38d4bfa73704654345464831d9d6c8ec29e771aa
Version     mpv v0.40.0-462-ge6885cb92 (mpv commit e6885cb926ca05a23f54f1b44b250f6f981b4e46,
            2025-11-18), FFmpeg N-121806-gb39989604
Build       Windows build by shinchiro (mpv-winbuild-cmake), configured with the default GPL
            options (no -Dgpl=false), distributed at
            https://sourceforge.net/projects/mpv-player-windows/files/libmpv/
            The file is redistributed unmodified.

License
-------
mpv is free software licensed under the GNU General Public License version 2 or later; see
"Copyright" (mpv's own licensing statement) and "LICENSE.GPL" in this folder, and "LICENSE.LGPL"
for the files mpv lists as LGPL. This build also links libraries under other free licenses (for
example FFmpeg with GPL components, libass, harfbuzz, freetype, libplacebo, mbedtls / Apache-2.0),
so the combined library is distributed under the terms of the GNU General Public License
version 3 — the same license as Anime Clip Studio itself (LICENSE in the program folder).

There is NO WARRANTY, to the extent permitted by law.

Source code
-----------
mpv          https://github.com/mpv-player/mpv/tree/e6885cb926ca05a23f54f1b44b250f6f981b4e46
FFmpeg       https://github.com/FFmpeg/FFmpeg/commit/b39989604
Build system https://github.com/shinchiro/mpv-winbuild-cmake (the list of all bundled libraries
             and their exact source locations is in its packages/ folder)

Whoever gave you this copy of Anime Clip Studio will also provide the corresponding source code
of this library on request, as required by the GPL.

-----------------------------------------------------------------------------------------------

libmpv-2.dll — сторонний компонент в составе Anime Clip Studio
==============================================================

Файл        vendor\libmpv-2.dll (64-бит) — библиотека видеоплеера, которую использует программа
SHA-256     39347ea11617550a2c9e8c9d38d4bfa73704654345464831d9d6c8ec29e771aa
Версия      mpv v0.40.0-462-ge6885cb92 (коммит mpv e6885cb926ca05a23f54f1b44b250f6f981b4e46
            от 18.11.2025), FFmpeg N-121806-gb39989604
Сборка      сборка для Windows от shinchiro (mpv-winbuild-cmake) с настройками GPL по умолчанию
            (без -Dgpl=false), публикуется на
            https://sourceforge.net/projects/mpv-player-windows/files/libmpv/
            Файл распространяется без изменений.

Лицензия
--------
mpv — свободная программа под лицензией GNU GPL версии 2 или более поздней; см. «Copyright»
(заявление о лицензии самого mpv) и «LICENSE.GPL» в этой папке, а также «LICENSE.LGPL» для файлов,
которые mpv указывает как LGPL. В эту сборку также входят библиотеки под другими свободными
лицензиями (например, FFmpeg с компонентами GPL, libass, harfbuzz, freetype, libplacebo,
mbedtls / Apache-2.0), поэтому библиотека целиком распространяется на условиях GNU GPL версии 3 —
той же лицензии, что и сама Anime Clip Studio (файл LICENSE в папке программы).

Гарантии отсутствуют в пределах, допускаемых законом.

Исходный код
------------
Ссылки на исходный код mpv, FFmpeg и системы сборки — в английской части выше. Тот, кто передал
вам эту копию Anime Clip Studio, по запросу также предоставит соответствующий исходный код этой
библиотеки, как того требует GPL.
