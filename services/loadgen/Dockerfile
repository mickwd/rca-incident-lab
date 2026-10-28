FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY loadgen.py .
RUN useradd --uid 10002 loadgen
USER loadgen
EXPOSE 9102
ENTRYPOINT ["python", "loadgen.py"]
