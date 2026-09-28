FROM python:3.11-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libffi-dev libssl-dev curl tzdata && \
    rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Kolkata
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# Install Python deps first (layer cache)
COPY deploy/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Create runtime dirs
RUN mkdir -p logs/live data reports

# Non-root user
RUN useradd -m -u 1000 trader && chown -R trader:trader /app
USER trader

EXPOSE 5051

CMD ["gunicorn", \
     "--config", "deploy/gunicorn.conf.py", \
     "--bind", "0.0.0.0:5051", \
     "web.app:create_app()"]
