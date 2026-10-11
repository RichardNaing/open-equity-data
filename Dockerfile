FROM python:3.12-slim

RUN useradd -m -u 1000 user
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user . .

USER user
ENV HOME=/home/user PYTHONUNBUFFERED=1 PORT=7860
EXPOSE 7860
CMD ["python", "server.py"]
