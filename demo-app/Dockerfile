# Build arg lets us bake the version into the image at build time.
# docker build --build-arg APP_VERSION=v1 -t payment-service:v1 .
ARG APP_VERSION=v1

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Promote the build arg to a runtime ENV so app.py can read it.
ARG APP_VERSION
ENV APP_VERSION=${APP_VERSION}

EXPOSE 5000

CMD ["python", "app.py"]
