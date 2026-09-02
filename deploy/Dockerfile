# Imagem enxuta: interpretador e gunicorn. O codigo NAO entra na imagem, vem por
# bind mount - assim atualizar e `git pull` + restart, sem rebuild.
#
# O gcc fica na imagem de proposito: o nucleo em C (native/) e compilado aqui
# dentro, contra a mesma libc que vai executa-lo. Binario compilado no host pode
# exigir uma glibc mais nova que a da imagem e nao carregar - falha silenciosa,
# porque o nucleo em C e opcional e o programa cai no Python puro sem avisar.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "gunicorn>=21.2"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

CMD ["gunicorn", "-c", "deploy/gunicorn.conf.py", "deploy.wsgi:application"]
