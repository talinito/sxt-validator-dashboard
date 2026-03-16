FROM python:3.12-slim

RUN groupadd -r exporter && useradd -r -g exporter exporter

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sxt_exporter.py economics.py ./

USER exporter
EXPOSE 9101

ENTRYPOINT ["python", "-u", "sxt_exporter.py"]
