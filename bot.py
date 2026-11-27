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
import re
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
SCHEDULE_FILE_URL = os.getenv("SCHEDULE_FILE_URL", "")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Yekaterinburg")
# Ссылка на школьный канал (кнопка в главном меню). Можно переопределить через env.
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://max.ru/id7203543799_gos")

bot = MaxBotClient(
    MAX_TOKEN,
    verify_tls=os.getenv("MAX_VERIFY_TLS", "").lower() in ("1", "true", "yes"),
) if MAX_TOKEN else None
parser: ScheduleParser | None = None
try:
    if SCHEDULE_FILE_URL:
        # cache_ttl=60: изменения в таблице подтягиваются максимум за минуту
        # (свежее расписание важнее, чем экономия на перекачивании книги).
        parser = ScheduleParser(SCHEDULE_FILE_URL, timezone=TIMEZONE, cache_ttl=60)
except Exception as exc:  # на старте не валимся, если файл ещё не готов
    log.warning("Не удалось инициализировать парсер: %s", exc)

# Соответствие индекса дня (0=пн) названию листа в книге.
# Листы идём по порядку книги.
DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def today_date() -> date:
    """Сегодняшняя дата по часовому поясу школы."""
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def build_menu_buttons() -> list[list[dict]]:
    """Собирает массив кнопок главного меню (действия + параллели).

    Тип message имитирует ввод текста — при нажатии пользователь
    "печатает" подпись кнопки. Это удобно: выбор класса/дня идёт через ту же
    текстовую логику, не требуя ответа на callback.
    """
    buttons: list[list[dict]] = []

    # Ряд со ссылкой на школьный канал (над действиями)
    buttons.append([
        {"type": "link", "text": "📣 Наш канал", "url": CHANNEL_URL},
    ])

    # Ряд с действиями
    actions = [
        {"type": "message", "text": "Сегодня"},
        {"type": "message", "text": "Завтра"},
        {"type": "message", "text": "Изменить класс"},
        {"type": "message", "text": "Помощь"},
    ]
    buttons.append(actions)

    # Параллели (5-е, 6-е, ...) — первый шаг выбора класса
    groups = _grouped_classes()
    row: list[dict] = []
    for k in groups:
        row.append({"type": "message", "text": f"{k}-е"})
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
            f"На {day.day_name} ({target:%d.%m}) расписания для {class_name} пока нет.\n"
            "Возможно, оно ещё не добавлено — попробуйте зайти чуть позже."
        )
    lines = [f"📅 Расписание {class_name} — {day.day_name} {target:%d.%m.%Y}:"]
    for i, lesson in enumerate(day.lessons, start=1):
        num = lesson.period or str(i)
        subj = lesson.subject
        mark = " ⚠️" if lesson.is_change else ""
        room = f" — каб. {lesson.room}" if lesson.room else ""
        lines.append(f"{num}. {subj}{mark}{room}")
    return "\n".join(lines)


def is_likely_class(text: str) -> bool:
    """Похоже ли сообщение на название класса (5А, 5-А, 9 б, 10А).

    Чистое число («1», «11») классом не считается — иначе бот собирает
    расписание по частичному совпадению цифры с параллелью.
    """
    normalized = normalize_class_name(text)
    return bool(re.match(r"\d{1,2}-?[А-ЯЁA-Z]", normalized))


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
    if "сегодня" in tl:
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


def weekend_message(target_date: date, tl: str) -> str | None:
    """Сообщение «выходной» для субботы/воскресенья, иначе None."""
    if target_date.weekday() < 5:
        return None
    if "сегодня" in tl:
        when = "Сегодня"
    elif "завтра" in tl:
        when = "Завтра"
    else:
        when = DAYS[target_date.weekday()]
    return (
        f"{when} ({target_date:%d.%m}) — выходной день.\n"
        "Хотите посмотреть расписание на понедельник?"
    )


def weekend_buttons() -> list[list[dict]]:
    """Кнопки к сообщению «выходной»: предложение понедельника + меню."""
    return [[{"type": "message", "text": "Понедельник"}], *build_menu_buttons()]


def handle_message(chat_id, user_id, text: str):
    """Главная логика: что ответить пользователю."""
    if not bot:
        log.error("Бот не инициализирован (нет MAX_TOKEN)")
        return None
    t = (text or "").strip()
    tl = t.lower()
    key = str(chat_id) if chat_id is not None else str(user_id)

    # --- 1) Состояние «ждём класс для дня» (пользователь выбирает класс после «Сегодня/Завтра/день») ---
    # «5-е» сюда не попадает: это выбор параллели, а не класс.
    if key in pending_day and is_likely_class(t) and not re.fullmatch(r"\s*\d{1,2}\s*-\s*е\s*", tl):
        target_date = pending_day.pop(key)
        class_name = normalize_class_name(t)
        set_user_class(key, class_name)  # запоминаем класс
        reply = format_schedule(class_name, target_date)
        return bot.send_message_with_keyboard(chat_id, reply, build_menu_buttons(), user_id=user_id)

    # --- 2) Кнопка/команда «Изменить класс»: забываем запомненный класс и показываем выбор ---
    if tl in ("изменить класс", "сменить класс", "изменить", "сменить", "выбрать класс",
              "изменитькласс", "сменитькласс"):
        forget_user_class(key)
        pending_day.pop(key, None)
        msg = "Хорошо! Выберите параллель, затем класс:"
        return bot.send_message_with_keyboard(chat_id, msg, build_menu_buttons(), user_id=user_id)

    # --- 2.5) Выбор параллели («5-е», «6-е»...) — показать классы параллели ---
    # Дефис обязателен, чтобы текст «5е» (класс 5Е) не спутался с «5-е».
    mpar = re.fullmatch(r"\s*(\d{1,2})\s*-\s*е\s*", tl)
    if mpar:
        par = mpar.group(1)
        classes = _classes_of_parallel(par)
        if not classes:
            msg = f"Параллель {par}-х не найдена в расписании."
            return bot.send_message_with_keyboard(chat_id, msg, build_menu_buttons(), user_id=user_id)
        # после выбора параллели показываем классы; дата для них уже в pending_day
        # и применится в блоке 1 после выбора конкретного класса.
        msg = f"Выберите класс из {par}-х:"
        return bot.send_message_with_keyboard(chat_id, msg, _class_buttons_for_parallel(par), user_id=user_id)

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
        wm = weekend_message(target_date, tl)
        if wm:
            return bot.send_message_with_keyboard(chat_id, wm, weekend_buttons(), user_id=user_id)
        saved = get_user_class(key)
        if saved:
            # класс уже выбран — сразу показываем расписание
            reply = format_schedule(saved, target_date)
            return bot.send_message_with_keyboard(chat_id, reply, build_menu_buttons(), user_id=user_id)
        # класса ещё нет — просим выбрать (двухступенчато: параллель → класс)
        pending_day[key] = target_date
        msg = f"Выберите параллель на {DAYS[target_date.weekday()]} ({target_date:%d.%m}):"
        return bot.send_message_with_keyboard(chat_id, msg, build_menu_buttons(), user_id=user_id)

    # --- 5) Пользователь написал класс (5А, 9-б, 10А и т.п.) ---
    if is_likely_class(t):
        class_name = normalize_class_name(t)
        set_user_class(key, class_name)  # запоминаем класс
        wm = weekend_message(today_date(), tl)
        if wm:
            return bot.send_message_with_keyboard(chat_id, wm, weekend_buttons(), user_id=user_id)
        return _answer_for_day(chat_id, user_id, today_date(), requested_class=class_name)

    # --- 6) Общий запрос: если класс уже есть — показать расписание на сегодня ---
    saved = get_user_class(key)
    if saved:
        wm = weekend_message(today_date(), tl)
        if wm:
            return bot.send_message_with_keyboard(chat_id, wm, weekend_buttons(), user_id=user_id)
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
        row.append({"type": "message", "text": cls})
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return buttons


def _parallel_of(class_name: str) -> str | None:
    """Возвращает параллель класса (первая цифра) как строку, или None."""
    m = re.match(r"\s*(\d{1,2})", class_name or "")
    return m.group(1) if m else None


def _grouped_classes() -> dict[str, list[str]]:
    """Классы, сгруппированные по параллели (5, 6, 7 ...). Порядок цифровой."""
    classes = parser.get_available_classes() if parser else []
    groups: dict[str, list[str]] = {}
    for cls in classes:
        par = _parallel_of(cls)
        if par is None:
            continue
        groups.setdefault(par, [])
        if cls not in groups[par]:
            groups[par].append(cls)
    # сортируем ключи по числу
    ordered = sorted(groups.keys(), key=lambda x: int(x))
    return {k: groups[k] for k in ordered}


def _parallel_buttons() -> list[list[dict]]:
    """Кнопки параллелей («5-е», «6-е» ...) — первый уровень выбора."""
    groups = _grouped_classes()
    buttons: list[list[dict]] = []
    row: list[dict] = []
    for k in groups:
        label = f"{k}-е" if int(k) <= 9 else f"{k}-е"
        row.append({"type": "message", "text": label})
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return buttons


def _classes_of_parallel(par: str) -> list[str]:
    """Возвращает отсортированный список классов конкретной параллели."""
    return _grouped_classes().get(str(par), [])


def _class_buttons_for_parallel(par: str) -> list[list[dict]]:
    """Кнопки классов одной параллели (второй уровень выбора)."""
    classes = _classes_of_parallel(par)
    buttons: list[list[dict]] = []
    row: list[dict] = []
    for cls in classes:
        row.append({"type": "message", "text": cls})
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

    log.info("Webhook получил событие: %s", json.dumps(data, ensure_ascii=False)[:2000])

    for upd in updates:
        utype = upd.get("update_type")
        log.info("Обрабатываю update_type=%s, update_keys=%s", utype, sorted(upd.keys()))
        # Обрабатываем текстовые сообщения и событие запуска бота («Старт»).
        if utype not in (None, "message_created", "bot_started"):
            log.info("Пропускаю события типа %s", utype)
            continue
        msg = upd.get("message") or {}
        recipient = msg.get("recipient") or {}
        sender = msg.get("sender") or {}
        body = msg.get("body") or {}
        text = (body.get("text") or "").strip()
        # Событие bot_started — пользователь нажал «Старт» / открыл бота.
        # Текста обычно нет, поэтому превращаем его в команду «/start».
        if utype == "bot_started" and not text:
            text = "/start"

        chat_id = recipient.get("chat_id")
        # user_id — отправитель для лички.
        user_id = sender.get("user_id") if not sender.get("is_bot") else None
        # Если sender нет — используем recipient.user_id (для диалогов).
        if user_id is None:
            user_id = recipient.get("user_id")
        # События без вложенного message (bot_started/bot_stopped/bot_added и др.)
        # несут chat_id и user на верхнем уровне Update. Подтягиваем их как fallback.
        if chat_id is None:
            chat_id = upd.get("chat_id")
        if user_id is None:
            top_user = upd.get("user") or {}
            user_id = top_user.get("user_id")

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
    debug = os.getenv("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    # threaded=True: Werkzeug-сервер иначе обрабатывает запросы последовательно
    # (однопоточно), что превращает параллельные webhook-события в очередь.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=debug, threaded=True)