from desktop_alerts import open_action_url


def test_open_action_url_rejects_http(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr("desktop_alerts.webbrowser.open", lambda url: opened.append(url))
    open_action_url("http://evil.example/phish")
    assert opened == []


def test_open_action_url_allows_https(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr("desktop_alerts.webbrowser.open", lambda url: opened.append(url))
    open_action_url("https://vizhi.rcsaware.com/help")
    assert opened == ["https://vizhi.rcsaware.com/help"]
