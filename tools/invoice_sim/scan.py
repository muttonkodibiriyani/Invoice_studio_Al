"""Turn a rendered PDF into a scan: low DPI, skew, noise, blur, fade. Same seed, same pixels."""
from __future__ import annotations

import io
import random
from pathlib import Path

import numpy as np
import pypdfium2
from PIL import Image, ImageFilter

PROFILES = {
    # name: dpi, maximum skew in degrees, noise sigma, blur radius, contrast kept, JPEG quality
    "office": (200, 0.6, 4, 0.0, 1.0, 88),
    "fax": (120, 1.2, 10, 0.4, 0.9, 70),
    "phone": (150, 2.5, 14, 0.7, 0.8, 55),
    "bad-copy": (100, 3.0, 20, 0.8, 0.65, 45),
}


def scan_page(page, rng: random.Random, profile: str) -> Image.Image:
    dpi, skew, sigma, blur, contrast, _ = PROFILES[profile]
    image = page.render(scale=dpi / 72).to_pil().convert("L")
    image = image.rotate(rng.uniform(-skew, skew), expand=True, fillcolor=255, resample=Image.BICUBIC)
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    pixels = np.asarray(image, dtype=np.float32)
    pixels = 255 - (255 - pixels) * contrast
    noise = np.random.default_rng(rng.randrange(1 << 31)).normal(0, sigma, pixels.shape)
    return Image.fromarray(np.clip(pixels + noise, 0, 255).astype(np.uint8))


def scan_pdf(source: Path, target: Path, seed: str, profile: str | None = None) -> tuple[Path, str]:
    """Write an image-only PDF of source and return it with the profile used. Images are JPEG compressed as scanners do."""
    rng = random.Random(seed)
    profile = profile or rng.choice(list(PROFILES))
    pdf = pypdfium2.PdfDocument(str(source))
    pages = []
    try:
        for page in pdf:
            scanned = scan_page(page, rng, profile)
            buffer = io.BytesIO()
            scanned.save(buffer, "JPEG", quality=PROFILES[profile][5])
            buffer.seek(0)
            pages.append(Image.open(buffer).convert("L"))
    finally:
        pdf.close()
    pages[0].save(target, "PDF", save_all=True, append_images=pages[1:], resolution=PROFILES[profile][0])
    return Path(target), profile
