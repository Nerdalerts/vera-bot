FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py conversation_handlers.py ./
COPY vera ./vera
COPY prompts ./prompts
EXPOSE 8080
# ONE worker on purpose: all state (contexts, conversations, suppression) lives in process memory.
CMD ["sh", "-c", "uvicorn bot:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 75"]
