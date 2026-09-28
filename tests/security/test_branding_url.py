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
    assert (
        branding.is_trusted_url(
            "https://avrhrxmoikcgddvolqea.supabase.co/storage/v1/object/public/branding/x.png"
        )
        is True
    )
    assert (
        branding.is_trusted_url(
            "https://abcd.supabase.co/storage/v1/object/sign/branding/x.png?token=t"
        )
        is True
    )
    assert branding.is_trusted_url("https://avrhrxmoikcgddvolqea.supabase.co/private/x.png") is False
    assert (
        branding.is_trusted_url("https://evil.supabase.co.attacker.com/storage/v1/object/public/x")
        is False
    )
    assert branding.is_trusted_url("http://localhost:3000/a.png") is True
    assert (
        branding.is_trusted_url(
            "https://staging.example.com/branding/a.png",
            api_base="https://staging.example.com",
        )
        is True
    )
    assert (
        branding.is_trusted_url(
            "https://other.example.com/branding/a.png",
            api_base="https://staging.example.com",
        )
        is False
    )
