import shutil
import sys
from pathlib import Path

import pytest

from acs.core import tools


def fake_pe(path: Path, machine: int = 0x8664) -> Path:
    head = bytearray(512)
    head[:2] = b"MZ"
    head[0x3C:0x40] = (0x80).to_bytes(4, "little")
    head[0x80:0x84] = b"PE\0\0"
    head[0x84:0x86] = machine.to_bytes(2, "little")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(head))
    return path


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for var in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)", "SCOOP", "USERPROFILE", "ChocolateyInstall"):
        monkeypatch.setenv(var, str(tmp_path / "none" / var))
    monkeypatch.setenv("PATH", str(tmp_path / "emptypath"))
    return tmp_path


def test_parse_version_and_encoders():
    assert tools.parse_ff_version("ffmpeg version 8.0.1-full_build Copyright", "ffmpeg") == "8.0.1-full_build"
    assert tools.parse_ff_version("ffprobe version 7.1 Copyright", "ffmpeg") is None
    enc = tools.parse_encoders(" V....D libx264     libx264 H.264\n V..... libsvtav1  SVT-AV1\n A....D aac   AAC\n")
    assert enc["libx264"] and enc["libsvtav1"] and not enc["libx265"]


def test_pe_header_check(tmp_path):
    assert tools.pe_machine(fake_pe(tmp_path / "a.dll")) == 0x8664
    assert tools.check_libmpv(fake_pe(tmp_path / "b.dll", 0x14C)).ok is False  # 32-bit
    (tmp_path / "c.dll").write_text("not a dll")
    st = tools.check_libmpv(tmp_path / "c.dll")
    assert not st.ok and st.error == "not a valid DLL"
    # the interpreter's own DLL is a real 64-bit PE
    real = next(Path(sys.base_prefix).glob("python3*.dll"), None)
    if real is not None:
        assert tools.pe_machine(real) == 0x8664


def test_finds_ffmpeg_in_winget_layout(clean_env, monkeypatch):
    local = clean_env / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    b = local / "Microsoft" / "WinGet" / "Packages" / "Gyan.FFmpeg_x" / "ffmpeg-8.0-full_build" / "bin"
    b.mkdir(parents=True)
    (b / "ffmpeg.exe").write_bytes(b"x")
    found = tools.find_ff_candidates("ffmpeg", clean_env / "app")
    assert found and found[0][0] == b / "ffmpeg.exe" and found[0][1] == "winget"


def test_finds_libmpv_one_level_deep_but_not_in_path_subdirs(clean_env, monkeypatch):
    pf = clean_env / "pf"
    monkeypatch.setenv("ProgramFiles", str(pf))
    dll = fake_pe(pf / "mpv" / "some-bundle" / "libmpv-2.dll")
    path_dir = clean_env / "onpath"
    fake_pe(path_dir / "nested" / "libmpv-2.dll")       # PATH dirs are not searched recursively
    monkeypatch.setenv("PATH", str(path_dir))
    found = [p for p, _ in tools.find_libmpv_candidates(clean_env / "app")]
    assert dll in found
    assert path_dir / "nested" / "libmpv-2.dll" not in found


def test_vendor_wins_and_current_setting_is_kept(clean_env, monkeypatch):
    app = clean_env / "app"
    vendor_dll = fake_pe(app / "vendor" / "libmpv-2.dll")
    other = fake_pe(clean_env / "elsewhere" / "libmpv-2.dll")
    st = tools.detect("libmpv", "", app)
    assert st.ok and Path(st.path) == vendor_dll and st.source == "vendor"
    st = tools.detect("libmpv", str(other), app)
    assert st.ok and Path(st.path) == other and st.source == "settings"
    st = tools.detect("libmpv", str(clean_env / "gone.dll"), clean_env / "noapp")
    assert not st.ok and st.error == "not found"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_is_validated():
    st = tools.check_ff(shutil.which("ffmpeg"), "ffmpeg")
    assert st.ok and st.version and "libx264" in st.features
    wrong = tools.check_ff(shutil.which("ffmpeg"), "ffprobe", with_features=False)
    assert not wrong.ok  # ffmpeg.exe is not accepted as ffprobe
