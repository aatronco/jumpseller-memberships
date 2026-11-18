FROM python:3.12-slim
RUN useradd --uid 10001 --create-home app
WORKDIR /app
COPY memberships.py renewal_audit.py dashboard.py ./
RUN mkdir /app/data && chown app:app /app/data
USER app
ENV HOST=0.0.0.0 DATABASE_FILE=/app/data/memberships.sqlite CONFIG_FILE=/app/config.json
EXPOSE 4248
CMD ["python", "memberships.py"]
