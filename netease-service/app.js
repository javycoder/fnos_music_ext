/**
 * Production entry point for netease-service
 */
const path = require('path');
const fs = require('fs');
const { spawnSync } = require('child_process');

// Runtime version check: must be >= 22.13.0
const [major, minor] = process.versions.node.split('.').map(Number);
if (major < 22 || (major === 22 && minor < 13)) {
  console.error(`Node.js >= 22.13.0 is required. Current version: ${process.version}`);
  process.exit(1);
}

// Configure dedicated tmp directory before loading upstream SDK
const dataDir = process.env.SOURCES_DATA_DIR || path.join(process.cwd(), 'sources-data');
const dedicatedTmp = path.join(dataDir, 'netease', 'tmp');
try {
  fs.mkdirSync(dedicatedTmp, { recursive: true, mode: 0o700 });
  process.env.TMPDIR = dedicatedTmp;
} catch {}

if (!process.env.NETEASE_SESSION_FILE) {
  const migration = spawnSync('python3', [
    '-c',
    'import sys; from proxy.netease_legacy import migrate_session; migrate_session(sys.argv[1])',
    dataDir,
  ], {
    cwd: path.resolve(__dirname, '..'),
    stdio: 'ignore',
    timeout: 10000,
  });
  if (migration.error || migration.status !== 0) {
    console.warn('[init] legacy NetEase session migration failed');
  }
}

// Global Axios default timeout (10s)
try {
  const axios = require('axios');
  axios.defaults.timeout = 10000;
} catch {}

const { createService } = require('./service');

const host = process.env.HOST || '127.0.0.1';
const port = parseInt(process.env.PORT || '8770', 10);

const service = createService({ dataDir });

const server = service.app.listen(port, host, () => {
  console.log(`netease-service running at http://${host}:${port}`);
  service.ensureInitialized().catch((err) => {
    console.warn(`[init] background initialization warning: ${err.message}`);
  });
});

const shutdown = () => {
  console.log('Received shutdown signal, closing netease-service...');
  server.close(() => {
    process.exit(0);
  });
  setTimeout(() => process.exit(0), 5000).unref();
};

process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);

module.exports = { service, server };
