FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependências primeiro, para aproveitar o cache de camadas.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# collectstatic precisa de uma SECRET_KEY só para importar as settings; o valor não vai para a
# imagem (a chave real vem do ambiente em tempo de execução).

# Usuário sem privilégios; /app/media recebe o volume de uploads de campanha.
RUN useradd --system --create-home --uid 1000 app \
    && mkdir -p /app/media /app/staticfiles \
    && SECRET_KEY=build-only-throwaway-key python manage.py collectstatic --noinput \
    && chown -R app:app /app/media /app/staticfiles
USER app

EXPOSE 8000

# Padrão = servidor web; o worker sobrescreve o comando no docker-compose.yml.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
