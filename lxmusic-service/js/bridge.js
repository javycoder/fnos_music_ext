#!/usr/bin/env node
/**
 * fnmusic-ext 洛雪自定义源沙箱桥（Node 端）。
 *
 * 与 Python 端（source_runtime.py）通过 stdin/stdout 的换行分隔 JSON 通信：
 *   Python → Node: {"id":"r1","type":"request","source":"kw","action":"musicUrl","info":{...}}
 *   Node → Python: {"type":"event","name":"inited","payload":{...}}
 *                 {"type":"event","name":"fatal","payload":{"error":"..."}}
 *                 {"type":"event","name":"updateAlert","payload":{...}}
 *                 {"id":"r1","ok":true,"result":"http://..."}
 *                 {"id":"r1","ok":false,"error":"..."}
 *                 {"type":"log","level":"info","message":"..."}
 *
 * 用户脚本在 vm 隔离上下文中执行，只能访问 globalThis.lx、setTimeout/clearTimeout、
 * console（转发为 log 事件，绝不污染协议 stdout）。对齐洛雪桌面版自定义源规范：
 * 事件 inited / request / updateAlert；非 local 源 action 仅 musicUrl。
 */
'use strict';

const fs = require('fs');
const vm = require('vm');
const crypto = require('crypto');
const zlib = require('zlib');
const readline = require('readline');

const MAX_REDIRECTS = 3;

function sendOut(obj) {
  try {
    process.stdout.write(JSON.stringify(obj) + '\n');
  } catch (_) { /* stdout 已关闭：无处可报 */ }
}

function formatArg(a) {
  if (typeof a === 'string') return a;
  try { return JSON.stringify(a); } catch (_) { return String(a); }
}

function logEvent(level, args) {
  const message = args.map(formatArg).join(' ');
  sendOut({ type: 'log', level, message: message.length > 2000 ? message.slice(0, 2000) : message });
}

// 洛雪桌面端给脚本的是完整 console；野生脚本普遍使用 group/table/count/time 等，
// 沙箱里缺任何一个方法都会让脚本抛 "console.xxx is not a function" 直接打断解析
let groupDepth = 0;
const consoleCounters = new Map();
const consoleTimers = new Map();
const indent = () => '  '.repeat(Math.max(0, groupDepth));

const sandboxConsole = {
  log: (...a) => logEvent('info', [indent(), ...a]),
  info: (...a) => logEvent('info', [indent(), ...a]),
  warn: (...a) => logEvent('warn', [indent(), ...a]),
  error: (...a) => logEvent('error', [indent(), ...a]),
  debug: (...a) => logEvent('debug', [indent(), ...a]),
  group: (...a) => { logEvent('info', [`${indent()}┌`, ...a]); groupDepth += 1; },
  groupCollapsed: (...a) => { logEvent('info', [`${indent()}┌`, ...a]); groupDepth += 1; },
  groupEnd: () => { groupDepth = Math.max(0, groupDepth - 1); },
  table: (data) => logEvent('info', [indent(), data]),
  dir: (...a) => logEvent('info', [indent(), ...a]),
  dirxml: (...a) => logEvent('info', [indent(), ...a]),
  trace: (...a) => logEvent('info', [indent(), 'trace', ...a]),
  count: (label = 'default') => {
    const n = (consoleCounters.get(label) || 0) + 1;
    consoleCounters.set(label, n);
    logEvent('info', [indent(), `${String(label)}: ${n}`]);
  },
  countReset: (label = 'default') => { consoleCounters.delete(label); },
  assert: (condition, ...a) => { if (!condition) logEvent('warn', [indent(), 'assertion failed:', ...a]); },
  time: (label = 'default') => { consoleTimers.set(label, process.hrtime.bigint()); },
  timeLog: (label = 'default') => {
    const start = consoleTimers.get(label);
    if (start != null) logEvent('info', [indent(), `${String(label)}: ${Number(process.hrtime.bigint() - start) / 1e6}ms`]);
  },
  timeEnd: (label = 'default') => { sandboxConsole.timeLog(label); consoleTimers.delete(label); },
  clear: () => {},
  profile: () => {},
  profileEnd: () => {},
  timeStamp: () => {},
  context: () => sandboxConsole,
};

function objHeaders(headers) {
  const out = {};
  for (const [k, v] of headers.entries()) out[k] = v;
  return out;
}

function sanitizeHeaders(headers) {
  const out = {};
  for (const [k, v] of Object.entries(headers || {})) {
    const lower = k.toLowerCase();
    if (lower === 'host' || lower === 'content-length') continue; // undici 自管
    out[k] = v;
  }
  return out;
}

async function httpFetch(url, options) {
  options = options || {};
  let method = String(options.method || 'GET').toUpperCase();
  const headers = Object.assign({}, options.headers || {});
  let body = options.body == null ? undefined : options.body;
  if (options.form != null && typeof options.form === 'object') {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(options.form)) params.append(k, v == null ? '' : String(v));
    body = params.toString();
    if (!Object.keys(headers).some((h) => h.toLowerCase() === 'content-type')) {
      headers['Content-Type'] = 'application/x-www-form-urlencoded';
    }
  } else if (options.formData != null && typeof options.formData === 'object') {
    const fd = new FormData();
    for (const [k, v] of Object.entries(options.formData)) {
      if (v && typeof v === 'object' && typeof v.arrayBuffer === 'function') fd.append(k, v, v.name);
      else fd.append(k, v == null ? '' : String(v));
    }
    body = fd;
  }
  const timeoutMs = Number(options.timeout) > 0 ? Number(options.timeout) : 20000;
  let current = String(url);
  for (let hop = 0; hop <= MAX_REDIRECTS; hop++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let resp;
    try {
      resp = await fetch(current, {
        method,
        headers: sanitizeHeaders(headers),
        body: method === 'GET' || method === 'HEAD' ? undefined : body,
        redirect: 'manual',
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }
    if (resp.status === 301 || resp.status === 302 || resp.status === 303 || resp.status === 307 || resp.status === 308) {
      const location = resp.headers.get('location');
      if (!location) return { statusCode: resp.status, headers: objHeaders(resp.headers), body: '' };
      const next = new URL(location, current).toString();
      if (!next.startsWith('http://') && !next.startsWith('https://')) {
        return { statusCode: resp.status, headers: objHeaders(resp.headers), body: '' };
      }
      if (resp.status === 303) { // 303 语义：后续请求改为 GET
        method = 'GET';
        body = undefined;
      }
      current = next;
      continue;
    }
    let text = '';
    try { text = await resp.text(); } catch (_) { text = ''; }
    return { statusCode: resp.status, headers: objHeaders(resp.headers), body: text };
  }
  const err = new Error('too many redirects');
  err.tooManyRedirects = true;
  throw err;
}

function lxRequest(url, options, callback) {
  const cb = typeof callback === 'function' ? callback : function () {};
  httpFetch(url, options)
    .then((resp) => cb(null, { statusCode: resp.statusCode, statusMessage: '', headers: resp.headers }, resp.body))
    .catch((err) => cb(err, null, null));
  // 规范要求返回取消函数；Python 侧持有总超时，桥内无需真实取消
  return function () {};
}

function toBuf(data, encoding) {
  if (typeof data === 'string') return Buffer.from(data, encoding || 'utf8');
  if (Buffer.isBuffer(data)) return data;
  if (data instanceof Uint8Array) return Buffer.from(data);
  return Buffer.from(String(data), 'utf8');
}

const lxUtils = {
  buffer: {
    from(data, encoding) {
      if (typeof data === 'string') return Buffer.from(data, encoding || 'utf8');
      return toBuf(data);
    },
    bufToString(buffer, encoding) {
      return Buffer.from(buffer).toString(encoding || 'utf8');
    },
  },
  crypto: {
    md5(data) {
      return crypto.createHash('md5').update(toBuf(data)).digest('hex');
    },
    randomBytes(size) {
      return crypto.randomBytes(Number(size) || 0);
    },
    aesEncrypt(data, mode, key, iv) {
      const cipher = crypto.createCipheriv(String(mode || ''), toBuf(key), iv ? toBuf(iv) : null);
      return Buffer.concat([cipher.update(toBuf(data)), cipher.final()]);
    },
    aesDecrypt(data, mode, key, iv) {
      const decipher = crypto.createDecipheriv(String(mode || ''), toBuf(key), iv ? toBuf(iv) : null);
      return Buffer.concat([decipher.update(toBuf(data)), decipher.final()]);
    },
    rsaEncrypt(data, key) {
      return crypto.publicEncrypt({ key: String(key), padding: crypto.constants.RSA_PKCS1_PADDING }, toBuf(data));
    },
  },
  zlib: {
    inflate(data) { return zlib.inflateSync(toBuf(data)); },
    deflate(data) { return zlib.deflateSync(toBuf(data)); },
    gzip(data) { return zlib.gzipSync(toBuf(data)); },
    ungzip(data) { return zlib.gunzipSync(toBuf(data)); },
  },
};

const EVENT_NAMES = { inited: 'inited', request: 'request', updateAlert: 'updateAlert' };
const handlers = {};

const lx = {
  version: '2.0.0',
  env: 'desktop',
  EVENT_NAMES,
  on(event, handler) {
    if (typeof handler === 'function') handlers[event] = handler;
  },
  send(event, data) {
    if (event === EVENT_NAMES.inited) sendOut({ type: 'event', name: 'inited', payload: data });
    else if (event === EVENT_NAMES.updateAlert) sendOut({ type: 'event', name: 'updateAlert', payload: data });
  },
  request: lxRequest,
  utils: lxUtils,
  currentScriptInfo: null, // 加载脚本前填充
};

function fatal(message) {
  sendOut({ type: 'event', name: 'fatal', payload: { error: message } });
  // 给 stdout 一点时间落地后再退出
  setTimeout(() => process.exit(3), 50).unref();
}

const scriptPath = process.argv[2];
if (!scriptPath) {
  fatal('bridge started without script path');
  process.exit(2);
}
let rawScript = '';
try {
  rawScript = fs.readFileSync(scriptPath, 'utf8');
} catch (e) {
  fatal('cannot read script file: ' + (e && e.message ? e.message : e));
  process.exit(2);
}
let meta = {};
try {
  meta = JSON.parse(process.argv[3] || '{}');
} catch (_) { meta = {}; }
lx.currentScriptInfo = {
  name: meta.name || '',
  description: meta.description || '',
  version: meta.version || '',
  author: meta.author || '',
  homepage: meta.homepage || '',
  rawScript,
};

const sandbox = {
  lx,
  setTimeout,
  clearTimeout,
  console: sandboxConsole,
  URL,
  URLSearchParams,
};
sandbox.globalThis = sandbox;
sandbox.global = sandbox;
vm.createContext(sandbox);

try {
  vm.runInContext(rawScript, sandbox, { filename: 'lx-source-script.js', timeout: 10000 });
} catch (e) {
  fatal('script load failed: ' + (e && e.stack ? e.stack : e));
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on('line', (line) => {
  line = line.trim();
  if (!line) return;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch (_) {
    sendOut({ type: 'log', level: 'error', message: 'host sent invalid json' });
    return;
  }
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'ping') {
    sendOut({ type: 'pong' });
    return;
  }
  if (msg.type !== 'request') return;
  const handler = handlers[EVENT_NAMES.request];
  if (typeof handler !== 'function') {
    sendOut({ id: msg.id, ok: false, error: 'script did not register a request handler' });
    return;
  }
  Promise.resolve()
    .then(() => handler({ source: msg.source, action: msg.action, info: msg.info }))
    .then((result) => sendOut({ id: msg.id, ok: true, result: result === undefined ? null : result }))
    .catch((err) => sendOut({ id: msg.id, ok: false, error: err && err.message ? err.message : String(err) }));
});
rl.on('close', () => process.exit(0));
process.stdin.on('error', () => process.exit(0));

process.on('uncaughtException', (err) => {
  logEvent('error', ['uncaughtException: ' + (err && err.stack ? err.stack : err)]);
});
process.on('unhandledRejection', (reason) => {
  const detail = reason && reason.message ? `${reason.message}${reason.stack ? '\n' + reason.stack : ''}` : formatArg(reason);
  logEvent('error', ['unhandledRejection: ' + detail]);
});
