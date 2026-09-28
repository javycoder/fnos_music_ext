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
 * 用户脚本在 vm 隔离上下文中执行，除 globalThis.lx 外还提供桌面版渲染上下文里
 * 脚本惯用的 Web API（setInterval、atob/btoa、TextEncoder、fetch、performance、
 * crypto.getRandomValues 等——官方 preload 在完整渲染进程执行脚本，这些全部可用），
 * 不暴露 process/require/Buffer。对齐洛雪桌面版自定义源规范：
 * 事件 inited / request / updateAlert；非 local 源 action 仅 musicUrl。
 */
'use strict';

const fs = require('fs');
const vm = require('vm');
const crypto = require('crypto');
const zlib = require('zlib');
const readline = require('readline');

// needle（桌面版 lx.request 底层）默认 follow 最多 10 跳
const MAX_REDIRECTS = 10;

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
  } else if (body != null && typeof body === 'object'
             && typeof body.arrayBuffer !== 'function'          // Blob/File
             && !(body instanceof URLSearchParams)
             && !ArrayBuffer.isView(body) && !(body instanceof ArrayBuffer)) {
    // 桌面版底层是 needle：body 为普通对象时按 Content-Type 决定编码——
    // json 头 → JSON 序列化，否则 form-urlencoded。原样交给 fetch 只会抛
    // "RequestInit: body must be a string/Buffer/..."，野生脚本常直接传对象。
    const ctKey = Object.keys(headers).find((h) => h.toLowerCase() === 'content-type');
    const contentType = ctKey ? String(headers[ctKey]) : '';
    if (/json/i.test(contentType)) {
      body = JSON.stringify(body);
    } else {
      const params = new URLSearchParams();
      for (const [k, v] of Object.entries(body)) {
        params.append(k, v == null ? '' : (typeof v === 'object' ? JSON.stringify(v) : String(v)));
      }
      body = params.toString();
      if (!contentType) headers['Content-Type'] = 'application/x-www-form-urlencoded';
    }
  }
  // 桌面版 preload：response_timeout = timeout ? Math.min(timeout, 60_000) : 60_000
  const timeoutMs = Number(options.timeout) > 0 ? Math.min(Number(options.timeout), 60000) : 60000;
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
    // 对齐桌面版（preload.js）：body 优先尝试 JSON.parse 成对象，失败保持字符串——
    // 野生脚本普遍直接读 body.code 等字段，纯字符串会 undefined 崩溃。
    // 变量名不能叫 body：外层请求体也叫 body，let 提升的 TDZ 会让上面 fetch
    // options 里的 `body` 在初始化前被引用，带请求体的 POST 全部 ReferenceError
    let parsed = text;
    try { parsed = JSON.parse(text); } catch (_) { /* 非 JSON 保持字符串 */ }
    return { statusCode: resp.status, statusMessage: resp.statusText, headers: objHeaders(resp.headers),
             bytes: Buffer.byteLength(text), body: parsed };
  }
  const err = new Error('too many redirects');
  err.tooManyRedirects = true;
  throw err;
}

function lxRequest(url, options, callback) {
  // 双契约：传入 callback 时按官方文档走 (err, resp, body) 回调并返回取消函数；
  // 不传 callback 时按官方 preload 实际行为返回 Promise，resolve 整个响应对象
  // （statusCode/statusMessage/headers/bytes/body）。Promise 风格（await
  // lx.request(...)）是野生脚本的普遍写法，主流服务端中转源全靠它拿响应，
  // 只实现回调式会让这类源拿不到响应、解析全挂（脚本只能抛自家通用错误）。
  const useCb = typeof callback === 'function';
  const cb = useCb ? callback : function () {};
  const p = httpFetch(url, options).then((resp) => {
    const respObj = { statusCode: resp.statusCode, statusMessage: resp.statusMessage || '',
                      headers: resp.headers, bytes: resp.bytes, body: resp.body };
    cb(null, respObj, resp.body);
    return respObj;
  });
  if (!useCb) return p;
  p.catch((err) => cb(err, null, null));
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
      // 桌面版 preload：Buffer.from(buf, 'binary').toString(format)
      return Buffer.from(buffer, 'binary').toString(encoding || 'utf8');
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
      // 桌面版 preload 精确复刻：左侧零填充到 128 字节 + RSA_NO_PADDING。
      // 网易 weapi 等协议依赖无填充语义，PKCS1 算出的密文上游必拒。
      const buf = toBuf(data);
      return crypto.publicEncrypt(
        { key: String(key), padding: crypto.constants.RSA_NO_PADDING },
        Buffer.concat([Buffer.alloc(128 - buf.length), buf]),
      );
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
let scriptInited = false; // inited 前的未捕获异常按官方语义视为初始化失败

const lx = {
  version: '2.0.0',
  env: 'desktop',
  EVENT_NAMES,
  on(event, handler) {
    if (typeof handler === 'function') handlers[event] = handler;
  },
  send(event, data) {
    if (event === EVENT_NAMES.inited) {
      scriptInited = true;
      sendOut({ type: 'event', name: 'inited', payload: data });
    } else if (event === EVENT_NAMES.updateAlert) {
      sendOut({ type: 'event', name: 'updateAlert', payload: data });
    }
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

// 脚本注册的 interval 不能阻止宿主进程退出（rl close 时统一 process.exit）。
// 常驻 Node 进程里野生脚本的短周期定时器会持续空烧 CPU（issue #29 实测空闲 ~50%），
// 故对脚本侧 interval 做治理：周期下限钳制 + 活跃数量上限，过频/超限打 WARN。
const INTERVAL_MIN_MS = 500;      // 脚本 interval 周期下限（低于则提到该值）
const INTERVAL_MAX_ACTIVE = 32;   // 同时存活的脚本 interval 数量上限
const liveIntervals = new Set();
let intervalClampWarned = 0;

const sandboxSetInterval = (fn, delay, ...rest) => {
  const rawDelay = typeof delay === 'number' && Number.isFinite(delay) ? delay : 1;
  if (rawDelay < INTERVAL_MIN_MS && intervalClampWarned < 3) {
    intervalClampWarned += 1;
    logEvent('warn', [
      `[bridge] 脚本 interval 周期 ${rawDelay}ms 低于 ${INTERVAL_MIN_MS}ms，已钳制（防常驻进程空烧 CPU，issue #29）`,
    ]);
  }
  const delayMs = Math.max(INTERVAL_MIN_MS, Math.min(rawDelay, 2147483647));
  if (liveIntervals.size >= INTERVAL_MAX_ACTIVE) {
    logEvent('warn', [`[bridge] 脚本存活 interval 已达上限 ${INTERVAL_MAX_ACTIVE}，拒绝新增（疑似失控脚本）`]);
    // 返回哑定时器（unref + 永不触发回调），保证脚本 clearInterval(句柄) 不抛错
    const dummy = setInterval(() => {}, 3600000);
    if (dummy && typeof dummy.unref === 'function') dummy.unref();
    return dummy;
  }
  const timer = setInterval(fn, delayMs, ...rest);
  liveIntervals.add(timer);
  if (timer && typeof timer.unref === 'function') timer.unref();
  return timer;
};

const sandboxClearInterval = (timer) => {
  if (timer) liveIntervals.delete(timer);
  return clearInterval(timer);
};

// setTimeout 治理（issue #29 补充）：递归 setTimeout 链是野生脚本空烧 CPU 的另一
// 常见形态——自替换句柄让"存活数量"恒为 1，数量上限拦不住，只能按"触发频率"治理：
// 滑动窗口内触发过频（默认 10s 内 ≥200 次，即平均 >20/s）判定为失控，此后短周期
// （<500ms）的 setTimeout 钳制到 500ms，窗口排空后自动恢复。一次性/低频定时器
// （含 setTimeout(0) 让步写法）不受影响；最坏情形是每 10s 窗口内一段 ~200ms 的
// 1ms 连发，CPU 占比可忽略。阈值可用环境变量覆盖（演练/排查用）。
const TIMEOUT_FLOOD_WINDOW_MS = Math.max(1000, Number(process.env.LX_TIMEOUT_FLOOD_WINDOW_MS) || 10000);
const TIMEOUT_FLOOD_MAX_FIRES = Math.max(10, Number(process.env.LX_TIMEOUT_FLOOD_MAX_FIRES) || 200);
const TIMEOUT_FLOOD_CLAMP_MS = Math.max(50, Number(process.env.LX_TIMEOUT_FLOOD_CLAMP_MS) || 500);
const timeoutFireTimes = [];
let timeoutFloodWarned = 0;

function timeoutFloodActive() {
  const cutoff = Date.now() - TIMEOUT_FLOOD_WINDOW_MS;
  while (timeoutFireTimes.length && timeoutFireTimes[0] <= cutoff) timeoutFireTimes.shift();
  return timeoutFireTimes.length >= TIMEOUT_FLOOD_MAX_FIRES;
}

const sandboxSetTimeout = (fn, delay, ...rest) => {
  const rawDelay = typeof delay === 'number' && Number.isFinite(delay) ? delay : 0;
  let delayMs = rawDelay;
  if (delayMs < TIMEOUT_FLOOD_CLAMP_MS && delayMs >= 0 && timeoutFloodActive()) {
    delayMs = TIMEOUT_FLOOD_CLAMP_MS;
    if (timeoutFloodWarned < 3) {
      timeoutFloodWarned += 1;
      logEvent('warn', [
        `[bridge] 脚本 setTimeout 触发过频（${TIMEOUT_FLOOD_WINDOW_MS}ms 窗口内 ${timeoutFireTimes.length} 次），` +
        `短周期定时器已钳制到 ${TIMEOUT_FLOOD_CLAMP_MS}ms（防递归链空烧 CPU，issue #29）`,
      ]);
    }
  }
  const timer = setTimeout(() => {
    timeoutFireTimes.push(Date.now());
    timeoutFloodActive(); // 顺带排空过期窗口，保持判定随时间自愈
    return fn(...rest);
  }, Math.min(delayMs, 2147483647));
  if (timer && typeof timer.unref === 'function') timer.unref();
  return timer;
};

const sandbox = {
  lx,
  setTimeout: sandboxSetTimeout,
  clearTimeout,
  setInterval: sandboxSetInterval,
  clearInterval: sandboxClearInterval,
  console: sandboxConsole,
  URL,
  URLSearchParams,
  // 官方 preload 在完整渲染进程上下文执行脚本，以下 Web API 全部真实可用；
  // 野生脚本普遍直接使用它们，缺任何一个都是 ReferenceError 打断解析
  atob,
  btoa,
  TextEncoder,
  TextDecoder,
  fetch,
  performance,
  crypto: crypto.webcrypto,
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

// 桌面版把 window error/unhandledrejection 在未初始化时上报为 init 失败；
// 沙箱对齐该语义，避免异步崩溃的脚本“假 inited”后每次解析都失败
process.on('uncaughtException', (err) => {
  const detail = err && err.stack ? err.stack : String(err);
  if (!scriptInited) {
    fatal('script init error (uncaughtException): ' + detail);
    return;
  }
  logEvent('error', ['uncaughtException: ' + detail]);
});
process.on('unhandledRejection', (reason) => {
  const detail = reason && reason.message ? `${reason.message}${reason.stack ? '\n' + reason.stack : ''}` : formatArg(reason);
  if (!scriptInited) {
    fatal('script init error (unhandledRejection): ' + detail);
    return;
  }
  logEvent('error', ['unhandledRejection: ' + detail]);
});
