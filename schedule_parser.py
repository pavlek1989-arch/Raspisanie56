"""Парсер расписания из Google-таблицы (публичный доступ по ссылке, .xlsx).

Бот НЕ использует сервисный аккаунт и Google Sheets API. Он скачивает
всю таблицу (книгу) как .xlsx по публичной ссылке:

    https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/export?format=xlsx

и разбирает все листы локально (через openpyxl).

Требование: для ID=... в .env таблица должна быть открыта «по ссылке —
Читатель».

Реальная структура листа-даты (например "18.11.2026"):
  - Строка 1 (индекс 0): титулы блоков параллелей по колонкам.
  - Строка 3 (индекс 2): строка-заголовок классов. Колонки классов,
    разделители "№ п/п". Блоки:
        A='№ п/п', B..V  = 5А-5Х
        W='№ п/п', X..AM = 6Р,6С,7Н,8З,8И,9А-9Л
        AN             = 10-11 классы (10А инф, 10А физ, ...)
        AZ='№ п/п', BA..BP = 6А-6Т
        BR             = 7А-7П  (сдвинутый блок)
        CG             = 8А-8Н  (сдвинутый блок)
  - Дальше данные идут ПАРАМИ строк (слот урока):
        слот1: строки (4,5), слот2: (6,7), ... — предмет, затем кабинет.
      Номер урока — из маркерной колонки блока (если есть) либо порядковый.

Время в предметах (например "0.\n8.00-8.40") игнорируется: выводятся
только номера уроков, предметы и кабинеты.
"""

from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass
from datetime import datetime

import requests
from openpyxl import load_workbook


@dataclass
class Lesson:
    period: str  # номер урока ("1", "2", ...)
    subject: str  # название предмета
    room: str  # кабинет ("" если нет)


@dataclass
class DaySchedule:
    day_name: str  # название дня недели
    lessons: list[Lesson]


# Паттерн названия класса: цифры + кириллическая/латинская литера (5А, 9Б, 10В...)
CLASS_RE = re.compile(r"^\s*(\d{1,2})\s*([А-ЯЁA-Z])")
# Паттерн кабинета: цифры/слэш или зал/библиотека/каб
ROOM_RE = re.compile(
    r"^\s*(\d{1,4}(\s*[/\\]\s*\d{1,4})*|спортзал|актовый\s*зал|библиотека|каб\.?\s*\d)",
    re.IGNORECASE,
)
# Паттерн "№ п/п" (маркер начала блока)
NUMP_RE = re.compile(r"№\s*п/?п")
# Паттерн, отсекающий время-ячейки (0.\n8.00-8.40 и т.п.)
TIME_RE = re.compile(r"\d{1,2}\s*[.\n-]\s*\d{1,2}[:.]\s*\d{2}")

EXPORT_URL = "https://docs.google.com/spreadsheets/d/{id}/export?format=xlsx"


def normalize_class_name(text: str) -> str:
    """Приводит ввод пользователя к виду '5А' / '9Б' / '10А'.

    Принимает: '5А', '5-А', '5 а', '5а', '5 "А"', '9б'. Отбрасывает
    случайные символы после литеры (для составных '10А инф' вернёт '10А').
    """
    if not text:
        return ""
    s = text.strip().replace("\u00a0", " ").upper().replace("Ё", "Е")
    m = CLASS_RE.match(s)
    if m:
        return m.group(1) + m.group(2)
    m = re.fullmatch(r"\s*(\d{1,2})\s*", s)
    if m:
        return m.group(1)
    return s


class ScheduleParser:
    """Читает расписание, скачивая книгу .xlsx по публичной ссылке."""

    def __init__(self, spreadsheet_id: str, timezone: str = "Asia/Yekaterinburg",
                 cache_ttl: float = 300.0, timeout: float = 30.0):
        from zoneinfo import ZoneInfo

        self.spreadsheet_id = spreadsheet_id
        self.tz = ZoneInfo(timezone)
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self.session = requests.Session()
        self._book = None  # openpyxl.Workbook
        self._book_ts = 0.0
        self._sheet_names: list[str] | None = None
        # кэш: (имя листа) -> list[list]  (сетка значений)
        self._sheets_data: dict[str, list[list]] = {}

    def _download_book(self) -> None:
        """Скачивает книгу целиком (все листы) и сохраняет объект Workbook."""
        url = EXPORT_URL.format(id=self.spreadsheet_id)
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        self._book = load_workbook(io.BytesIO(resp.content), data_only=True, read_only=False)
        self._book_ts = time.monotonic()
        self._sheet_names = None
        self._sheets_data.clear()

    def _ensure_book(self) -> None:
        if self._book is None or time.monotonic() - self._book_ts >= self.cache_ttl:
            self._download_book()

    def _get_sheet_names(self) -> list[str]:
        self._ensure_book()
        if self._sheet_names is None:
            self._sheet_names = self._book.sheetnames
        return self._sheet_names

    def _sheet_rows(self, sheet_title: str) -> list[list]:
        """Возвращает сетку строк листа (свежую в пределах кэша)."""
        self._ensure_book()
        if sheet_title not in self._sheets_data:
            ws = self._book[sheet_title] if sheet_title in self._book.sheetnames else None
            data: list[list] = []
            if ws is not None:
                for row in ws.iter_rows(values_only=True):
                    data.append([("" if c is None else c) for c in row])
            self._sheets_data[sheet_title] = data
        return self._sheets_data[sheet_title]

    # --- работа с датами листов ---
    def _sheet_to_date(self, title: str) -> "datetime.date | None":
        name = str(title).strip()
        for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m"):
            try:
                return datetime.strptime(name, fmt).date()
            except ValueError:
                continue
        return None

    def find_sheet_for_date(self, target: "datetime.date") -> str | None:
        try:
            for s in self._get_sheet_names():
                if self._sheet_to_date(s) == target:
                    return s
        except requests.RequestException:
            return None
        return None

    def invalidate_cache(self) -> None:
        """Сбрасывает кэш — в следующий запрос книгу перекачают заново."""
        self._book = None
        self._book_ts = 0.0
        self._sheet_names = None
        self._sheets_data.clear()

    def get_available_classes(self, day_sheet: str | None = None) -> list[str]:
        try:
            target = day_sheet
            if target is None:
                for s in self._get_sheet_names():
                    if self._sheet_to_date(s) is not None:
                        target = s
                        break
                if target is None and self._get_sheet_names():
                    target = self._get_sheet_names()[0]
            if not target:
                return []
            rows = self._sheet_rows(target)
            header_row = self._find_header_row(rows)
            if header_row is None:
                return []
            headers = rows[header_row]
            classes = []
            for cell in headers:
                name = self._normalize_header_class(cell)
                if name:
                    classes.append(name)
            return classes
        except requests.RequestException:
            return []

    def _find_header_row(self, rows: list[list]) -> int | None:
        for i, row in enumerate(rows):
            text_cols = [str(c).strip() for c in row if c not in (None, "")]
            has_num = any(NUMP_RE.search(c) for c in text_cols)
            class_count = sum(1 for c in text_cols if CLASS_RE.match(c))
            if has_num and class_count >= 2:
                return i
        return None

    def _normalize_header_class(self, cell) -> str:
        if cell is None:
            return ""
        s = str(cell).strip()
        if not s or NUMP_RE.search(s):
            return ""
        if not CLASS_RE.match(s):
            return ""
        return s

    def get_schedule_for_date(self, class_name: str, date_value: "datetime.date") -> DaySchedule:
        day_name = self._weekday_name(date_value.weekday())
        try:
            sheet_title = self.find_sheet_for_date(date_value)
            if sheet_title is None:
                return DaySchedule(day_name=day_name, lessons=[])
            return self._parse_sheet_schedule(sheet_title, class_name, day_name)
        except requests.RequestException:
            return DaySchedule(day_name=day_name, lessons=[])

    def _parse_sheet_schedule(self, sheet_title: str, class_name: str, day_name: str) -> DaySchedule:
        rows = self._sheet_rows(sheet_title)
        if not rows:
            return DaySchedule(day_name=day_name, lessons=[])
        header_idx = self._find_header_row(rows)
        if header_idx is None:
            return DaySchedule(day_name=day_name, lessons=[])
        header = rows[header_idx]

        want = class_name.strip().upper()
        class_col = None
        for i, cell in enumerate(header):
            h = self._normalize_header_class(cell)
            if not h:
                continue
            if h.upper() == want:
                class_col = i
                break
        if class_col is None:
            # частичное совпадение (10А -> 10А инф / 10А физ)
            for i, cell in enumerate(header):
                h = self._normalize_header_class(cell)
                if h and h.upper().startswith(want):
                    class_col = i
                    break
        if class_col is None:
            return DaySchedule(day_name=day_name, lessons=[])

        # маркерная колонка номера урока для этого класса
        marker_col: int | None = None
        for j in range(class_col, -1, -1):
            if j >= len(header) or header[j] is None:
                continue
            if NUMP_RE.search(str(header[j]).strip()):
                marker_col = j
                break
            if CLASS_RE.match(self._normalize_header_class(header[j])):
                break

        lessons: list[Lesson] = []
        slot = 1
        k = header_idx + 1
        n = len(rows)
        while k < n:
            subj_row = rows[k]
            cab_row = rows[k + 1] if k + 1 < n else []
            subj = subj_row[class_col] if class_col < len(subj_row) else None
            if subj is None or str(subj).strip() == "":
                k += 2
                slot += 1
                continue
            subj_text = str(subj).strip()
            if TIME_RE.search(subj_text) and not CLASS_RE.match(subj_text):
                k += 2
                slot += 1
                continue
            period = str(slot)
            if marker_col is not None:
                mv = subj_row[marker_col] if marker_col < len(subj_row) else None
                if mv is not None and str(mv).strip() and not TIME_RE.search(str(mv)):
                    m2 = re.match(r"(\d{1,2})", str(mv).strip())
                    if m2:
                        period = m2.group(1)
            room = ""
            if cab_row and class_col < len(cab_row) and cab_row[class_col] is not None:
                r = str(cab_row[class_col]).strip()
                if r and (ROOM_RE.match(r) or not re.fullmatch(r"\d{1,2}\.?", r)):
                    room = r
            lessons.append(Lesson(period=period, subject=subj_text, room=room))
            k += 2
            slot += 1
        return DaySchedule(day_name=day_name, lessons=lessons)

    @staticmethod
    def _weekday_name(weekday: int) -> str:
        names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        return names[weekday] if 0 <= weekday < 7 else ""