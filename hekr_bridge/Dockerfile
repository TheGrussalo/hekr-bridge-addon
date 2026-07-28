FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends jq \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir paho-mqtt

WORKDIR /app
COPY hekr_bridge.py /app/hekr_bridge.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
