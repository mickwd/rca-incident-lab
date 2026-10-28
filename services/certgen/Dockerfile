FROM alpine:3.20
RUN apk add --no-cache openssl
COPY gen-certs.sh /usr/local/bin/gen-certs.sh
RUN chmod +x /usr/local/bin/gen-certs.sh
ENTRYPOINT ["/usr/local/bin/gen-certs.sh"]
CMD ["init"]
