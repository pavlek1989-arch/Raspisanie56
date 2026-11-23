"""Парсер расписания из файла .xlsx по публичной ссылке (например Яндекс.Диск).

Бот НЕ использует ни сервисный аккаунт, ни Google Sheets API. Он скачивает
книгу .xlsx целиком по публичной ссылке и разбирает все листы локально
(через openpyxl). Если ссылка с Яндекс.Диска (disk.yandex.ru), парсер сам
получает прямой URL скачивания через публичный API.

Переменная окружения: SCHEDULE_FILE_URL = <публичная ссылка на .xlsx>.

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
import zipfile
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


def _sanitize_styles_xml(xml_text: str) -> str:
    """Чистит styles.xml от нестандартных атрибутов границ.

    Некоторые редакторы (напр. Яндекс.Документы) пишут в теги границ
    <left>/<right>/<top>/<bottom> недопустимый style='solid' (это тип заливки,
    а не границы). openpyxl на этом падает с 'could not read stylesheet'.
    Убираем style= у тегов границ (значения ячеек от этого не зависят)."""
    return re.sub(
        r'<(s:)?(left|right|top|bottom|diagonal)\b[^>]*>',
        lambda m: re.sub(r'\s+style="[^"]*"', '', m.group(0)),
        xml_text,
    )


def _load_workbook_robust(content: bytes):
    """Загружает xlsx, устойчиво к битым стилям (нестандартные границы).

    Сначала пробуем обычный load_workbook. При ошибке чтения стилей
    пересобираем архив с очищенными styles.xml и пробуем ещё раз.
    """
    try:
        return load_workbook(io.BytesIO(content), data_only=True, read_only=False)
    except ValueError:
        pass
    # Пересобираем zip, заменяя повреждённые XML стилей.
    try:
        src = zipfile.ZipFile(io.BytesIO(content))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename.endswith('styles.xml'):
                    data = _sanitize_styles_xml(data.decode('utf-8', 'replace')).encode('utf-8')
                dst.writestr(item, data)
        return load_workbook(io.BytesIO(buf.getvalue()), data_only=True, read_only=False)
    except Exception:
        # Если не помогло — отдаём исходную ошибку (обычная картина для битого файла).
        return load_workbook(io.BytesIO(content), data_only=True, read_only=False)


class ScheduleParser:
    """Читает расписание, скачивая книгу .xlsx по публичной ссылке."""

    def __init__(self, file_url: str, timezone: str = "Asia/Yekaterinburg",
                 cache_ttl: float = 300.0, timeout: float = 60.0):
        from zoneinfo import ZoneInfo

        self.file_url = file_url
        self.tz = ZoneInfo(timezone)
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self.session = requests.Session()
        self._book = None  # openpyxl.Workbook
        self._book_ts = 0.0
        self._sheet_names: list[str] | None = None
        # кэш: (имя листа) -> list[list]  (сетка значений)
        self._sheets_data: dict[str, list[list]] = {}

    def _resolve_download_url(self) -> str:
        """Превращает публичную ссылку в прямой URL скачивания файла.

        Если ссылка уже прямая (содержит downloader.disk.yandex.ru или не
        disk.yandex.ru) — возвращаем как есть. Для публичной ссылки Яндекс.Диска
        вида https://disk.yandex.ru/… запрашиваем href через публичный API,
        чтобы подпись всегда была свежей.
        """
        if "downloader.disk.yandex.ru" in (self.file_url or "") or "disk.yandex.ru" not in (self.file_url or ""):
            return self.file_url
        api = f"https://cloud-api.yandex.net/v1/disk/public/resources/download?public_key={self.file_url}"
        resp = self.session.get(api, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        href = data.get("href")
        if not href:
            raise requests.RequestException("Не удалось получить ссылку на скачивание из Яндекс.Диска")
        return href

    def _download_book(self) -> None:
        """Скачивает книгу .xlsx по публичной ссылке (Яндекс.Диск/прямой URL)."""
        url = self._resolve_download_url()
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        if not resp.content:
            raise requests.RequestException("Пустой ответ при скачивании файла расписания")
        self._book = _load_workbook_robust(resp.content)
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

        # Базовая колонка для подгруппы (вариант 2): если у класса есть суффикс
        # подгруппы («10А физ», «10А инф»), базой служит соседняя колонка такой же
        # параллели со «сплошным» расписанием. По ней заполняются общие уроки,
        # которых нет в своей колонке. Пример: 10А физ = своя колонка + «10А инф».
        base_class_col: int | None = None
        base_prefix = None
        base_m = re.match(r"^\d{1,2}[А-ЯЁA-Z]+", want)
        if base_m:
            base_prefix = base_m.group(0)
            # ищем соседнюю подгруппу с тем же префиксом (ближайшую слева, не равную текущей)
            for i in range(class_col - 1, -1, -1):
                hh = header[i] if i < len(header) else None
                hn = self._normalize_header_class(hh)
                if not hn:
                    continue
                if hn.upper() == want:
                    continue
                mm = re.match(r"^\d{1,2}[А-ЯЁA-Z]+", hn.upper())
                if mm and mm.group(0) == base_prefix:
                    base_class_col = i
                    break

        # маркерная колонка номера урока для этого класса.
        # Это ближайшая слева колонка с числами-номерами уроков в слотах ниже
        # шапки, но в ТОМ ЖЕ блоке, что и класс (не перескакивая через другие
        # классы). У блоков 10-11 и 6-7 нумерация в отдельной колонке (есть «0»).
        marker_col: int | None = None
        # Выберем колонку-маркер номера урока для этого класса.
        # Маркером считаем колонку, где в слотах под шапкой стоят числа
        # (номера уроков, включая "0.\n8.00" для 0-урока). Нужная колонка —
        # ближайшая слева от class_col, не являющаяся классом; допускаем, что
        # в группах 10-11 между маркером и классом стоят другие классы параллели.
        def _col_has_lesson_numbers(j: int) -> bool:
            if j >= len(rows[min(header_idx + 1, len(rows) - 1)]):
                return False
            count = 0
            k = header_idx + 1
            while k < len(rows) and k - (header_idx + 1) < 16:
                v = rows[k][j] if j < len(rows[k]) else None
                if v is not None:
                    # первая цифра (обрабатывает "1\n8.50" и "0.\n8.00" как номер)
                    if re.match(r"^\s*\d{1,2}\.?", str(v)):
                        count += 1
                k += 2
            return count >= 2

        # Идём влево от class_col, сначала пропуская классы той же параллели,
        # затем ищем первую числовую колонку.
        for j in range(class_col - 1, -1, -1):
            hdrj = header[j] if j < len(header) else None
            if CLASS_RE.match(self._normalize_header_class(hdrj)):
                continue  # другой класс — просто пропускаем, маркер может быть левее
            if _col_has_lesson_numbers(j):
                marker_col = j
                break
        # Если в самом начале листа (например 5А): берём первую числовую колонку слева
        if marker_col is None:
            for j in range(class_col - 1, -1, -1):
                if _col_has_lesson_numbers(j):
                    marker_col = j
                    break

        lessons: list[Lesson] = []
        slot = 1
        prev_num = None  # последний выданный номер урока (для пустых маркеров)
        k = header_idx + 1
        n = len(rows)
        while k < n:
            subj_row = rows[k]
            cab_row = rows[k + 1] if k + 1 < n else []
            subj = subj_row[class_col] if class_col < len(subj_row) else None
            # подгруппа: если своя ячейка пуста, берём общий предмет из базовой колонки
            if (subj is None or str(subj).strip() == "") and base_class_col is not None:
                b = subj_row[base_class_col] if base_class_col < len(subj_row) else None
                if b is not None and str(b).strip():
                    subj = b
                    base_used = True
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
                # номер урока — первые цифры исходного значения; так "1\n8.50"
                # даёт 1, а "0.\n8.00" — 0.
                m2 = re.match(r"^\s*(\d{1,2})", str(mv)) if mv is not None else None
                if m2:
                    period = m2.group(1)
                else:
                    # пустой маркер: продолжаем нумерацию после предыдущего урока
                    # (первый пустой слот становится 0 — «0-урок» у 6-7 и 10-11).
                    period = str(prev_num + 1 if prev_num is not None else 0)
            try:
                prev_num = int(period)
            except ValueError:
                prev_num = None
            room = ""
            if cab_row and class_col < len(cab_row) and cab_row[class_col] is not None:
                r = str(cab_row[class_col]).strip()
                if r and (ROOM_RE.match(r) or not re.fullmatch(r"\d{1,2}\.?", r)):
                    room = r
            # подгруппа: если кабинета нет — пробуем из базовой колонки
            if not room and base_class_col is not None and cab_row and base_class_col < len(cab_row):
                rb = str(cab_row[base_class_col]).strip() if cab_row[base_class_col] is not None else ""
                if rb and (ROOM_RE.match(rb) or not re.fullmatch(r"\d{1,2}\.?", rb)):
                    room = rb
            lessons.append(Lesson(period=period, subject=subj_text, room=room))
            k += 2
            slot += 1
        return DaySchedule(day_name=day_name, lessons=lessons)

    @staticmethod
    def _weekday_name(weekday: int) -> str:
        names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        return names[weekday] if 0 <= weekday < 7 else ""