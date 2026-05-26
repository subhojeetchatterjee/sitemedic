const { NodeSDK } = require('@opentelemetry/sdk-node');
const { getNodeAutoInstrumentations } = require('@opentelemetry/auto-instrumentations-node');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const { OTLPMetricExporter } = require('@opentelemetry/exporter-metrics-otlp-http');
const { PeriodicExportingMetricReader } = require('@opentelemetry/sdk-metrics');
const { Resource } = require('@opentelemetry/resources');
const { SEMRESATTRS_SERVICE_NAME, SEMRESATTRS_SERVICE_VERSION } = require('@opentelemetry/semantic-conventions');

const resource = new Resource({
  [SEMRESATTRS_SERVICE_NAME]: process.env.OTEL_SERVICE_NAME || 'sitemedic-demo-app',
  [SEMRESATTRS_SERVICE_VERSION]: process.env.SERVICE_VERSION || '1.0.0',
});

const dtEndpoint = process.env.OTEL_EXPORTER_OTLP_ENDPOINT;
const dtHeaders = process.env.OTEL_EXPORTER_OTLP_HEADERS
  ? Object.fromEntries(
      process.env.OTEL_EXPORTER_OTLP_HEADERS.split(',').map(h => h.split('='))
    )
  : {};

const traceExporter = new OTLPTraceExporter({
  url: dtEndpoint ? `${dtEndpoint}/v1/traces` : undefined,
  headers: dtHeaders,
});

const metricExporter = new OTLPMetricExporter({
  url: dtEndpoint ? `${dtEndpoint}/v1/metrics` : undefined,
  headers: dtHeaders,
});

const sdk = new NodeSDK({
  resource,
  traceExporter,
  metricReader: new PeriodicExportingMetricReader({
    exporter: metricExporter,
    exportIntervalMillis: 15000,
  }),
  instrumentations: [getNodeAutoInstrumentations()],
});

sdk.start();

process.on('SIGTERM', () => sdk.shutdown());
