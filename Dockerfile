FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Install system packages
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-dev \
    curl \
    wget \
    git \
    zip \
    unzip \
    tar \
    gzip \
    net-tools \
    iputils-ping \
    netcat-openbsd \
    build-essential \
    lsof \
    procps \
    libxml2-dev \
    libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Verify installations
RUN python3 --version && pip3 --version && node --version && npm --version

WORKDIR /app

# Create required directories
RUN mkdir -p /app/downloads /app/temp

# Install Python dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Install cloudscraper
RUN pip3 install --no-cache-dir cloudscraper

# Copy app
COPY main.py .

EXPOSE 7860

CMD ["python3", "main.py"]
