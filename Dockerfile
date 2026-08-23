FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pounce/ pounce/

# the volume is owned by uid 1000 on the host
USER 1000:1000

CMD ["python", "-m", "pounce", "run"]
