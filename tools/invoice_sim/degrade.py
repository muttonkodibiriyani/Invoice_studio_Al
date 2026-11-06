"""Carbon copies, photos and poor scans, overlaps: raster renders of a generated invoice, deterministic per id.

Handwriting uses a handwriting font with jittered glyphs (INVOICE_SIM_HANDWRITING_FONT), or a jittered oblique font
without one. Real handwriting is harder: read these counts as an upper bound on what a real pen would give.
"""
from __future__ import annotations

import math
import os
import random
import tempfile
from decimal import Decimal
from pathlib import Path

import numpy as np
import pdfplumber
import pypdfium2
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .generator import Invoice, LABELS, fmt_number, render_pdf

DEJAVU_OBLIQUE = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"
DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

CARBON = ["carbon-blue", "carbon-purple", "carbon-uneven-pressure", "carbon-smudged", "carbon-blurred", "carbon-tinted-paper",
          "carbon-show-through", "carbon-handwritten-form", "carbon-handwritten-numbers", "ink-handwritten-form"]
PHOTO = ["photo-perspective-shadow", "photo-fold-lines", "rotated-90", "rotated-180", "rotated-270", "cropped-margins",
         "cropped-into-text", "very-low-dpi", "fax-black-white", "thermal-faded"]
OVERLAP = ["overlap-stamp-totals", "overlap-signature-totals", "overlap-watermark", "overlap-crossed-out-correction"]
DEGRADED = CARBON + PHOTO + OVERLAP
# What each family is saved as: photos come as JPEG, the rest as an image-only PDF.
JPEG = {"photo-perspective-shadow", "photo-fold-lines"}


def _rng(inv: Invoice) -> random.Random:
    return random.Random(f"degrade:{inv.id}")


def _noise(rng, shape, cells=8):
    """Low-frequency noise in 0..1: uneven pressure, shadows, fading."""
    small = np.random.default_rng(rng.randrange(1 << 31)).random((cells, cells))
    image = Image.fromarray((small * 255).astype(np.uint8)).resize((shape[1], shape[0]), Image.BICUBIC)
    return np.asarray(image, dtype=np.float32) / 255


def _speckle(rng, pixels, sigma):
    return pixels + np.random.default_rng(rng.randrange(1 << 31)).normal(0, sigma, pixels.shape)


def _to_image(pixels):
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))


def carbon(image: Image.Image, rng, ink=(45, 65, 185), paper=(244, 244, 236), strength=.6, pressure=(.75, 1.0)) -> Image.Image:
    """Dark print becomes ink transferred through carbon: coloured, faint, as hard as the pen or printer pressed."""
    dark = 1 - np.asarray(image.convert("L"), dtype=np.float32) / 255
    mask = pressure[0] + (pressure[1] - pressure[0]) * _noise(rng, dark.shape)
    alpha = np.clip(dark * mask * strength, 0, 1)[..., None]
    out = np.array(paper, dtype=np.float32) * (1 - alpha) + np.array(ink, dtype=np.float32) * alpha
    return _to_image(_speckle(rng, out, 4))


def smudge(image: Image.Image, rng) -> Image.Image:
    out = image.copy()
    w, h = image.size
    for _ in range(rng.randrange(4, 9)):
        box = (rng.randrange(0, w - 300), rng.randrange(0, h - 200))
        box = (*box, box[0] + rng.randrange(180, 420), box[1] + rng.randrange(60, 150))
        patch = out.crop(box).filter(ImageFilter.GaussianBlur(rng.uniform(3, 6)))
        out.paste(patch, box)
        tint = Image.new("L", image.size, 0)
        ImageDraw.Draw(tint).ellipse(box, fill=rng.randrange(25, 60))
        out.paste(Image.new("RGB", image.size, (70, 80, 150)), mask=tint.filter(ImageFilter.GaussianBlur(14)))
    return out


def show_through(image: Image.Image, back: Image.Image, rng) -> Image.Image:
    ghost = ImageOps.mirror(back.convert("L")).resize(image.size)
    pixels = np.asarray(image, dtype=np.float32)
    dark = (1 - np.asarray(ghost, dtype=np.float32) / 255)[..., None]
    return _to_image(pixels * (1 - dark * rng.uniform(.14, .24)))


def perspective(image: Image.Image, rng) -> Image.Image:
    w, h = image.size
    d = lambda: rng.uniform(.015, .06)  # noqa: E731
    src = [(0, 0), (w, 0), (w, h), (0, h)]
    dst = [(w * d(), h * d()), (w * (1 - d()), h * d()), (w * (1 - d()), h * (1 - d())), (w * d(), h * (1 - d()))]
    matrix = []
    for (x, y), (u, v) in zip(dst, src):
        matrix.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        matrix.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    coeffs = np.linalg.solve(np.array(matrix, dtype=float), np.array(src, dtype=float).reshape(8))
    out = image.transform((w, h), Image.PERSPECTIVE, tuple(coeffs), Image.BICUBIC, fillcolor=(62, 54, 48))
    shade = .55 + .45 * _noise(rng, (h, w), 3)
    return _to_image(_speckle(rng, np.asarray(out, dtype=np.float32) * shade[..., None], 6))


def fold_lines(image: Image.Image, rng) -> Image.Image:
    draw = ImageDraw.Draw(image)
    w, h = image.size
    for _ in range(rng.randrange(1, 3)):
        y = rng.randrange(h // 4, 3 * h // 4)
        draw.line([(0, y), (w, y + rng.randrange(-8, 8))], fill=(120, 118, 112), width=rng.randrange(2, 5))
        draw.line([(0, y + 5), (w, y + 5)], fill=(236, 236, 232), width=2)
    x = rng.randrange(w // 3, 2 * w // 3)
    draw.line([(x, 0), (x + rng.randrange(-8, 8), h)], fill=(130, 128, 122), width=3)
    return image.filter(ImageFilter.GaussianBlur(.5))


def fax(image: Image.Image, rng) -> Image.Image:
    pixels = np.asarray(image.convert("L"), dtype=np.float32)
    pixels = _speckle(rng, pixels, 28)
    out = np.where(pixels > 150, 255, 0).astype(np.uint8)
    for _ in range(rng.randrange(3, 9)):
        out[rng.randrange(out.shape[0]) :][:rng.randrange(1, 3)] = 255  # Dropped scan lines.
    return Image.fromarray(out).convert("RGB")


def thermal_faded(image: Image.Image, rng) -> Image.Image:
    dark = 1 - np.asarray(image.convert("L"), dtype=np.float32) / 255
    fade = (.18 + .3 * _noise(rng, dark.shape, 5)) * (0.75 + .25 * np.sin(np.arange(dark.shape[1]) / rng.uniform(18, 60)))
    out = 238 - 190 * np.clip(dark * fade, 0, 1)
    return _to_image(_speckle(rng, out, 3)).convert("RGB")


# Handwriting.
def hand_font():
    return os.environ.get("INVOICE_SIM_HANDWRITING_FONT") or DEJAVU_OBLIQUE


def write(image: Image.Image, xy, value, rng, size=34, color=(20, 20, 30)):
    """Draw text glyph by glyph: each one tilted, shifted and spaced differently, as a hand does."""
    font = ImageFont.truetype(hand_font(), size)
    x, y = xy
    slope = rng.uniform(-.015, .015)
    for ch in str(value):
        if ch == " ":
            x += int(size * .35)
            continue
        box = font.getbbox(ch)
        tile = Image.new("L", (box[2] + 14, size + 20), 0)
        ImageDraw.Draw(tile).text((5, 5), ch, fill=255, font=font)
        tile = tile.rotate(rng.uniform(-6, 6), resample=Image.BICUBIC)
        image.paste(Image.new("RGB", tile.size, color), (int(x), int(y + rng.uniform(-2.5, 2.5) + (x - xy[0]) * slope)), tile)
        x += box[2] + rng.uniform(.5, 3.5)
    return x


def preprinted_form(inv: Invoice, rng, numbers_only: bool) -> Image.Image:
    """A preprinted pad: supplier and labels in print, values by hand. numbers_only prints the descriptions and codes."""
    t = inv.truth
    style = inv.style
    labels = {k: rng.choice(v).replace("{rate}%", "").strip() for k, v in LABELS.items()}
    w, h = 1654, 2339
    image = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(image)
    big, mid, small = (ImageFont.truetype(DEJAVU, s) for s in (46, 30, 25))
    d.text((110, 90), t["supplier_name"], fill="black", font=big)
    d.text((110, 150), f"P.O. Box {rng.randrange(100, 9999)}  TRN {style['trn']}", fill="black", font=small)
    d.text((w // 2, 215), labels["title"], fill="black", font=mid, anchor="ma")
    y = 290
    for key, value in (("number", t["number"]), ("date", t["date_printed"]), ("po", t["po"]), ("currency", t["currency"])):
        if value is None:
            continue
        d.text((110, y), labels[key] + ":", fill="black", font=small)
        d.line([(440, y + 34), (1000, y + 34)], fill="black", width=1)
        write(image, (450, y - 10), value, rng)
        y += 62
    d.text((110, y + 6), labels["buyer"] + ":", fill="black", font=small)
    d.line([(440, y + 40), (1000, y + 40)], fill="black", width=1)
    write(image, (450, y - 4), style["buyer"], rng)
    y += 92
    cols = [("sku", 240), ("description", 560), ("uom", 110), ("qty", 130), ("price", 170), ("net_amount", 224)]
    x0 = 110
    d.rectangle([x0, y, x0 + sum(c[1] for c in cols), y + 54], outline="black", width=2)
    x = x0
    for key, width in cols:
        d.text((x + 8, y + 14), labels[key][: width // 15], fill="black", font=small)
        x += width
        d.line([(x, y), (x, y + 54 + 60 * max(len(t["lines"]), 12))], fill="black", width=1)
    d.line([(x0, y), (x0, y + 54 + 60 * max(len(t["lines"]), 12))], fill="black", width=1)
    y += 54
    dec, numfmt = t["dec"], style["numfmt"]
    money = lambda v: fmt_number(Decimal(v), dec, numfmt, style["neg"])  # noqa: E731
    for line in t["lines"][:24]:
        x = x0
        for key, width in cols:
            value = line.get(key)
            if key in ("price", "net_amount"):
                value = money(value)
            if value:
                if numbers_only and key in ("sku", "description", "uom"):
                    d.text((x + 8, y + 16), str(value)[: width // 13], fill="black", font=small)
                else:
                    write(image, (x + 8, y + 4), str(value)[: width // 15], rng, 32)
            x += width
        y += 60
        d.line([(x0, y), (x0 + sum(c[1] for c in cols), y)], fill="black", width=1)
    y += 26 + 60 * max(0, 12 - len(t["lines"]))
    for key, value in (("net", t["net"]), ("tax", t["tax"]), ("gross", t["gross"])):
        if value is None:
            continue
        d.text((900, y + 8), labels[key].replace(" @", ""), fill="black", font=small)
        write(image, (1300, y - 4), money(value), rng, 34)
        y += 58
    return image


# Overlaps: what covers the printed value is placed from the PDF's own word positions.
def _find(pdf: Path, needle: str, scale: float):
    with pdfplumber.open(pdf) as doc:
        for number, page in reversed(list(enumerate(doc.pages))):
            for word in reversed(page.extract_words()):
                if needle in word["text"]:
                    return number, tuple(v * scale for v in (word["x0"], word["top"], word["x1"], word["bottom"]))
    return None


def _printed(inv, field):
    return fmt_number(abs(Decimal(inv.truth[field])), inv.truth["dec"], inv.style["numfmt"], inv.style["neg"])


def stamp(image: Image.Image, centre, rng) -> Image.Image:
    size = 420
    layer = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(layer)
    d.ellipse([12, 12, size - 12, size - 12], outline=255, width=9)
    d.ellipse([46, 46, size - 46, size - 46], outline=255, width=4)
    d.text((size // 2, size // 2 - 24), rng.choice(("PAID", "RECEIVED", "APPROVED", "GRN DONE")), fill=255, anchor="mm", font=ImageFont.truetype(DEJAVU, 54))
    d.text((size // 2, size // 2 + 38), f"{rng.randrange(1, 28):02d} MAR 2026", fill=255, anchor="mm", font=ImageFont.truetype(DEJAVU, 30))
    layer = layer.rotate(rng.uniform(-25, 25), resample=Image.BICUBIC).point(lambda v: int(v * .72))
    color = rng.choice(((190, 30, 40), (30, 50, 170), (110, 40, 150)))
    image.paste(Image.new("RGB", layer.size, color), (int(centre[0] - size / 2), int(centre[1] - size / 2)), layer)
    return image


def signature(image: Image.Image, centre, rng) -> Image.Image:
    d = ImageDraw.Draw(image)
    points = []
    x, y = centre[0] - 260, centre[1]
    for i in range(70):
        points.append((x + i * 7.5, y + 44 * math.sin(i / rng.uniform(2.0, 3.6)) * rng.uniform(.6, 1.1)))
    d.line(points, fill=(25, 35, 130), width=4, joint="curve")
    return image


def watermark(image: Image.Image, rng) -> Image.Image:
    word = rng.choice(("COPY", "DUPLICATE", "ORIGINAL", "NOT A TAX INVOICE"))
    w, h = image.size
    layer = Image.new("L", (w, h), 0)
    size = int(min(w, h) * (.22 if len(word) < 9 else .11))
    ImageDraw.Draw(layer).text((w // 2, h // 2), word, fill=rng.randrange(85, 140), anchor="mm", font=ImageFont.truetype(DEJAVU, size))
    layer = layer.rotate(rng.uniform(25, 50), resample=Image.BICUBIC)
    image.paste(Image.new("RGB", (w, h), (90, 90, 90)), mask=layer)
    return image


def crossed_out(image: Image.Image, box, inv, rng) -> Image.Image:
    """The printed value is the stale one, struck through, with the true one written above it."""
    x0, y0, x1, y1 = box
    true = Decimal(inv.truth["net"])
    stale = fmt_number(abs(true) * Decimal(rng.choice(("1.10", "0.90", "1.25"))), inv.truth["dec"], inv.style["numfmt"], "-")
    d = ImageDraw.Draw(image)
    d.rectangle([x0 - 4, y0 - 2, x1 + 4, y1 + 2], fill="white")
    font = ImageFont.truetype(DEJAVU, int((y1 - y0) * .9))
    d.text((x1, y0), stale, fill="black", font=font, anchor="ra")
    d.line([(x1 - font.getlength(stale) - 6, (y0 + y1) / 2 + 2), (x1 + 6, (y0 + y1) / 2 - 2)], fill=(30, 30, 120), width=4)
    write(image, (x1 - font.getlength(stale), y0 - 44), _printed(inv, "net"), rng, 34, (30, 30, 120))
    return image


def _pages(pdf: Path, dpi: int):
    doc = pypdfium2.PdfDocument(str(pdf))
    try:
        return [page.render(scale=dpi / 72).to_pil().convert("RGB") for page in doc]
    finally:
        doc.close()


def _save(pages, target: Path, dpi: int) -> Path:
    if target.suffix == ".jpg":
        pages[0].convert("RGB").save(target, "JPEG", quality=72)
    else:
        pages[0].convert("RGB").save(target, "PDF", save_all=True, append_images=[p.convert("RGB") for p in pages[1:]], resolution=dpi)
    return target


def render_degraded(inv: Invoice, stem: Path) -> Path:
    """Render inv as its family and return the file. stem has no suffix."""
    family, rng = inv.family, _rng(inv)
    suffix = ".jpg" if family in JPEG else ".pdf"
    target = stem.with_suffix(suffix)
    if "handwritten" in family:
        form = preprinted_form(inv, rng, "numbers" in family)
        if family.startswith("carbon"):
            form = carbon(form, rng, strength=.62, pressure=(.55, 1.0))
        return _save([form], target, 200)
    dpi = {"very-low-dpi": 72, "fax-black-white": 100, "thermal-faded": 150}.get(family, 200)
    with tempfile.TemporaryDirectory() as folder:
        pdf = render_pdf(inv, Path(folder) / "base.pdf")
        pages = _pages(pdf, dpi)
        scale = dpi / 72
        if family.startswith("overlap-") and family != "overlap-watermark":
            field = "net" if family == "overlap-crossed-out-correction" else rng.choice(("gross", "net"))
            spot = _find(pdf, _printed(inv, field), scale)
            if spot:
                n, box = spot
                centre = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
                if family == "overlap-stamp-totals":
                    pages[n] = stamp(pages[n], centre, rng)
                elif family == "overlap-signature-totals":
                    pages[n] = signature(pages[n], centre, rng)
                else:
                    pages[n] = crossed_out(pages[n], box, inv, rng)
    out = []
    for page in pages:
        out.append({
            "carbon-blue": lambda p: carbon(p, rng),
            "carbon-purple": lambda p: carbon(p, rng, ink=(115, 50, 165)),
            "carbon-uneven-pressure": lambda p: carbon(p, rng, pressure=(.25, 1.0)),
            "carbon-smudged": lambda p: smudge(carbon(p, rng), rng),
            "carbon-blurred": lambda p: carbon(p, rng).filter(ImageFilter.GaussianBlur(rng.uniform(1.1, 1.7))),
            "carbon-tinted-paper": lambda p: carbon(p, rng, paper=rng.choice(((250, 215, 225), (250, 240, 170), (205, 235, 210)))),
            "carbon-show-through": lambda p: show_through(carbon(p, rng), p, rng),
            "photo-perspective-shadow": lambda p: perspective(p, rng),
            "photo-fold-lines": lambda p: fold_lines(p, rng),
            "rotated-90": lambda p: p.rotate(90, expand=True),
            "rotated-180": lambda p: p.rotate(180, expand=True),
            "rotated-270": lambda p: p.rotate(270, expand=True),
            "cropped-margins": lambda p: p.crop((int(p.width * .05), int(p.height * .03), int(p.width * .95), int(p.height * .97))),
            "cropped-into-text": lambda p: p.crop((int(p.width * .1), 0, int(p.width * .88), p.height)),
            "very-low-dpi": lambda p: p,
            "fax-black-white": lambda p: fax(p, rng),
            "thermal-faded": lambda p: thermal_faded(p, rng),
        }.get(family, lambda p: p)(page))
        if family == "overlap-watermark":
            out[-1] = watermark(out[-1], rng)
    return _save(out, target, dpi)
