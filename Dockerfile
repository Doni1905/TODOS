FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app_db.py .
COPY templates/ templates/

EXPOSE 5001

CMD ["python", "app_db.py"]
