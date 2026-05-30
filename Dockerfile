FROM python:3.13-slim

RUN groupadd -r trader && useradd -r -g trader -d /app -s /sbin/nologin trader

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Bundle lightweight-charts so the chart page works without internet access
RUN python3 -c "\
import urllib.request; \
urllib.request.urlretrieve(\
  'https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js', \
  '/app/lightweight-charts.min.js')"

COPY *.py ./

RUN mkdir -p /app/data && chown -R trader:trader /app

VOLUME /app/data

USER trader

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["python", "main.py"]
