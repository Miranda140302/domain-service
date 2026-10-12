FROM python:3.13-alpine

WORKDIR /app
COPY app.py initial-services.json ./

EXPOSE 8080
CMD ["python", "-u", "app.py"]
