FROM --platform=linux/amd64 rayproject/ray:2.10.0-py311

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


COPY ray_pipeline.py .
