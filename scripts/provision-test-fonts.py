"""Map Ubuntu's free system fonts into an isolated CPU test runtime.

These substitutes exercise layout behavior; they are not production fonts or
evidence of identical production typography. Pass a fresh temporary directory.
"""
from pathlib import Path
import sys


def main():
    font_root = Path(sys.argv[1]) / "fonts"
    font_root.mkdir(parents=True)
    system = Path("/usr/share/fonts/truetype")
    fonts = {f"SourceHanSerifCN-{weight}.ttf": system / "droid/DroidSansFallbackFull.ttf"
             for weight in ("Regular", "Medium", "Bold")}
    fonts.update({
        "SourceSerif4-Regular.ttf": system / "dejavu/DejaVuSerif.ttf",
        "SourceSerif4-Bold.ttf": system / "dejavu/DejaVuSerif-Bold.ttf",
        "SourceSerif4-It.ttf": system / "dejavu/DejaVuSerif-Italic.ttf",
        "NotoSansMono-Regular.ttf": system / "dejavu/DejaVuSansMono.ttf",
    })
    fonts.update({name: system / "dejavu" / name for name in (
        "DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf", "DejaVuSerif-Italic.ttf")})
    for name, source in fonts.items():
        (font_root / name).symlink_to(source)


if __name__ == "__main__":
    main()
