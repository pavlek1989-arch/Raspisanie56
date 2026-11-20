"""Интеграция с MAX Bot API (мессенджер МАКС).

Документация: https://dev.max.ru/docs-api
Актуальный домен: platform-api2.max.ru

Основные операции:
  - отправка сообщения пользователю (POST /messages)
  - регистрация webhook-подписки (POST /subscriptions)
"""

from __future__ import annotations

import os
import logging

import requests

# Официальный домен API. Некоторые примеры используют platform-api.max.ru,
# dev.max.ru рекомендует platform-api2.max.ru. Значение можно переопределить
# переменной окружения MAX_API_BASE.
API_BASE = os.getenv("MAX_API_BASE", "https://platform-api2.max.ru")


class MaxBotError(Exception):
    """Ошибка взаимодействия с MAX Bot API."""


class MaxBotClient:
    def __init__(self, token: str, base_url: str = API_BASE, timeout: float = 20.0):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"{token}",
                "Content-Type": "application/json",
            }
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = self._url(path)
        resp = self.session.request(method, url, json=payload, timeout=self.timeout)
        if resp.status_code >= 400:
            detail = resp.text[:500]
            raise MaxBotError(f"MAX API {method} {path} -> {resp.status_code}: {detail}")
        try:
            return resp.json()
        except ValueError:
            return {}

    def _messages_query(self, chat_id=None, user_id=None) -> dict:
        """Собирает query-параметры для POST /messages (user_id или chat_id)."""
        params = {}
        if user_id is not None:
            params["user_id"] = int(user_id)
        elif chat_id is not None:
            params["chat_id"] = int(chat_id)
        return params

    def send_message(self, chat_id, text: str, *, user_id=None, disable_link_preview: bool = True) -> dict:
        """Отправляет текстовое сообщение в диалог/чат/канал.

        user_id — для лички (приоритетнее), chat_id — для групп и каналов.
        Передаются как query-параметры (официальный формат MAX).
        """
        body = {
            "text": text,
            "disable_link_preview": disable_link_preview,
        }
        params = self._messages_query(chat_id=chat_id, user_id=user_id)
        if not params:
            raise MaxBotError("Нет user_id и chat_id для отправки сообщения")
        # Соберём URL с query вручную, т.к. payload уже json.
        from urllib.parse import urlencode

        return self._request("POST", f"/messages?{urlencode(params)}", payload=body)

    def send_message_to_user(self, user_id: int, text: str) -> dict:
        """Отправляет сообщение в личный диалог с пользователем по user_id."""
        return self.send_message(None, text, user_id=user_id)

    def send_message_with_keyboard(
        self,
        chat_id,
        text: str,
        buttons: list[list[dict]],
        *,
        user_id=None,
        disable_link_preview: bool = True,
    ) -> dict:
        """Отправляет сообщение с inline-клавиатурой.

        buttons — список рядов; каждый ряд — список кнопок:
          {"type": "message_button", "text": "5А"}           # имитирует ввод текста
          {"type": "callback_button", "text": "Помощь", "callback_data": "help"}
          {"type": "link", "text": "Сайт", "url": "https://..."}

        user_id — для лички, chat_id — для групп/каналов (query-параметры).
        """
        from urllib.parse import urlencode

        body = {
            "text": text,
            "disable_link_preview": disable_link_preview,
            "attachments": [
                {
                    "type": "inline_keyboard",
                    "payload": {"buttons": buttons},
                }
            ],
        }
        params = self._messages_query(chat_id=chat_id, user_id=user_id)
        if not params:
            raise MaxBotError("Нет user_id и chat_id для отправки сообщения")
        return self._request("POST", f"/messages?{urlencode(params)}", payload=body)

    def answer_on_callback(
        self,
        callback_id: str,
        text: str | None = None,
        *,
        notification: str | None = None,
        update_message: bool = False,
    ) -> dict:
        """Отвечает на нажатие callback-кнопки (событие message_callback).

        callback_id — идентификатор из события (updates[].callback.callback_id).

        Либо отправляет одноразовое уведомление (notification),
        либо (update_message=True + text) обновляет сообщение с кнопками.
        """
        payload: dict = {}
        if text is not None and update_message:
            payload["message"] = {"text": text}
        if notification is not None:
            payload["notification"] = notification
        return self._request("POST", f"/answers?callback_id={callback_id}", payload=payload)

    def subscribe_webhook(
        self, url: str, secret: str, update_types: list[str] | None = None
    ) -> dict:
        """Регистрирует webhook, на который MAX будет слать события.

        update_types — список типов событий; дефолт подходит для сообщений.
        """
        body = {"url": url}
        if secret:
            body["secret"] = secret
        if update_types:
            body["update_types"] = update_types
        return self._request("POST", "/subscriptions", payload=body)

    def get_updates(self, **params) -> dict:
        """Long polling (для разработки). В проде лучше webhook."""
        return self._request("GET", "/updates", payload=params)


def validate_webhook_request(headers: dict, expected_secret: str) -> bool:
    """Проверяет заголовок X-Max-Bot-Api-Secret на соответствие секрету."""
    got = headers.get("X-Max-Bot-Api-Secret", "")
    if not expected_secret:
        return True
    import hmac

    return hmac.compare_digest(got, expected_secret)