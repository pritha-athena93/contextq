FROM --platform=linux/amd64 rayproject/ray:2.10.0-py311

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

COPY ray_pipeline.py .
