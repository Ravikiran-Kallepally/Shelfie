FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py auth.py catalog.py reads.py social.py store.py ./
COPY static ./static
# the database and downloaded books live on a persistent volume mounted here
ENV SHELFIE_DATA=/data
EXPOSE 8080
# --proxy-headers: the host terminates https in front of us; this makes cookies "Secure"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips=*"]
