FROM python:3.12-slim

WORKDIR /app

COPY realtime_scanner/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY realtime_scanner/kalshi_reversion_scanner.py .

CMD ["python", "-u", "kalshi_reversion_scanner.py"]
