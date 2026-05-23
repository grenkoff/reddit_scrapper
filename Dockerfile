FROM python:3.12-slim

RUN apt-get update && apt-get install -y ffmpeg fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY . .
RUN mkdir -p data tmp

EXPOSE 8080

CMD ["python", "-m", "src.main"]
