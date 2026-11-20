FROM python:3.12-slim

WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY bot.py max_client.py schedule_parser.py ./

# Порт, который слушает приложение (должен совпадать с PORT из окружения)
ENV PORT=8000
EXPOSE 8000

# Запуск бота
CMD ["python", "bot.py"]