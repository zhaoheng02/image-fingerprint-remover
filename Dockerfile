FROM python:3.11-slim

WORKDIR /app

COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

COPY imgclean ./imgclean
COPY imgclean_web ./imgclean_web

ENV HOST=0.0.0.0
ENV PORT=8000
ENV IMGCLEAN_WEB_DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 8000

CMD ["python", "-m", "imgclean_web"]
