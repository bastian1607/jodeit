ARG BUILD_VERSION
ARG BUILD_ARCH

FROM evcc/evcc:${BUILD_VERSION}

ARG BUILD_VERSION
ARG BUILD_ARCH

RUN mkdir -p /etc/jodeit
COPY jodeit-logo.png /etc/jodeit/jodeit-logo.png
COPY run.sh /run-jodeit.sh
RUN chmod 0755 /run-jodeit.sh

CMD ["/run-jodeit.sh"]

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"
