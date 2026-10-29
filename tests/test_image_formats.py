import io

import PIL
import pytest
from PIL import Image, UnidentifiedImageError

from app.vertex import _document_parts

# A valid 1x1 X BitMap: a format the app never accepts, so its decoder must stay unreachable.
XBM = b"#define im_width 1\n#define im_height 1\nstatic char im_bits[] = {\n0x00\n};\n"


def test_pillow_carries_the_fixes_for_the_known_decoder_vulnerabilities():
    assert int(PIL.__version__.split(".")[0]) >= 13


def test_an_exotic_format_behind_an_image_name_is_never_decoded(tmp_path):
    assert Image.open(io.BytesIO(XBM)).format == "XBM"
    disguised = tmp_path / "invoice.png"
    disguised.write_bytes(XBM)
    with pytest.raises(UnidentifiedImageError):
        _document_parts(disguised)


def test_an_accepted_format_is_still_read(tmp_path):
    real = tmp_path / "invoice.png"
    Image.new("RGB", (4, 4), "white").save(real)
    assert _document_parts(real)[0]["inlineData"]["mimeType"] == "image/png"
