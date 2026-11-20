# -*- coding: utf-8 -*-
"""Проверка парсера на реальном скачивании таблицы по публичной ссылке."""
import sys
from datetime import date

sys.path.insert(0, r"C:\Users\Учитель_2\Documents\MultiTool\HomeChats\Chat-2")

from schedule_parser import ScheduleParser

SPREADSHEET_ID = "1ryEIYma633BhbZhy2sgNs-WsRfRiObal"
parser = ScheduleParser(SPREADSHEET_ID, timezone="Asia/Yekaterinburg")

print("days:", parser._get_sheet_names())
print("classes (первый лист-дата):", parser.get_available_classes())

for cls in ("5А", "9А", "7А", "8А", "10А"):
    sched = parser.get_schedule_for_date(cls, date(2026, 11, 18))
    print(f"\n=== {cls} на 18.11.2026 ===")
    for l in sched.lessons:
        print(f"  {l.period}. {l.subject}  [каб: {l.room}]")