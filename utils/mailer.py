"""SMTP 发信（stdlib ``smtplib``，不引入新依赖）。

- 未配置 SMTP（缺 host/user/password）时**降级**：只记日志、返回 False，不抛异常，
  这样注册链路在没接邮件服务时依然能跑通（配合跳过邮箱验证）。
- ``smtplib`` 是阻塞的，对外统一暴露 ``async`` 接口，内部丢线程池执行，
  避免卡住 FastAPI 事件循环。

配置项见 ``utils.configs``：SMTP_HOST/PORT/USER/PASSWORD/FROM/FROM_NAME/SSL。
"""
from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.mime.text import MIMEText
from email.utils import formataddr
from string import Template

import utils.configs as configs
from utils.Logger import logger

# Swiss 黑白风邮件模板（内联样式，邮件客户端兼容）
_TPL = Template("""<!DOCTYPE html>
<html><body style="margin:0;padding:32px;background:#f4f4f4;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:#0a0a0a;">
  <div style="max-width:480px;margin:0 auto;background:#ffffff;border:1px solid #0a0a0a;padding:36px 32px;">
    <div style="font-weight:700;font-size:18px;letter-spacing:-0.02em;margin-bottom:26px;">Chat-Share</div>
    <h1 style="font-size:22px;font-weight:700;letter-spacing:-0.02em;margin:0 0 12px;">$title</h1>
    <p style="font-size:14px;line-height:1.7;color:#666;margin:0 0 28px;">$body</p>
    <a href="$cta_url" style="display:inline-block;background:#000000;color:#ffffff;text-decoration:none;padding:12px 26px;font-size:14px;font-weight:500;">$cta_text</a>
    <p style="font-size:12px;line-height:1.7;color:#999;margin:28px 0 0;">如果按钮无法点击，复制此链接到浏览器打开：<br><span style="color:#666;">$cta_url</span></p>
    <p style="font-size:12px;color:#999;margin:14px 0 0;">若非本人操作，忽略本邮件即可，你的账号不会受影响。</p>
  </div>
</body></html>""")


def _send_sync(to: str, subject: str, html: str) -> bool:
    if not configs.smtp_configured():
        logger.warning(f"[mailer] SMTP 未配置，跳过发信 -> {to} | {subject}")
        return False
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((configs.smtp_from_name, configs.sender_address()))
    msg["To"] = to
    try:
        ctx = ssl.create_default_context()
        if configs.smtp_ssl:
            with smtplib.SMTP_SSL(configs.smtp_host, configs.smtp_port, context=ctx, timeout=15) as s:
                s.login(configs.smtp_user, configs.smtp_password)
                s.sendmail(configs.sender_address(), [to], msg.as_string())
        else:
            with smtplib.SMTP(configs.smtp_host, configs.smtp_port, timeout=15) as s:
                if configs.smtp_starttls:
                    s.starttls(context=ctx)
                if configs.smtp_user:
                    s.login(configs.smtp_user, configs.smtp_password)
                s.sendmail(configs.sender_address(), [to], msg.as_string())
        logger.info(f"[mailer] sent -> {to} | {subject}")
        return True
    except Exception as e:
        logger.error(f"[mailer] send failed -> {to}: {e}")
        return False


async def send_email(to: str, subject: str, html: str) -> bool:
    """异步发信（内部走线程池，不阻塞事件循环）。"""
    return await asyncio.to_thread(_send_sync, to, subject, html)


def _wrap(title: str, body: str, cta_text: str, cta_url: str) -> str:
    return _TPL.substitute(title=title, body=body, cta_text=cta_text, cta_url=cta_url)


async def send_verify_email(to: str, token: str) -> bool:
    url = f"{configs.site_base_url}/verify-email?token={token}"
    html = _wrap("验证你的邮箱", "点击下面的按钮完成邮箱验证，之后即可正常使用账号。", "验证邮箱", url)
    return await send_email(to, "验证你的 Chat-Share 邮箱", html)


async def send_reset_email(to: str, token: str) -> bool:
    url = f"{configs.site_base_url}/reset-password?token={token}"
    html = _wrap("重置密码", "点击下面的按钮设置新密码。链接有效期 30 分钟，过期请重新申请。", "重置密码", url)
    return await send_email(to, "重置你的 Chat-Share 密码", html)
