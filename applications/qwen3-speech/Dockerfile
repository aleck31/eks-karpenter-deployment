FROM 123456789012.dkr.ecr.us-west-2.amazonaws.com/qwen3-asr:latest

RUN pip install websockets python-multipart --no-cache-dir

COPY asr-adapter.py /app/asr-adapter.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV ASR_BACKEND_URL=http://localhost:8000
ENV ASR_BACKEND_WS=ws://localhost:8000

EXPOSE 8000 8001

ENTRYPOINT ["/app/entrypoint.sh"]
