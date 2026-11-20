"""Чат-бот МАКС для школьного расписания (5–11 классы).

Расписание берётся из Google Sheets: лист = день, колонки = классы,
столбец A = время/номер урока. Пользователь пишет боту название класса
(например "5А" или "5-А"), бот показывает расписание на сегодня.

Запуск:
  pip install -r requirements.txt
  python bot.py
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, date, timedelta
from threading import Lock
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request
from dotenv import load_dotenv

from max_client import MaxBotClient, validate_webhook_request
from schedule_parser import ScheduleParser, normalize_class_name

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("schedule-bot")

app = Flask(__name__)

# --- Настройки ---
MAX_TOKEN = os.getenv("MAX_TOKEN", "")
MAX_WEBHOOK_SECRET = os.getenv("MAX_WEBHOOK_SECRET", "")
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID", "")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Yekaterinburg")

bot = MaxBotClient(MAX_TOKEN) if MAX_TOKEN else None
parser: ScheduleParser | None = None
try:
    if SPREADSHEET_ID:
        parser = ScheduleParser(SPREADSHEET_ID, timezone=TIMEZONE)
except Exception as exc:  # на старте не валимся, если файлы ещё не готовы
    log.warning("Не удалось инициализировать парсер: %s", exc)

# Соответствие индекса дня (0=пн) названию листа в книге.
# Листы идём по порядку книги.
DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def today_date() -> date:
    """Сегодняшняя дата по часовому поясу школы."""
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def build_menu_buttons() -> list[list[dict]]:
    """Собирает массив кнопок главного меню.

    Тип message_button имитирует ввод текста — при нажатии пользователь
    "печатает" подпись кнопки. Это удобно: выбор класса/дня идёт через ту же
    текстовую логику, не требуя ответа на callback.
    """
    classes = parser.get_available_classes() if parser else []
    buttons: list[list[dict]] = []

    # Ряд с действиями
    actions = [
        {"type": "message_button", "text": "Сегодня"},
        {"type": "message_button", "text": "Завтра"},
        {"type": "message_button", "text": "Сменить класс"},
        {"type": "message_button", "text": "Помощь"},
    ]
    buttons.append(actions)

    # Классы по рядам, до 4 в ряд
    row: list[dict] = []
    for cls in classes:
        row.append({"type": "message_button", "text": cls})
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return buttons


def send_main_menu(chat_id, user_id=None) -> None:
    """Отправляет главное меню с кнопками."""
    if not bot:
        return
    text = "📚 Выберите класс или действие:"
    bot.send_message_with_keyboard(chat_id, text, build_menu_buttons(), user_id=user_id)


def format_schedule(class_name: str, target: date) -> str:
    """Формирует текст расписания класса class_name на дату target.

    Ищет лист, название которого соответствует дате.
    """
    if parser is None:
        return "Расписание временно недоступно — таблица не подключена."
    day = parser.get_schedule_for_date(class_name, target)
    if not day.lessons:
        return (
            f"Для класса {class_name} на {day.day_name} ({target:%d.%m}) расписания не нашлось. "
            "Проверьте название класса или что лист с этой датой есть в таблице."
        )
    lines = [f"📅 Расписание {class_name} — {day.day_name} {target:%d.%m.%Y}:"]
    for i, lesson in enumerate(day.lessons, start=1):
        num = lesson.period or str(i)
        subj = lesson.subject
        room = f" — каб. {lesson.room}" if lesson.room else ""
        lines.append(f"{num}. {subj}{room}")
    return "\n".join(lines)


def is_likely_class(text: str) -> bool:
    """Проверяем, похоже ли сообщение на название класса (5А, 5-А, 9 б)."""
    normalized = normalize_class_name(text)
    return len(normalized) >= 1 and normalized[:1].isdigit()


# Состояние «ждём класс для конкретного дня».
# Ключ — chat_id строкой, значение — дата, для которой выбирается класс.
pending_day: dict[str, date] = {}


# --- Запоминание класса ученика -------------------------------------------
# Почти все запросы к ученика — «покажи расписание на <день>». Если класс уже
# выбран ранее, бот не спрашивает его заново: сразу отвечает расписанием.
# Сохраняется в JSON-файл, чтобы пережить перезапуск процесса.
USER_CLASS_FILE = os.getenv("USER_CLASS_FILE", "user_classes.json")
_user_classes: dict[str, str] = {}
_user_lock = Lock()


def _load_user_classes() -> None:
    global _user_classes
    try:
        with open(USER_CLASS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _user_classes = {str(k): str(v) for k, v in data.items()}
    except (FileNotFoundError, ValueError, OSError):
        _user_classes = {}


def _save_user_classes() -> None:
    try:
        with open(USER_CLASS_FILE, "w", encoding="utf-8") as f:
            json.dump(_user_classes, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        log.warning("Не удалось сохранить user_classes.json: %s", exc)


def get_user_class(key: str) -> str | None:
    with _user_lock:
        return _user_classes.get(key)


def set_user_class(key: str, class_name: str) -> None:
    with _user_lock:
        _user_classes[key] = class_name
        _save_user_classes()


def forget_user_class(key: str) -> bool:
    with _user_lock:
        existed = _user_classes.pop(key, None) is not None
        if existed:
            _save_user_classes()
        return existed


_load_user_classes()


def _resolve_target_date(tl: str) -> date | None:
    """Возвращает дату по тексту запроса ('сегодня'/'завтра'/'понедельник') или None."""
    if "завтра" in tl:
        return today_date() + timedelta(days=1)
    if tl == "сегодня":
        return today_date()
    weekday_map = {
        "понедельник": 0, "вторник": 1, "среда": 2, "среду": 2,
        "четверг": 3, "пятница": 4, "пятницу": 4, "суббота": 5, "субботу": 5,
        "воскресенье": 6,
    }
    for word, weekday in weekday_map.items():
        if word in tl:
            return _next_weekday(weekday, today_date())
    return None


def handle_message(chat_id, user_id, text: str):
    """Главная логика: что ответить пользователю."""
    if not bot:
        log.error("Бот не инициализирован (нет MAX_TOKEN)")
        return None
    t = (text or "").strip()
    tl = t.lower()
    key = str(chat_id) if chat_id is not None else str(user_id)

    # --- 1) Состояние «ждём класс для дня» (пользователь выбирает класс после «Сегодня/Завтра/день») ---
    if key in pending_day and is_likely_class(t):
        target_date = pending_day.pop(key)
        class_name = normalize_class_name(t)
        set_user_class(key, class_name)  # запоминаем класс
        reply = format_schedule(class_name, target_date)
        return bot.send_message_with_keyboard(chat_id, reply, build_menu_buttons(), user_id=user_id)

    # --- 2) Кнопка/команда «Сменить класс»: забываем запомненный класс и показываем выбор ---
    if tl in ("сменить класс", "сменить", "выбрать класс", "сменитькласс"):
        forget_user_class(key)
        pending_day.pop(key, None)
        msg = "Хорошо! Выберите новый класс:"
        return bot.send_message_with_keyboard(chat_id, msg, _class_buttons_only(), user_id=user_id)

    # --- 3) Простое приветствие/меню ---
    if tl in ("/start", "привет", "здравствуй", "начать", "старт", "помощь", "/help", "меню"):
        saved = get_user_class(key)
        extra = f" (сейчас выбран {saved})" if saved else ""
        reply = (
            "Привет! Я бот расписания 🎒\n"
            f"Выберите класс на кнопках, и я покажу расписание на сегодня.{extra}\n"
            "Можно также спросить «расписание на завтра» или «на понедельник»."
        )
        return bot.send_message_with_keyboard(chat_id, reply, build_menu_buttons(), user_id=user_id)

    # --- 4) Запрос дня (сегодня/завтра/понедельник...) ---
    target_date = _resolve_target_date(tl)
    if target_date is not None:
        saved = get_user_class(key)
        if saved:
            # класс уже выбран — сразу показываем расписание
            reply = format_schedule(saved, target_date)
            return bot.send_message_with_keyboard(chat_id, reply, build_menu_buttons(), user_id=user_id)
        # класса ещё нет — просим выбрать
        pending_day[key] = target_date
        msg = f"Выберите класс на {DAYS[target_date.weekday()]} ({target_date:%d.%m}):"
        return bot.send_message_with_keyboard(chat_id, msg, _class_buttons_only(), user_id=user_id)

    # --- 5) Пользователь написал класс (5А, 9-б, 10А и т.п.) ---
    if is_likely_class(t):
        class_name = normalize_class_name(t)
        set_user_class(key, class_name)  # запоминаем класс
        return _answer_for_day(chat_id, user_id, today_date(), requested_class=class_name)

    # --- 6) Общий запрос: если класс уже есть — показать расписание на сегодня ---
    saved = get_user_class(key)
    if saved:
        reply = format_schedule(saved, today_date())
        return bot.send_message_with_keyboard(chat_id, reply, build_menu_buttons(), user_id=user_id)

    # --- 7) Не распознали ---
    classes = parser.get_available_classes() if parser else []
    hint = ""
    if classes:
        hint = "\nДоступные классы: " + ", ".join(classes[:8])
    reply = "Не понял запрос 🤔 Просто напишите класс, например 5А." + hint
    return bot.send_message_with_keyboard(chat_id, reply, build_menu_buttons(), user_id=user_id)


def _class_buttons_only() -> list[list[dict]]:
    """Кнопки только с классами (без действий) для выбора при конкретном дне."""
    classes = parser.get_available_classes() if parser else []
    buttons: list[list[dict]] = []
    row: list[dict] = []
    for cls in classes:
        row.append({"type": "message_button", "text": cls})
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return buttons


def _next_weekday(weekday: int, start: date) -> date:
    """Ближайшая дата с днём недели weekday (0=пн), не раньше start."""
    delta = (weekday - start.weekday()) % 7
    if delta == 0:
        delta = 7  # ближайший такой же день в будущем
    return start + timedelta(days=delta)


def _answer_for_day(chat_id, user_id, target_date: date, requested_class=None, close_day=None):
    """Собирает ответ для даты. Если класс непонятен — спросит."""
    if not bot:
        return None
    # Если класс не передан — спросим какой.
    if requested_class is None:
        bot.send_message_with_keyboard(
            chat_id,
            "Для какого класса показать расписание? Например: 5А",
            build_menu_buttons(),
            user_id=user_id,
        )
        return None
    reply = format_schedule(requested_class, target_date)
    return bot.send_message_with_keyboard(chat_id, reply, build_menu_buttons(), user_id=user_id)


# --- Webhook ---
@app.route("/max/webhook", methods=["POST"])
def webhook():
    if not validate_webhook_request(request.headers, MAX_WEBHOOK_SECRET):
        log.warning("Неверный секрет webhook")
        return jsonify({"ok": False, "error": "invalid secret"}), 401

    # MAX присылает один объект Update на запрос (не массив).
    data = request.get_json(silent=True) or {}
    # На случай обоих форматов — принимаем и массив (на всякий случай), и одиночный объект.
    updates = data.get("updates") if isinstance(data.get("updates"), list) else [data]

    for upd in updates:
        # Обрабатываем только события создания сообщения.
        if upd.get("update_type") not in (None, "message_created"):
            continue
        msg = upd.get("message") or {}
        recipient = msg.get("recipient") or {}
        sender = msg.get("sender") or {}
        body = msg.get("body") or {}
        text = (body.get("text") or "").strip()

        chat_id = recipient.get("chat_id")
        # user_id — отправитель для лички.
        user_id = sender.get("user_id") if not sender.get("is_bot") else None
        # Если sender нет — используем recipient.user_id (для диалогов).
        if user_id is None:
            user_id = recipient.get("user_id")

        # Для ответа: в личку нужен user_id, в группу/канал — chat_id.
        chat_type = str(recipient.get("chat_type") or "").lower()
        if chat_type in ("chat", "channel"):
            active_chat = chat_id
            active_user = None
        else:
            active_chat = None if user_id else chat_id
            active_user = user_id if user_id is not None else None

        if text and (active_user is not None or active_chat is not None):
            try:
                handle_message(active_chat, active_user or user_id, text)
            except Exception:
                log.exception("Ошибка обработки сообщения")
                try:
                    bot.send_message(active_chat, "Произошла ошибка. Попробуйте ещё раз.",
                                     user_id=active_user)
                except Exception:
                    pass
    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "parser_ready": parser is not None})


@app.route("/refresh", methods=["POST"])
def refresh():
    """Принудительно сбрасывает кэш расписания (новые листы появятся сразу)."""
    if parser is None:
        return jsonify({"ok": False, "error": "parser not ready"}), 500
    parser.invalidate_cache()
    return jsonify({"ok": True})


@app.route("/subscribe", methods=["POST"])
def subscribe():
    """Разовый эндпоинт для регистрации webhook-подписки."""
    if not bot:
        return jsonify({"ok": False, "error": "bot not configured"}), 500
    url = os.getenv("MAX_WEBHOOK_URL")
    if not url:
        return jsonify({"ok": False, "error": "MAX_WEBHOOK_URL not set"}), 400
    res = bot.subscribe_webhook(url, MAX_WEBHOOK_SECRET)
    return jsonify(res)


if __name__ == "__main__":
    # В dev-режиме (без TLS). В продакшене запускайте через gunicorn/reverse-proxy.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=True)