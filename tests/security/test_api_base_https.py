import pytest

from api_base import resolve_api_base


def test_production_http_api_base_is_rejected():
    with pytest.raises(RuntimeError, match="HTTPS"):
        resolve_api_base({"API_BASE": "http://vizhi.rcsaware.com"})


def test_https_api_base_is_accepted():
    assert resolve_api_base({"API_BASE": "https://vizhi.rcsaware.com"}) == "https://vizhi.rcsaware.com"


def test_localhost_http_is_allowed():
    assert resolve_api_base({"API_BASE": "http://localhost:3000"}) == "http://localhost:3000"
