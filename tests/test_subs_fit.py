from acs.core.model import CropRect
from acs.core.subs import FIT_STYLE_SUFFIX, fit_ass, shorts_sub_scale

ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,60,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3,1,2,40,40,50,1
Style: Sign,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,5,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Hello, world{\\i1}!{\\fs80} big
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\an8}Top line
Dialogue: 0,0:00:01.00,0:00:03.00,Sign,,0,0,0,,{\\pos(300,200)}A sign
"""


def events(text):
    return [line for line in text.splitlines() if line.startswith("Dialogue:")]


def fields(line, n=10):
    return line.partition(":")[2].split(",", n - 1)


def test_dialogue_goes_into_the_shorts_frame_and_is_scaled():
    frame = CropRect(656, 108, 486, 864)   # x, y, w, h of a frame inside a 1920x1080 picture
    out = fit_ass(ASS, 1920, 1080, frame, 0.5)
    bottom, top, sign = events(out)
    b = fields(bottom)
    assert b[3] == "Default" + FIT_STYLE_SUFFIX
    # left: frame offset + scaled style margin; right: the rest of the width; bottom: below the frame
    assert (b[5], b[6], b[7]) == ("676", "798", "133")
    assert "{\\fs40}" in b[9] and "Hello, world" in b[9]
    t = fields(top)
    assert t[7] == str(108 + 25)            # top-aligned line: under the top edge of the frame
    assert fields(sign)[3] == "Sign" and "{\\pos(300,200)}" in sign   # signs stay as they were
    style = next(line for line in out.splitlines() if line.startswith("Style: Default" + FIT_STYLE_SUFFIX))
    vals = style.split(",")
    assert vals[2] == "30" and vals[16] == "1.5"   # font size and outline halved
    assert "Style: Default,Arial,60," in out       # the original style is still there


def test_size_only_without_crop_and_untouched_when_unknown():
    out = fit_ass(ASS, 1920, 1080, None, 1.5)
    b = fields(events(out)[0])
    assert (b[5], b[6], b[7]) == ("60", "60", "75")
    assert fit_ass("not an ass file", 1920, 1080, None, 2.0) == "not an ass file"


def test_play_res_differs_from_the_video():
    small = ASS.replace("PlayResX: 1920", "PlayResX: 640").replace("PlayResY: 1080", "PlayResY: 360")
    b = fields(events(fit_ass(small, 1920, 1080, CropRect(656, 0, 608, 1080), 1.0))[0])
    assert (b[5], b[6]) == (str(round(656 / 3 + 40)), str(round(656 / 3 + 40)))


def test_shorts_scale_follows_the_frame_height():
    assert shorts_sub_scale(1080, 1080) == 0.75
    assert shorts_sub_scale(540, 1080) == 0.375
