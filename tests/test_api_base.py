from api_base import resolve_api_base


def test_cli_override_wins_over_config_and_env():
    assert (
        resolve_api_base(
            {"API_BASE": "https://from-config.example"},
            {"API_BASE": "https://from-env.example"},
            override="http://localhost:3000",
        )
        == "http://localhost:3000"
    )


def test_config_wins_without_override():
    assert (
        resolve_api_base({"API_BASE": "https://from-config.example"}, {})
        == "https://from-config.example"
    )


def test_blank_override_is_ignored():
    assert (
        resolve_api_base(
            {"API_BASE": "https://from-config.example"},
            {},
            override="  ",
        )
        == "https://from-config.example"
    )
