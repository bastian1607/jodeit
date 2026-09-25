ARG BUILD_VERSION
ARG BUILD_ARCH

FROM evcc/evcc:${BUILD_VERSION}

COPY jodeit-logo.png /etc/jodeit/jodeit-logo.png

CMD ["evcc", "--config", "/config/evcc.yaml"]
