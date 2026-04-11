FROM python:3.12-slim

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .

ENV DRY_RUN=true
ENV POLL_INTERVAL=15
ENV STATE_FILE=/tmp/chimera_scalp_state.json

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
