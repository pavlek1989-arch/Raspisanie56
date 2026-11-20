# -*- coding: utf-8 -*-
"""Проверка парсера на реальном скачивании файла расписания (.xlsx)."""
import sys
from datetime import date

from schedule_parser import ScheduleParser

FILE_URL = "https://disk.yandex.ru/i/-yedy5QwZvabPQ"
parser = ScheduleParser(FILE_URL, timezone="Asia/Yekaterinburg")

print("days:", parser._get_sheet_names())
print("classes (первый лист-дата):", parser.get_available_classes())

for cls in ("5А", "9А", "7А", "8А", "10А"):
    sched = parser.get_schedule_for_date(cls, date(2026, 11, 18))
    print(f"\n=== {cls} на 18.11.2026 ===")
    for l in sched.lessons:
        print(f"  {l.period}. {l.subject}  [каб: {l.room}]")