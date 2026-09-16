from pathlib import Path

import branding


def test_download_image_rejects_untrusted_url(tmp_path: Path):
    branding.BRANDING_CACHE_DIR = tmp_path
    try:
        branding.download_image("https://evil.example/wall.jpg")
        raised = False
        message = ""
    except ValueError as exc:
        raised = True
        message = str(exc)
    assert raised
    assert "Untrusted" in message


def test_is_trusted_url_requires_https_allowlist():
    assert branding.is_trusted_url("https://vizhi.rcsaware.com/branding/a.png") is True
    assert branding.is_trusted_url("http://vizhi.rcsaware.com/branding/a.png") is False
    assert branding.is_trusted_url("https://evil.example/a.png") is False
