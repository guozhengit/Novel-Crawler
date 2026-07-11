FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-noto-cjk \
    fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY README.md ./
COPY requirements.txt ./
COPY novel_crawler ./novel_crawler
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./main.py
COPY font_decode_map.json ./font_decode_map.json

VOLUME ["/app/data"]
ENTRYPOINT ["python", "main.py", "--data-dir", "/app/data"]
