"""Generate the branded Windows icon used by the reproducible release build."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw


def main() -> None:
    target = Path(sys.argv[1])
    target.parent.mkdir(parents=True, exist_ok=True)
    base = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base)
    draw.rounded_rectangle((18, 18, 238, 238), radius=58, fill=(36, 99, 235, 255))
    draw.ellipse((66, 62, 190, 186), fill=(255, 255, 255, 255))
    draw.rectangle((82, 145, 174, 174), fill=(53, 167, 213, 255))
    draw.polygon(((82, 145), (117, 105), (143, 135), (159, 116), (174, 145)), fill=(53, 167, 213, 255))
    base.save(target, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    main()
