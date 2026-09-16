from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from .config import DeliveryConfig
from .utils import env


def _resolve_addresses(config: DeliveryConfig) -> tuple[str, str, list[str]]:
    env_address = (env(config.email_address_env) or "").strip()
    username = (config.username or env_address).strip()
    from_address = (config.from_address or env_address or username).strip()
    recipients = [address.strip() for address in config.to_addresses if address.strip()]
    if not recipients and env_address:
        recipients = [env_address]
    if not username:
        raise RuntimeError(f"未配置 QQ 邮箱地址：请在 .env 中设置 {config.email_address_env}。")
    if not from_address:
        raise RuntimeError("未配置发件人地址。")
    if not recipients:
        raise RuntimeError("未配置收件人地址。")
    return username, from_address, recipients


def send_digest(config: DeliveryConfig, subject: str, html_body: str) -> None:
    if not config.email_enabled:
        return
    password = env(config.password_env)
    if not password:
        raise RuntimeError(f"邮件已启用，但环境变量 {config.password_env} 未设置。")
    username, from_address, recipients = _resolve_addresses(config)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_address
    message["To"] = ", ".join(recipients)
    message.set_content("请使用支持 HTML 的邮件客户端查看 arXiv 科研日报。")
    message.add_alternative(html_body, subtype="html")
    _send_message(config, username, password, message)


def _send_message(config: DeliveryConfig, username: str, password: str, message: EmailMessage) -> None:
    try:
        if config.smtp_ssl:
            with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, context=ssl.create_default_context()) as smtp:
                smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(config.smtp_host, config.smtp_port) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(username, password)
                smtp.send_message(message)
    except PermissionError as exc:
        raise RuntimeError("当前运行环境禁止建立外部 SMTP 连接；请在普通本地终端或计划任务中执行测试。") from exc


def send_test_email(config: DeliveryConfig) -> None:
    send_digest(
        config,
        "PaperLoom｜QQ邮箱配置测试",
        """<html><body style="font-family:system-ui,sans-serif">
<h2>QQ 邮箱配置成功</h2>
<p>这是一封由 PaperLoom 发送的测试邮件。</p>
<p>收到此邮件说明 SMTP 地址、SSL、邮箱帐号和授权码均可正常使用。</p>
</body></html>""",
    )
