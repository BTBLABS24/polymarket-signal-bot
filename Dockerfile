FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy signal bot
COPY simple_signal_bot.py .

# Run the bot
CMD ["python", "-u", "simple_signal_bot.py"]
