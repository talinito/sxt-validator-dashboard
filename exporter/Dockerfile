FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sxt_exporter.py .

EXPOSE 9101

ENTRYPOINT ["python", "-u", "sxt_exporter.py"]
