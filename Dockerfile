FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8090 \
    RUN_HOUR=3 \
    TZ=Europe/Warsaw

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

VOLUME ["/data"]
EXPOSE 8090
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/status' % os.environ.get('PORT','8090'))" || exit 1

CMD ["python", "-m", "app.main"]
