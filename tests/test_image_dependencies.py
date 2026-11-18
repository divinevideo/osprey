"""Exercise the image codecs and hashing libraries installed in worker images."""

from io import BytesIO

import imagehash
import numpy as np
import pdqhash
import pytest
from PIL import Image


@pytest.fixture
def image() -> Image.Image:
    y, x = np.indices((64, 64))
    pixels = np.stack((x * 4, y * 4, (x + y) * 2), axis=-1).astype(np.uint8)
    return Image.fromarray(pixels)


@pytest.mark.parametrize('format', ['PNG', 'WEBP'])
def test_lossless_decode_preserves_pixels_and_hashes(image: Image.Image, format: str) -> None:
    encoded = BytesIO()
    image.save(encoded, format=format, lossless=True)
    encoded.seek(0)

    with Image.open(encoded) as decoded:
        decoded.load()
        rgb = decoded.convert('RGB')
        np.testing.assert_array_equal(np.asarray(rgb), np.asarray(image))
        assert imagehash.phash(rgb) == imagehash.phash(image)
        assert imagehash.dhash(rgb) == imagehash.dhash(image)
        expected_hash, expected_quality = pdqhash.compute(np.asarray(image))
        actual_hash, actual_quality = pdqhash.compute(np.asarray(rgb))
        np.testing.assert_array_equal(actual_hash, expected_hash)
        assert actual_quality == expected_quality


def test_jpeg_decode_produces_a_usable_perceptual_hash(image: Image.Image) -> None:
    encoded = BytesIO()
    image.save(encoded, format='JPEG', quality=95)
    encoded.seek(0)

    with Image.open(encoded) as decoded:
        decoded.load()
        assert decoded.size == image.size
        assert imagehash.dhash(decoded) - imagehash.dhash(image) <= 2
