"""通知: メール(SMTP)と LINE(Messaging API)。LINE Notify は 2025年3月に終了しているため使えません。"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import requests

log = logging.getLogger("toshi.notify")


class Notifier:
    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def email_on(self) -> bool:
        c = self.cfg
        return bool(c.smtp_host and c.smtp_user and c.smtp_password and (c.mail_to or c.smtp_user))

    @property
    def line_on(self) -> bool:
        return bool(self.cfg.line_token and self.cfg.line_user_id)

    def channels(self) -> list[str]:
        return [n for n, on in (("メール", self.email_on), ("LINE", self.line_on)) if on]

    def send(self, subject: str, text: str) -> list[dict]:
        """設定済みの全チャネルに送る。1つが失敗しても他は送る。結果は [{channel, ok, error}]。"""
        out = []
        if self.email_on:
            out.append(self._run("メール", lambda: self._email(subject, text)))
        if self.line_on:
            out.append(self._run("LINE", lambda: self._line(subject + "\n\n" + text)))
        return out

    @staticmethod
    def _run(name: str, fn) -> dict:
        try:
            fn()
            return {"channel": name, "ok": True, "error": ""}
        except Exception as e:  # noqa: BLE001
            log.warning("notify %s failed: %s", name, e)
            return {"channel": name, "ok": False, "error": str(e)[:200]}

    def _email(self, subject: str, text: str) -> None:
        c = self.cfg
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, c.smtp_user, c.mail_to or c.smtp_user
        msg.set_content(text)
        with smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=30) as s:
            s.starttls()
            s.login(c.smtp_user, c.smtp_password)
            s.send_message(msg)

    def _line(self, text: str) -> None:
        r = requests.post("https://api.line.me/v2/bot/message/push", timeout=30,
                          headers={"Authorization": f"Bearer {self.cfg.line_token}"},
                          json={"to": self.cfg.line_user_id, "messages": [{"type": "text", "text": text[:4900]}]})
        if r.status_code != 200:
            raise RuntimeError(f"LINE {r.status_code}: {r.text[:150]}")
