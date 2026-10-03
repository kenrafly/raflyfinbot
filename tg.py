"""Minimal Telegram Bot API client (standard library only)."""
import http.client
import json
import os
import threading
import uuid
from urllib.parse import urlparse

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")

_local = threading.local()


class TelegramError(Exception):
    pass


def _request(method: str, body: bytes, content_type: str) -> dict:
    url = urlparse(API_BASE)
    for attempt in (1, 2):
        try:
            conn = getattr(_local, "conn", None)
            if conn is None:
                maker = (http.client.HTTPConnection if url.scheme == "http"
                         else http.client.HTTPSConnection)
                conn = _local.conn = maker(url.netloc, timeout=20)
            conn.request("POST", f"/bot{BOT_TOKEN}/{method}", body,
                         {"Content-Type": content_type})
            data = conn.getresponse().read()
            break
        except (http.client.HTTPException, OSError):
            _local.conn = None
            if attempt == 2:
                raise TelegramError("Could not reach Telegram.")
    reply = json.loads(data)
    if not reply.get("ok"):
        raise TelegramError(reply.get("description", "Telegram error"))
    return reply.get("result")


def call(method: str, **params):
    body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
    return _request(method, body, "application/json")


def send(chat_id: int, text: str, keyboard: dict | None = None):
    return call("sendMessage", chat_id=chat_id, text=text, reply_markup=keyboard)


def send_document(chat_id: int, filename: str, data: bytes):
    boundary = uuid.uuid4().hex
    safe_name = filename.replace('"', "")
    body = b"".join([
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n"
        f"{chat_id}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
        f"filename=\"{safe_name}\"\r\nContent-Type: application/octet-stream"
        f"\r\n\r\n".encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    return _request("sendDocument", body, f"multipart/form-data; boundary={boundary}")


def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """[[("Label", "callback_data"), ...], ...] -> Telegram inline keyboard."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                for row in rows]}
