FROM python:3.12-slim

# Отключаем буферизацию вывода и генерацию .pyc файлов
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Сначала копируем только requirements, чтобы использовать кэш слоев Docker
COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копируем остальной код
COPY . .

CMD ["python", "bot.py"]