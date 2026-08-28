# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --system rehab && useradd --system --gid rehab --home /app rehab

COPY --chown=rehab:rehab . .
RUN mkdir -p /app/media /app/private-artifacts /app/staticfiles \
    && chown -R rehab:rehab /app

USER rehab

EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
