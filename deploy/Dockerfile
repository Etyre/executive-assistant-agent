FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 EA_DATA=/data EA_CONFIG=/app/config.toml
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ea ./ea
COPY instructions ./instructions
COPY config.toml ./config.toml
VOLUME ["/data"]
EXPOSE 8800
HEALTHCHECK CMD python -c "import httpx;httpx.get('http://127.0.0.1:8800/health').raise_for_status()"
CMD ["python", "-m", "ea", "serve"]
