"""Generate a sample image for multimodal testing."""

from __future__ import annotations

from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    raise SystemExit("Pillow required: pip install pillow")

MEDIA_DIR = Path(__file__).resolve().parents[1] / "data" / "media"
OUTPUT = MEDIA_DIR / "sample_reel_frame.png"


def main() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (640, 360), color=(20, 30, 50))
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 40, 600, 320], outline=(100, 180, 255), width=3)
    draw.text((60, 80), "POV: Startup founder", fill=(255, 255, 255))
    draw.text((60, 120), "explaining SaaS architecture", fill=(200, 220, 255))
    draw.text((60, 160), "to investors in 60 seconds", fill=(200, 220, 255))
    draw.text((60, 260), "Text overlay: MVP → Scale", fill=(100, 255, 180))
    img.save(OUTPUT)
    print(f"Created {OUTPUT}")


if __name__ == "__main__":
    main()
