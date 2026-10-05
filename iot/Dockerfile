FROM python:3.13-slim
WORKDIR /app
COPY iot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY iot/ ./iot/
COPY web/ ./web/
ENV HANZHUB_IOT_WEB_DIR=/app/web PYTHONUNBUFFERED=1
EXPOSE 4011
CMD ["python", "iot/server.py"]
