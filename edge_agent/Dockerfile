FROM alpine:3.20
RUN apk add --no-cache ffmpeg python3
COPY agent.py /agent.py
USER nobody
ENTRYPOINT ["python3", "-u", "/agent.py"]
