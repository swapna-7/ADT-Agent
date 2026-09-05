from __future__ import annotations

import pytest

from enrollment import EnrollmentError, _clean_username, enroll


def test_clean_username_keeps_sam_account():
    assert _clean_username("ASUS\\swapn") == "ASUS\\swapn"


def test_clean_username_rejects_system_accounts():
    assert _clean_username("SYSTEM") is None
    assert _clean_username("NT AUTHORITY\\SYSTEM") is None
    assert _clean_username("NT AUTHORITY\\LOCAL SERVICE") is None


def test_enroll_does_not_follow_login_redirect(monkeypatch: pytest.MonkeyPatch):
    class FakeResp:
        status_code = 307
        url = "https://vizhi.rcsaware.com/api/agent/enroll"
        history = []
        headers = {"Location": "/auth/login?next=/api/agent/enroll", "Content-Type": "text/html"}

        def json(self):
            raise ValueError("not json")

    def fake_post(*_args, **kwargs):
        assert kwargs.get("allow_redirects") is False
        return FakeResp()

    monkeypatch.setattr("enrollment.requests.post", fake_post)
    monkeypatch.setattr("enrollment.system_hostname", lambda: "debug-host")
    monkeypatch.setattr("enrollment.machine_guid", lambda: "guid-1")

    with pytest.raises(EnrollmentError) as exc:
        enroll("https://vizhi.rcsaware.com", "VZ-ABCD-1234-5678", "2.1.0")

    message = str(exc.value).lower()
    assert "redirect" in message
    assert "307" in message
