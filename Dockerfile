FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Instala dependências primeiro para aproveitar o cache de camadas.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código da aplicação + scripts (ingestão da base).
COPY app ./app
COPY scripts ./scripts

EXPOSE 8000

# main.py é criado no Módulo 7; até lá o container sobe mas o endpoint não existe.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
