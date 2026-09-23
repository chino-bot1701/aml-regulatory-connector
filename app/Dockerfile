FROM ${ECR_REGISTRY}/python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --force-reinstall -r requirements.txt

COPY *.py .
COPY utils/ utils/
COPY SVC_ANALYTICS_key_pk8.pem .

EXPOSE 8501

#HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
#    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')"

CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
