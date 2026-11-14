FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/pm25.db \
    PORT=5000

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY templates ./templates
COPY static ./static

RUN mkdir -p /data
EXPOSE 5000

CMD ["python", "app.py"]
