const express = require('express');
const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;

// Environment: dev (allows fault injection), staging/prod (blocks fault injection)
const ENV = process.env.ENV || 'dev';
const isProduction = ENV === 'prod';
const isStaging = ENV === 'staging';

// Fault-injection state
let injectLatencyMs = 0;
let errorRate = 0;
let memLeakHandles = [];
let memLeakBuffers = [];

// Pub/Sub flood state
let pubsubFloodHandle = null;
let pubsubFloodCount = 0;

// Simulate a checkout flow — respects injected faults
app.get('/checkout', async (req, res) => {
  if (Math.random() < errorRate) {
    return res.status(500).json({ error: 'Internal Server Error', code: 'CHECKOUT_FAILED' });
  }
  if (injectLatencyMs > 0) {
    await new Promise(r => setTimeout(r, injectLatencyMs));
  }
  res.json({
    orderId: `ord_${Date.now()}`,
    status: 'confirmed',
    latencyInjected: injectLatencyMs,
  });
});

app.get('/products', async (req, res) => {
  if (Math.random() < errorRate) {
    return res.status(503).json({ error: 'Service Unavailable' });
  }
  if (injectLatencyMs > 0) {
    await new Promise(r => setTimeout(r, injectLatencyMs));
  }
  res.json({ products: [{ id: 1, name: 'Widget', price: 9.99 }] });
});

app.get('/health', (req, res) => {
  res.json({
    status: 'ok',
    faults: {
      latencyMs: injectLatencyMs,
      errorRate,
      memLeakActive: memLeakHandles.length > 0,
      pubsubFloodActive: pubsubFloodHandle !== null,
      pubsubMessagesSent: pubsubFloodCount,
    },
  });
});

// ── Fault injection endpoints ──────────────────────────────────────────────
//
// SECURITY: These endpoints are DEVELOPMENT ONLY
// - prod: blocked entirely (404)
// - staging: optional admin token
// - dev: freely accessible

// Middleware: Gate fault injection endpoints by environment
const faultInjectionMiddleware = (req, res, next) => {
  if (isProduction) {
    // Block entirely in production
    return res.status(404).json({ error: 'Not Found' });
  }

  if (isStaging) {
    // In staging, require an admin token for fault injection
    const adminToken = req.headers['x-admin-token'];
    if (!adminToken || adminToken !== process.env.ADMIN_TOKEN) {
      return res.status(403).json({ error: 'Forbidden: requires X-Admin-Token header' });
    }
  }

  // In dev (or staging with valid token), allow the request
  next();
};

app.post('/inject/latency', faultInjectionMiddleware, (req, res) => {
  injectLatencyMs = req.body.ms ?? 3000;
  console.log(`[FAULT] Latency injection: ${injectLatencyMs}ms`);
  res.json({ injected: 'latency', ms: injectLatencyMs });
});

app.post('/inject/errors', faultInjectionMiddleware, (req, res) => {
  errorRate = req.body.rate ?? 0.5;
  console.log(`[FAULT] Error rate injection: ${errorRate * 100}%`);
  res.json({ injected: 'errors', rate: errorRate });
});

app.post('/inject/memory', faultInjectionMiddleware, (req, res) => {
  const handle = setInterval(() => {
    memLeakBuffers.push(Buffer.alloc(10_000_000));
    console.log(`[FAULT] Memory leak: ${memLeakBuffers.length * 10}MB allocated`);
  }, 1000);
  memLeakHandles.push(handle);
  console.log('[FAULT] Memory leak injection started');
  res.json({ injected: 'memory_leak', intervalMs: 1000, chunkMb: 10 });
});

/**
 * Pub/Sub backlog injection — floods a topic faster than the subscriber drains it.
 * This simulates a "consumer lag growing" incident for the Pub/Sub acceptance test.
 *
 * Body: { topicName: "projects/p/topics/t", messagesPerSecond: 50 }
 *
 * The demo has no subscriber running, so the backlog accumulates in the subscription
 * until Cloud Monitoring detects num_undelivered_messages growing.
 *
 * ⚠️  DEVELOPMENT ONLY: Gated by faultInjectionMiddleware
 */
app.post('/inject/pubsub', faultInjectionMiddleware, async (req, res) => {
  const { PubSub } = require('@google-cloud/pubsub');

  const topicName = req.body.topicName;
  const mps = req.body.messagesPerSecond ?? 50;

  if (!topicName) {
    return res.status(400).json({ error: 'topicName is required' });
  }
  if (pubsubFloodHandle) {
    return res.status(409).json({ error: 'Pub/Sub flood already active. POST /reset first.' });
  }

  const client = new PubSub();
  const topic = client.topic(topicName);

  const intervalMs = Math.max(10, Math.round(1000 / mps));
  pubsubFloodCount = 0;

  pubsubFloodHandle = setInterval(async () => {
    const batch = [];
    const batchSize = Math.max(1, Math.round(mps / (1000 / intervalMs)));
    for (let i = 0; i < batchSize; i++) {
      batch.push(
        topic.publishMessage({
          data: Buffer.from(JSON.stringify({
            event: 'order.created',
            orderId: `ord_${Date.now()}_${i}`,
            ts: new Date().toISOString(),
          })),
          attributes: { source: 'sitemedic-fault-injector' },
        }).then(() => { pubsubFloodCount++; }).catch(() => {})
      );
    }
    await Promise.all(batch);
    if (pubsubFloodCount % 500 === 0) {
      console.log(`[FAULT] Pub/Sub flood: ${pubsubFloodCount} messages published to ${topicName}`);
    }
  }, intervalMs);

  console.log(`[FAULT] Pub/Sub flood started: ${mps} msg/s → ${topicName}`);
  res.json({ injected: 'pubsub_flood', topicName, messagesPerSecond: mps, intervalMs });
});

app.post('/reset', faultInjectionMiddleware, (req, res) => {
  injectLatencyMs = 0;
  errorRate = 0;
  memLeakHandles.forEach(h => clearInterval(h));
  memLeakHandles = [];
  memLeakBuffers = [];

  if (pubsubFloodHandle) {
    clearInterval(pubsubFloodHandle);
    pubsubFloodHandle = null;
    console.log(`[RESET] Pub/Sub flood stopped (sent ${pubsubFloodCount} messages total)`);
    pubsubFloodCount = 0;
  }

  if (global.gc) global.gc();
  console.log('[RESET] All fault injections cleared');
  res.json({ reset: true });
});

app.listen(PORT, () => {
  console.log(`SiteMedic demo-app listening on :${PORT}`);
});
