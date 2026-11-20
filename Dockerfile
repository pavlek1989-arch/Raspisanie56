FROM python:3.12-slim

WORKDIR /app

# Устанавливаем систему: корневые SSL-сертификаты (нужны для HTTPS-запросов к MAX API)
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

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