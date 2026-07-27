FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY frontend ./frontend
COPY scripts ./scripts

# Frontend build is optional for M0: a committed fallback page in app/static/dist
# is served by default. To ship the real SPA, uncomment the line below.
# RUN apt-get update && apt-get install -y nodejs npm && cd frontend && npm install && npm run build

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
