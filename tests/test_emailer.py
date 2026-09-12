from arxiv_ra.config import DeliveryConfig
from arxiv_ra.emailer import _resolve_addresses


def test_qq_email_env_is_used_for_all_addresses(monkeypatch) -> None:
    monkeypatch.setenv("QQ_EMAIL", "123456789@qq.com")
    config = DeliveryConfig(email_address_env="QQ_EMAIL")
    username, sender, recipients = _resolve_addresses(config)
    assert username == "123456789@qq.com"
    assert sender == "123456789@qq.com"
    assert recipients == ["123456789@qq.com"]

