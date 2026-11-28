FROM python:3.15-slim-trixie

RUN useradd --uid 1000 --create-home archive
COPY forecast_archive.py headroom.py /app/
USER archive
ENV PYTHONUNBUFFERED=1
CMD ["python3", "-I", "/app/forecast_archive.py"]
