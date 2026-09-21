#!/usr/bin/env node
/**
 * bridge.js 沙箱桥的真实 Node 行为测试（node 原生 assert + 本地 http 服务器）。
 *
 * 由 lxmusic-service/test_bridge_node.py 在 node 可用时执行（无 node 自动跳过）。
 * 覆盖最近线上事故的全部行为面：
 *   - console 全 API 可调用（沙箱缺方法曾让野生脚本直接崩）
 *   - lx.request 响应体 JSON 自动转对象 / 非 JSON 保持字符串
 *   - 重定向、303 改 GET、超时中断、form 编码、object body 编码（对齐 needle）
 *   - musicUrl 协议往返、ping/pong、utils（buffer/crypto/zlib）冒烟
 *   - rsaEncrypt 对齐官方 NO_PADDING + 左零填充（网易 weapi 依赖此语义）
 *   - 沙箱 Web API（setInterval/atob/btoa/TextEncoder/crypto.getRandomValues 等）
 *   - 未 inited 前异步崩溃按官方语义报 fatal；inited 后仅记日志
 *   - 沙箱全局白名单（process/require/Buffer 不可见）
 *
 * 运行：node lxmusic-service/js/test_bridge.js   （退出码 0=全部通过）
 */
'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const BRIDGE = path.join(__dirname, 'bridge.js');
const NODE = process.execPath;

// ------------------------------------------------------------------ 测试服务器 ---

const server = http.createServer((req, res) => {
  const chunks = [];
  req.on('data', (c) => chunks.push(c));
  req.on('end', () => {
    const body = Buffer.concat(chunks).toString('utf8');
    if (req.url.startsWith('/redirect301')) {
      res.writeHead(301, { location: '/redirect302' });
      return res.end();
    }
    if (req.url.startsWith('/redirect302')) {
      res.writeHead(302, { location: '/final' });
      return res.end();
    }
    if (req.url.startsWith('/final')) {
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ code: 0, data: 'ok' }));
    }
    if (req.url.startsWith('/see303')) {
      res.writeHead(303, { location: '/echo-method' });
      return res.end();
    }
    if (req.url.startsWith('/loop')) {
      res.writeHead(302, { location: '/loop' });
      return res.end();
    }
    if (req.url.startsWith('/hang')) {
      return; // 永不响应：测超时
    }
    if (req.url.startsWith('/plain')) {
      res.writeHead(200, { 'content-type': 'text/plain' });
      return res.end('not-json-body');
    }
    if (req.url.startsWith('/echo-method')) {
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ method: req.method }));
    }
    if (req.url.startsWith('/echo-form')) {
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ ct: req.headers['content-type'], body }));
    }
    res.writeHead(404);
    res.end('{}');
  });
});

// ------------------------------------------------------------------ 桥进程驱动 ---

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function safeParse(line) {
  try { return JSON.parse(line); } catch (_) { return null; }
}

/**
 * 驱动一个桥进程直到满足条件，再收 stdin 让其自然退出。
 * 关键点：stdin 不能提前 end（readline close 会立刻 exit(0)，
 * 异步 lx.request 回调还没触发进程就没了），必须先等到目标输出。
 */
class BridgeProc {
  constructor(scriptBody) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lxbridge-test-'));
    const scriptPath = path.join(dir, 'source.js');
    fs.writeFileSync(scriptPath, scriptBody, 'utf8');
    this.outLines = [];
    this.errText = '';
    this.exitCode = null;
    this.exited = false;
    this.child = spawn(NODE, [BRIDGE, scriptPath, JSON.stringify({ name: 't', version: '1.0.0' })],
      { stdio: ['pipe', 'pipe', 'pipe'] });
    this.exitPromise = new Promise((resolve) => {
      this.child.on('exit', (code) => { this.exited = true; this.exitCode = code; resolve(code); });
    });
    let raw = '';
    this.child.stdout.on('data', (d) => {
      raw += d.toString('utf8');
      let idx;
      while ((idx = raw.indexOf('\n')) >= 0) { // 按行切，容忍 chunk 边界
        const line = raw.slice(0, idx).trim();
        raw = raw.slice(idx + 1);
        if (line) this.outLines.push(line);
      }
    });
    this.child.stderr.on('data', (d) => { this.errText += d.toString('utf8'); });
  }

  events() {
    return this.outLines.map(safeParse).filter(Boolean);
  }

  logs() {
    return this.events().filter((e) => e.type === 'log').map((e) => String(e.message || ''));
  }

  write(line) {
    this.child.stdin.write(line + '\n');
  }

  async waitUntil(predicate, timeoutMs = 8000, stepMs = 25) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (predicate(this.events())) return true;
      await delay(stepMs);
    }
    return predicate(this.events());
  }

  async finish(killFirst = false) {
    if (killFirst && !this.exited) this.child.kill('SIGKILL');
    if (!this.exited) {
      try { this.child.stdin.end(); } catch (_) { /* 已关闭 */ }
      await Promise.race([
        this.exitPromise,
        delay(5000).then(() => { if (!this.exited) this.child.kill('SIGKILL'); }),
      ]);
    }
    return this.exitCode;
  }
}

/** 完整跑一个用例：起桥 → 喂 stdin → 等条件 → 收尾 → 断言回调。
 * 断言必须放在 finish 之后（exitCode 才有值；outLines 已冻结）。 */
async function bridgeCase(scriptBody, { feed = [], waitUntil, timeoutMs = 8000, then } = {}) {
  const b = new BridgeProc(scriptBody);
  let error;
  try {
    for (const line of feed) b.write(line);
    const ok = await b.waitUntil(waitUntil, timeoutMs);
    if (!ok) {
      throw new Error('等待条件超时; 已收到: ' + b.outLines.slice(-6).join(' | '));
    }
    await b.finish();
    if (then) await then(b);
  } catch (e) {
    error = e;
  }
  if (error) {
    await b.finish(true);
    throw error;
  }
  return b;
}

// ------------------------------------------------------------------ 用例 ---

let base = ''; // 本地测试服务器地址，注入脚本时替换 BASE 占位符

function inject(script) {
  return script.replace(/BASE/g, `'${base}'`);
}

async function test_console_full_api_never_crashes() {
  const script = `
lx.on(lx.EVENT_NAMES.request, () => {});
console.log('log'); console.info('info'); console.warn('warn'); console.error('error');
console.debug('debug');
console.group('g'); console.groupCollapsed('gc'); console.table({a:1}); console.dir({b:2});
console.dirxml('x'); console.trace('t'); console.count('c'); console.count('c');
console.countReset('c'); console.assert(1 === 2, 'boom'); console.time('t1');
console.timeLog('t1'); console.timeEnd('t1'); console.clear(); console.profile();
console.profileEnd(); console.timeStamp(); console.groupEnd(); console.groupEnd();
console.log('ALL_DONE');
`;
  await bridgeCase(script, {
    waitUntil: (evs) => evs.some((e) => e.type === 'log' && String(e.message || '').includes('ALL_DONE')),
    then: (b) => {
      assert.equal(b.exitCode, 0, 'console 全量调用不得让进程异常退出');
      const fatal = b.events().find((e) => e.type === 'event' && e.name === 'fatal');
      assert.ok(!fatal, `不应有 fatal: ${JSON.stringify(fatal)}`);
      const logs = b.logs();
      assert.ok(logs.some((m) => m.includes('info')), 'log/info 应以 info 级别转发');
      assert.ok(logs.some((m) => m.includes('assertion failed')), 'assert 失败应告警');
      assert.ok(logs.some((m) => m.includes('c: 2')), 'count 应累计到 2');
    },
  });
}

async function test_lx_request_json_body_auto_parsed() {
  const script = `
lx.on(lx.EVENT_NAMES.request, () => {});
lx.request(BASE + '/final', {}, (err, resp, body) => {
  console.log('TYPE=' + (body && typeof body));
  if (body && typeof body === 'object') console.log('CODE=' + body.code);
});
`;
  await bridgeCase(inject(script), {
    waitUntil: (evs) => evs.some((e) => String(e.message || '').includes('CODE=')),
    then: (b) => {
      const logs = b.logs();
      assert.ok(logs.some((m) => m.includes('TYPE=object')), 'JSON 响应体必须是对象');
      assert.ok(logs.some((m) => m.includes('CODE=0')), '对象字段可直接访问');
    },
  });
}

async function test_lx_request_plain_body_stays_string() {
  const script = `
lx.on(lx.EVENT_NAMES.request, () => {});
lx.request(BASE + '/plain', {}, (err, resp, body) => {
  console.log('PTYPE=' + typeof body);
});
`;
  await bridgeCase(inject(script), {
    waitUntil: (evs) => evs.some((e) => String(e.message || '').includes('PTYPE=')),
    then: (b) => {
      assert.ok(b.logs().some((m) => m.includes('PTYPE=string')), '非 JSON 响应体必须保持字符串');
    },
  });
}

async function test_redirect_chain_followed() {
  const script = `
lx.on(lx.EVENT_NAMES.request, () => {});
lx.request(BASE + '/redirect301', {}, (err, resp, body) => {
  console.log('STATUS=' + (resp && resp.statusCode) + ' BODY=' + (body && body.code));
});
`;
  await bridgeCase(inject(script), {
    waitUntil: (evs) => evs.some((e) => String(e.message || '').includes('STATUS=')),
    then: (b) => {
      const logs = b.logs();
      assert.ok(logs.some((m) => m.includes('STATUS=200')), '301→302→200 应跟随到最终响应');
      assert.ok(logs.some((m) => m.includes('BODY=0')), '最终 JSON 体自动解析');
    },
  });
}

async function test_303_changes_method_to_get() {
  const script = `
lx.on(lx.EVENT_NAMES.request, () => {});
lx.request(BASE + '/see303', { method: 'POST', body: 'x=1' }, (err, resp, body) => {
  console.log('M=' + (body && body.method));
});
`;
  await bridgeCase(inject(script), {
    waitUntil: (evs) => evs.some((e) => String(e.message || '').includes('M=')),
    then: (b) => {
      assert.ok(b.logs().some((m) => m.includes('M=GET')),
        `303 重定向后必须转为 GET; 实际: ${b.logs().join(' | ')} stderr: ${b.errText.slice(0, 200)}`);
    },
  });
}

async function test_too_many_redirects_errors() {
  const script = `
lx.on(lx.EVENT_NAMES.request, () => {});
lx.request(BASE + '/loop', {}, (err, resp, body) => {
  console.log('ERR=' + (err && err.message));
});
`;
  await bridgeCase(inject(script), {
    waitUntil: (evs) => evs.some((e) => String(e.message || '').includes('ERR=')),
    then: (b) => {
      assert.ok(b.logs().some((m) => m.includes('ERR=too many redirects')),
        '无限重定向必须以 too many redirects 报错');
    },
  });
}

async function test_form_encoded() {
  const script = `
lx.on(lx.EVENT_NAMES.request, () => {});
lx.request(BASE + '/echo-form', { method: 'POST', form: { a: '1', b: 'x y' } }, (err, resp, body) => {
  console.log('CT=' + (body && body.ct) + ' BODY=' + (body && body.body));
});
`;
  await bridgeCase(inject(script), {
    waitUntil: (evs) => evs.some((e) => String(e.message || '').includes('CT=')),
    then: (b) => {
      const logs = b.logs();
      assert.ok(logs.some((m) => m.includes('CT=application/x-www-form-urlencoded')),
        `form 必须按 urlencoded 发送; 实际: ${logs.join(' | ')} stderr: ${b.errText.slice(0, 200)}`);
      assert.ok(logs.some((m) => m.includes('BODY=a=1&b=x+y')), logs.join(' | '));
    },
  });
}

async function test_request_timeout_aborts() {
  const script = `
lx.on(lx.EVENT_NAMES.request, () => {});
lx.request(BASE + '/hang', { timeout: 300 }, (err, resp, body) => {
  console.log('TERR=' + (err ? 'yes' : 'no'));
});
`;
  const started = Date.now();
  await bridgeCase(inject(script), {
    waitUntil: (evs) => evs.some((e) => String(e.message || '').includes('TERR=')),
    timeoutMs: 6000,
    then: (b) => {
      const elapsed = Date.now() - started;
      assert.ok(elapsed < 5000, `300ms 超时应在 5s 内回调，实际 ${elapsed}ms`);
      assert.ok(b.logs().some((m) => m.includes('TERR=yes')), '超时必须以错误回调');
    },
  });
}

async function test_musicurl_protocol_roundtrip() {
  const script = `
lx.on(lx.EVENT_NAMES.request, ({ action, info }) => {
  console.log('GOT=' + action + ':' + info.songmid);
  return 'http://media.test/a.flac';
});
lx.send(lx.EVENT_NAMES.inited, { openAPI: [], platforms: {} });
`;
  await bridgeCase(script, {
    feed: [JSON.stringify({
      id: 'r1', type: 'request', source: 'kw', action: 'musicUrl',
      info: { songmid: '9001', quality: '128k' },
    })],
    waitUntil: (evs) => evs.some((e) => e.id === 'r1'),
    then: (b) => {
      const reply = b.events().find((e) => e.id === 'r1');
      assert.equal(reply.ok, true);
      assert.equal(reply.result, 'http://media.test/a.flac');
      assert.ok(b.logs().some((m) => m.includes('GOT=musicUrl:9001')), 'handler 应收到 source/action/info');
      const inited = b.events().find((e) => e.type === 'event' && e.name === 'inited');
      assert.ok(inited, '加载完成应发出 inited 事件');
    },
  });
}

async function test_ping_pong() {
  await bridgeCase('lx.on(lx.EVENT_NAMES.request, () => {});', {
    feed: ['{"type":"ping"}'],
    waitUntil: (evs) => evs.some((e) => e.type === 'pong'),
  });
}

async function test_utils_smoke() {
  const script = `
const md5 = lx.utils.crypto.md5('abc');
const buf = lx.utils.buffer.bufToString(lx.utils.buffer.from('hello'), 'utf8');
const gz = lx.utils.zlib.gzip('compress-me');
const back = lx.utils.buffer.bufToString(lx.utils.zlib.ungzip(gz));
const key = lx.utils.buffer.from('0123456789abcdef');
const iv = lx.utils.buffer.from('fedcba9876543210');
const enc = lx.utils.crypto.aesEncrypt('secret', 'aes-128-cbc', key, iv);
const dec = lx.utils.buffer.bufToString(lx.utils.crypto.aesDecrypt(enc, 'aes-128-cbc', key, iv));
console.log('MD5=' + md5 + '|BUF=' + buf + '|ZLIB=' + back + '|AES=' + dec);
lx.on(lx.EVENT_NAMES.request, () => {});
`;
  await bridgeCase(script, {
    waitUntil: (evs) => evs.some((e) => String(e.message || '').includes('AES=')),
    then: (b) => {
      const message = b.logs().find((m) => m.includes('MD5='));
      assert.ok(message.includes('MD5=900150983cd24fb0d6963f7d28e17f72'), message);
      assert.ok(message.includes('BUF=hello'), message);
      assert.ok(message.includes('ZLIB=compress-me'), message);
      assert.ok(message.includes('AES=secret'), message);
    },
  });
}

async function test_sandbox_global_whitelist() {
  const script = `
const leaked = [];
for (const name of ['process', 'require', 'Buffer', 'global', 'setTimeout', 'URL']) {
  leaked.push(name + '=' + (typeof globalThis[name] !== 'undefined'));
}
console.log('LEAK=' + leaked.join(','));
lx.on(lx.EVENT_NAMES.request, () => {});
`;
  await bridgeCase(script, {
    waitUntil: (evs) => evs.some((e) => String(e.message || '').includes('LEAK=')),
    then: (b) => {
      const message = b.logs().find((m) => m.includes('LEAK='));
      assert.ok(message.includes('process=false'), message);
      assert.ok(message.includes('require=false'), message);
      assert.ok(message.includes('Buffer=false'), message);
      assert.ok(message.includes('global=true'), 'global 应指向沙箱自身');
      assert.ok(message.includes('setTimeout=true'), message);
      assert.ok(message.includes('URL=true'), message);
    },
  });
}

async function test_missing_request_handler_replies_error() {
  // 脚本不注册 request handler：宿主请求应拿到明确错误而非无响应
  const script = 'lx.send(lx.EVENT_NAMES.inited, {});';
  await bridgeCase(script, {
    feed: [JSON.stringify({ id: 'r2', type: 'request', source: 'kw', action: 'musicUrl', info: {} })],
    waitUntil: (evs) => evs.some((e) => e.id === 'r2'),
    then: (b) => {
      const reply = b.events().find((e) => e.id === 'r2');
      assert.equal(reply.ok, false);
      assert.ok(String(reply.error).includes('did not register'), JSON.stringify(reply));
    },
  });
}

// 固定 1024bit 测试公钥（仅用于验证 rsaEncrypt 算法，无对应私钥分发问题）
const RSA_TEST_PUB = [
  '-----BEGIN PUBLIC KEY-----',
  'MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDicHAMeR7q+Gfj8XsKItVSgZCe',
  'AQr1OC9baofzrjTtH9BAN9pM0VxWrIUhtoeFG1cYkSh8aoCNtioorN+qXeY+xGu9',
  'E4XLq/Q/PrXNOZlY+ZxawDqg+djkeDjVfFEmgkuTrWMl39l7eUsxXIxk9Qx9R6S0',
  'aTp2+NOxQS79tRMCWwIDAQAB',
  '-----END PUBLIC KEY-----',
].join('\n');

async function test_rsa_encrypt_no_padding() {
  const pubLiteral = RSA_TEST_PUB.split('\n').map((l) => `'${l}'`).join(',');
  const script = `
const PUB = [${pubLiteral}].join('\\n') + '\\n';
const a = lx.utils.crypto.rsaEncrypt('test-payload', PUB);
const b = lx.utils.crypto.rsaEncrypt('test-payload', PUB);
console.log('RSA_LEN=' + a.length + ' HEX=' + a.toString('hex') + ' SAME=' + a.equals(b));
lx.on(lx.EVENT_NAMES.request, () => {});
`;
  await bridgeCase(script, {
    waitUntil: (evs) => evs.some((e) => String(e.message || '').includes('RSA_LEN=')),
    then: (b) => {
      const message = b.logs().find((m) => m.includes('RSA_LEN='));
      assert.ok(message.includes('RSA_LEN=128'), message);
      assert.ok(message.includes('SAME=true'),
        'NO_PADDING 无随机填充，同输入必须确定性输出（PKCS1 则每次不同）');
      // 桌面版 preload 算法逐字节复刻：左零填充到 128 字节 + RSA_NO_PADDING
      const payload = Buffer.from('test-payload');
      const expected = crypto.publicEncrypt(
        { key: RSA_TEST_PUB, padding: crypto.constants.RSA_NO_PADDING },
        Buffer.concat([Buffer.alloc(128 - payload.length), payload]),
      ).toString('hex');
      const got = (message.match(/HEX=([0-9a-f]+)/) || [])[1];
      assert.equal(got, expected, 'rsaEncrypt 输出必须与官方 preload 算法一致');
    },
  });
}

async function test_object_body_form_encoded_by_default() {
  const script = `
lx.on(lx.EVENT_NAMES.request, () => {});
lx.request(BASE + '/echo-form', { method: 'POST', body: { a: '1', b: 'x y' } }, (err, resp, body) => {
  console.log('OBJ=' + (body && body.ct) + '&' + (body && body.body));
});
`;
  await bridgeCase(inject(script), {
    waitUntil: (evs) => evs.some((e) => String(e.message || '').includes('OBJ=')),
    then: (b) => {
      const message = b.logs().find((m) => m.includes('OBJ='));
      assert.ok(message.includes('OBJ=application/x-www-form-urlencoded'),
        `needle 默认把 object body 编码为 urlencoded; 实际: ${message}`);
      assert.ok(message.includes('&a=1&b=x+y'), message);
    },
  });
}

async function test_object_body_json_with_content_type() {
  const script = `
lx.on(lx.EVENT_NAMES.request, () => {});
lx.request(BASE + '/echo-form', { method: 'POST',
  headers: { 'Content-Type': 'application/json' }, body: { a: 1 } }, (err, resp, body) => {
  console.log('JSON=' + (body && body.ct) + '&' + (body && body.body));
});
`;
  await bridgeCase(inject(script), {
    waitUntil: (evs) => evs.some((e) => String(e.message || '').includes('JSON=')),
    then: (b) => {
      const message = b.logs().find((m) => m.includes('JSON='));
      assert.ok(message.includes('JSON=application/json&{"a":1}'),
        `显式 json 头时 object body 必须 JSON 序列化; 实际: ${message}`);
    },
  });
}

async function test_sandbox_web_api_available() {
  const script = `
const results = [];
results.push('interval=' + typeof setInterval + ':' + typeof clearInterval);
let ticks = 0;
const timer = setInterval(() => {
  ticks += 1;
  if (ticks >= 2) { clearInterval(timer); done(); }
}, 40);
function done() {
  results.push('ticks=' + ticks);
  results.push('atob=' + atob('aGk='));
  results.push('btoa=' + btoa('hi'));
  results.push('te=' + new TextEncoder().encode('ab').length);
  results.push('td=' + new TextDecoder('utf-8').decode(new Uint8Array([104, 105])));
  results.push('rand=' + crypto.getRandomValues(new Uint8Array(4)).length);
  results.push('perf=' + typeof performance.now);
  results.push('fetch=' + typeof fetch);
  console.log('WEBAPI=' + results.join('|'));
}
lx.on(lx.EVENT_NAMES.request, () => {});
`;
  await bridgeCase(script, {
    timeoutMs: 10000,
    waitUntil: (evs) => evs.some((e) => String(e.message || '').includes('WEBAPI=')),
    then: (b) => {
      const message = b.logs().find((m) => m.includes('WEBAPI='));
      assert.ok(message.includes('interval=function:function'), message);
      assert.ok(message.includes('ticks=2'), `setInterval 必须真实触发; 实际: ${message}`);
      assert.ok(message.includes('atob=hi'), message);
      assert.ok(message.includes('btoa=aGk='), message);
      assert.ok(message.includes('te=2'), message);
      assert.ok(message.includes('td=hi'), message);
      assert.ok(message.includes('rand=4'), message);
      assert.ok(message.includes('perf=function'), message);
      assert.ok(message.includes('fetch=function'), message);
    },
  });
}

async function test_async_crash_before_init_is_fatal() {
  // 官方语义：inited 前任何未捕获错误 = 初始化失败（fatal + 退出码 3）。
  // 不走 bridgeCase：stdin.end() 会触发 rl close 的 exit(0)，抢在 fatal 的
  // 50ms 延迟退出之前，必须让 fatal 自己退出进程。
  const script = `
setTimeout(() => { throw new Error('boom-before-init'); }, 50);
`;
  const b = new BridgeProc(script);
  try {
    const ok = await b.waitUntil((evs) => evs.some((e) => e.type === 'event' && e.name === 'fatal'), 10000);
    assert.ok(ok, '未 inited 前的异步崩溃必须报 fatal; 已收到: ' + b.outLines.slice(-4).join(' | '));
    const code = await Promise.race([
      b.exitPromise,
      delay(5000).then(() => { b.child.kill('SIGKILL'); return -1; }),
    ]);
    assert.equal(code, 3, 'fatal 后应以退出码 3 结束');
    const fatal = b.events().find((e) => e.type === 'event' && e.name === 'fatal');
    assert.ok(String(fatal.payload.error).includes('boom-before-init'),
      JSON.stringify(fatal));
    assert.ok(String(fatal.payload.error).includes('uncaughtException'), JSON.stringify(fatal));
  } finally {
    await b.finish(true);
  }
}

async function test_async_crash_after_init_only_logs() {
  const script = `
lx.on(lx.EVENT_NAMES.request, () => {});
lx.send(lx.EVENT_NAMES.inited, {});
setTimeout(() => { throw new Error('boom-after-init'); }, 50);
`;
  await bridgeCase(script, {
    timeoutMs: 10000,
    waitUntil: (evs) => evs.some((e) => e.type === 'log' && String(e.message || '').includes('boom-after-init')),
    then: (b) => {
      assert.equal(b.exitCode, 0, 'inited 后的未捕获错误只记日志，进程不退出');
      const fatal = b.events().find((e) => e.type === 'event' && e.name === 'fatal');
      assert.ok(!fatal, `不应有 fatal: ${JSON.stringify(fatal)}`);
    },
  });
}

// ------------------------------------------------------------------ 运行 ---

async function main() {
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${server.address().port}`;

  const tests = [
    ['console 全 API 不崩', test_console_full_api_never_crashes],
    ['lx.request JSON 自动转对象', test_lx_request_json_body_auto_parsed],
    ['lx.request 非 JSON 保持字符串', test_lx_request_plain_body_stays_string],
    ['重定向链跟随', test_redirect_chain_followed],
    ['303 转 GET', test_303_changes_method_to_get],
    ['重定向上限报错', test_too_many_redirects_errors],
    ['form urlencoded', test_form_encoded],
    ['请求超时中断', test_request_timeout_aborts],
    ['musicUrl 协议往返', test_musicurl_protocol_roundtrip],
    ['ping/pong', test_ping_pong],
    ['utils 冒烟', test_utils_smoke],
    ['rsaEncrypt NO_PADDING 对齐官方', test_rsa_encrypt_no_padding],
    ['object body 默认 urlencoded', test_object_body_form_encoded_by_default],
    ['object body json 头序列化', test_object_body_json_with_content_type],
    ['沙箱 Web API 可用', test_sandbox_web_api_available],
    ['init 前异步崩溃报 fatal', test_async_crash_before_init_is_fatal],
    ['init 后异步崩溃仅记日志', test_async_crash_after_init_only_logs],
    ['沙箱全局白名单', test_sandbox_global_whitelist],
    ['未注册 handler 明确报错', test_missing_request_handler_replies_error],
  ];

  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log(`ok - ${name}`);
    } catch (err) {
      failed += 1;
      console.error(`not ok - ${name}: ${err && err.message ? err.message : err}`);
    }
  }
  server.close();
  console.log(`\n${tests.length - failed}/${tests.length} passed`);
  process.exit(failed ? 1 : 0);
}

main().catch((err) => {
  console.error('suite crashed:', err);
  process.exit(1);
});
