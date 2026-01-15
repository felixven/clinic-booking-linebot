FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000
EXPOSE 8000

# CMD ["sh", "-c", "echo GUNICORN_START='gunicorn app:app --bind 0.0.0.0:8000 --workers 2 --timeout 120' && gunicorn app:app --bind 0.0.0.0:8000 --workers 2 --timeout 120"]
CMD ["sh", "-c", "echo GUNICORN_START='gunicorn app:app --bind 0.0.0.0:8000 --worker-class gthread --workers 4 --threads 4 --timeout 120' && gunicorn app:app --bind 0.0.0.0:8000 --worker-class gthread --workers 4 --threads 4 --timeout 120"]


