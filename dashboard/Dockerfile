FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dashboard.py .

CMD gunicorn -b 0.0.0.0:${PORT:-5000} dashboard:app
