FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir discord.py plexapi aiohttp

COPY onboardarr.py /app/onboardarr.py
COPY invite_languages.py /app/invite_languages.py

ENTRYPOINT ["python", "-u", "/app/onboardarr.py"]
CMD ["--env-file", "/run/secrets/onboardarr.env"]
