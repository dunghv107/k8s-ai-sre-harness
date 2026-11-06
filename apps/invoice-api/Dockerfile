FROM golang:1.27.3-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/invoice-api .

FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
COPY --from=build /out/invoice-api /invoice-api
ENV OTEL_SERVICE_NAME=invoice-api OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/invoice-api"]
