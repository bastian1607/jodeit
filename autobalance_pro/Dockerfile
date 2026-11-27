FROM python:3.12-alpine
RUN apk add --no-cache tzdata
WORKDIR /app
COPY app/ /app/
ENV PYTHONUNBUFFERED=1 AUTO_BALANCE_DB=/data/autobalance.db
EXPOSE 8099
CMD ["python", "server.py"]
