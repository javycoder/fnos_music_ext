"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const PROTOCOL_VERSION = 1;
const MAX_SCRIPT_BYTES = 2 * 1024 * 1024;

let activeContext = null;

function setActiveContext(context) {
  activeContext = context || null;
}

function clearActiveContext(context) {
  if (activeContext === context) activeContext = null;
}

function parseScriptMetadata(script) {
  const meta = { name: "", description: "", version: "1.0.0", author: "", homepage: "" };
  const block = String(script || "").match(/\/\*[*!]([\s\S]*?)\*\//);
  if (!block) return meta;
  for (const line of block[1].split(/\r?\n/)) {
    const match = line.trim().match(/^@(\w+)\s+(.+?)(?=\s+@\w+|$)/);
    if (!match) continue;
    const key = match[1].toLowerCase();
    const value = match[2].trim();
    if (key === "repository") meta.homepage = value;
    else if (key in meta) meta[key] = value;
  }
  return meta;
}

function byteLength(value) {
  try {
    return Buffer.byteLength(String(value || ""), "utf8");
  } catch {
    return Number.MAX_SAFE_INTEGER;
  }
}

function safeName(value, fallback) {
  const text = String(value || "").trim();
  return text || fallback;
}

function safeSourceId(name, filename) {
  let input = String(name || filename || "source");
  try {
    input = decodeURIComponent(input);
  } catch {
    /* keep original */
  }
  let base = path.basename(input);
  if (base.toLowerCase().endsWith(".js")) base = base.slice(0, -3);
  return `${base.replace(/[\\/:*?"<>|]/g, "_").trim() || "source"}.js`;
}

function toBuffer(value) {
  if (Buffer.isBuffer(value)) return Buffer.from(value);
  if (value instanceof Uint8Array) return Buffer.from(Uint8Array.from(value));
  if (Array.isArray(value)) return Buffer.from(value);
  if (typeof value === "string") return Buffer.from(value);
  try {
    return Buffer.from(Uint8Array.from(value));
  } catch {
    return Buffer.alloc(0);
  }
}

function sanitizeError(error) {
  const name = error && error.name ? String(error.name).slice(0, 80) : "Error";
  let message = error && error.message ? String(error.message) : String(error || "");
  message = message
    .replace(/https?:\/\/[^\s"'<>]+/gi, "[url]")
    .replace(/\b(?:authorization|cookie|token|key|signature)=?[^\s&;]+/gi, "$1=[redacted]")
    .replace(/[\r\n\t]+/g, " ")
    .slice(0, 240);
  return { name, message };
}

function mapSongInfo(input) {
  const info = input && typeof input === "object" ? { ...input } : {};
  const meta = input && input.meta && typeof input.meta === "object" ? input.meta : null;
  if (meta) {
    Object.assign(info, meta);
    if (meta.songId && !info.songmid) info.songmid = meta.songId;
    if (meta.picUrl && !info.img) info.img = meta.picUrl;
    if (meta.qualitys && !info.types) info.types = meta.qualitys;
    if (meta._qualitys && !info._types) info._types = meta._qualitys;
    for (const key of [
      "hash", "albumId", "copyrightId", "lrcUrl", "mrcUrl", "trcUrl",
      "strMediaMid", "albumMid",
    ]) {
      if (meta[key] && !info[key]) info[key] = meta[key];
    }
  }
  for (const key of [
    "hash", "copyrightId", "strMediaMid", "albumMid", "albumId",
    "lrcUrl", "mrcUrl", "trcUrl",
  ]) {
    if (!info[key] && input && input[key]) info[key] = input[key];
  }
  if (typeof info.hash === "string" && !info.hash) delete info.hash;
  return info;
}

function normalizeResult(value) {
  if (value == null) return null;
  if (typeof value === "string") {
    const url = value.trim();
    return url ? { url } : null;
  }
  if (typeof value === "object") {
    if (typeof value.url === "string" && value.url.trim()) return { url: value.url.trim() };
    if (typeof value.data === "string" && value.data.trim().startsWith("http")) {
      return { url: value.data.trim() };
    }
    if (value.data && typeof value.data === "object") return normalizeResult(value.data);
  }
  return null;
}

function installVmCompat(lxserverDir) {
  const root = path.resolve(lxserverDir);
  const vm2Path = path.join(root, "node_modules", "vm2");
  const vm2 = require(vm2Path);
  if (vm2.__fnmusicCompatInstalled) return vm2;

  const OriginalVM = vm2.VM;
  class FnMusicCompatVM extends OriginalVM {
    constructor(options = {}) {
      const sandbox = options.sandbox || {};
      const hostFetch = (...args) => globalThis.fetch(...args);
      sandbox.fetch = (input, init = {}) => {
        const url = typeof input === "string" ? input : String(input && input.url ? input.url : input);
        if (!url || !/^https?:\/\//i.test(url)) {
          return Promise.reject(new Error("unsupported fetch target"));
        }
        const options = { ...(init && typeof init === "object" ? init : {}) };
        const context = activeContext;
        const controller = context && context.controller;
        if (controller) {
          if (options.signal && typeof AbortSignal !== "undefined" && AbortSignal.any) {
            options.signal = AbortSignal.any([options.signal, controller.signal]);
          } else {
            options.signal = controller.signal;
          }
        }
        return hostFetch(url, options);
      };
      sandbox.performance = globalThis.performance;
      sandbox.queueMicrotask = (fn) => globalThis.queueMicrotask(fn);
      sandbox.setImmediate = (fn, ...args) => globalThis.setImmediate(fn, ...args);

      const trackTimer = (kind) => (fn, delay, ...args) => {
        const contextRef = activeContext;
        let handle;
        const wrapped = (...callArgs) => {
          if (contextRef) contextRef.timers.delete(handle);
          if (typeof fn === "function") fn(...callArgs);
        };
        handle = kind === "interval"
          ? globalThis.setInterval(wrapped, delay, ...args)
          : globalThis.setTimeout(wrapped, delay, ...args);
        if (contextRef) contextRef.timers.add(handle);
        return handle;
      };
      sandbox.setTimeout = trackTimer("timeout");
      sandbox.setInterval = trackTimer("interval");
      sandbox.clearTimeout = (handle) => {
        if (activeContext) activeContext.timers.delete(handle);
        return globalThis.clearTimeout(handle);
      };
      sandbox.clearInterval = (handle) => {
        if (activeContext) activeContext.timers.delete(handle);
        return globalThis.clearInterval(handle);
      };

      if (sandbox.lx && sandbox.lx.utils && sandbox.lx.utils.crypto) {
        sandbox.lx.utils.crypto.aesEncrypt = (buffer, mode, key, iv) => {
          const keyBuffer = toBuffer(key);
          let algorithm = String(mode || "cbc").trim().toLowerCase();
          if (!/^aes-\d{3}-/.test(algorithm)) {
            algorithm = `aes-${keyBuffer.length * 8}-${algorithm}`;
          }
          const ivBuffer = iv == null || /-ecb$/i.test(algorithm) ? null : toBuffer(iv);
          const cipher = crypto.createCipheriv(algorithm, keyBuffer, ivBuffer);
          return Buffer.concat([cipher.update(toBuffer(buffer)), cipher.final()]);
        };
      }
      if (sandbox.lx && typeof sandbox.lx.request === "function") {
        const originalRequest = sandbox.lx.request;
        sandbox.lx.request = (...args) => {
          const context = activeContext;
          const cancel = originalRequest(...args);
          if (context && typeof cancel === "function") context.cancelers.add(cancel);
          return cancel;
        };
      }
      super({ ...options, sandbox });
    }
  }
  vm2.VM = FnMusicCompatVM;
  Object.defineProperty(vm2, "__fnmusicCompatInstalled", { value: true });
  return vm2;
}

function createLoader(lxserverDir) {
  const root = path.resolve(lxserverDir);
  const userApiPath = path.join(root, "server", "server", "sources", "userApi.js");
  if (!fs.existsSync(userApiPath)) throw new Error("USER_API_MODULE_MISSING");
  const beforeSha = crypto.createHash("sha256").update(fs.readFileSync(userApiPath)).digest("hex");
  installVmCompat(root);
  const userApi = require(userApiPath);
  if (typeof userApi.loadUserApi !== "function" || typeof userApi.extractMetadata !== "function") {
    throw new Error("USER_API_EXPORTS_INVALID");
  }

  return {
    userApiPath,
    beforeSha,
    async load({ sourceId, runtimeId, filename, script }) {
      if (typeof script !== "string" || !script.trim()) throw new Error("SCRIPT_EMPTY");
      if (byteLength(script) > MAX_SCRIPT_BYTES) throw new Error("SCRIPT_TOO_LARGE");
      const metadata = userApi.extractMetadata(script);
      const displayName = safeName(metadata.name, filename || sourceId);
      const result = await userApi.loadUserApi({
        id: sourceId,
        name: displayName,
        description: metadata.description,
        version: metadata.version || "1.0.0",
        author: metadata.author,
        homepage: metadata.homepage,
        script,
        enabled: false,
        allowUnsafeVM: false,
        owner: "fnmusic",
      });
      if (!result || !result.success || !result.apiInstance) {
        const raw = String((result && result.error) || "SOURCE_INIT_FAILED");
        if ((result && result.requireUnsafe)
            || raw === "REQUIRE_UNSAFE_VM" || raw === "VM_DISABLED"
            || /vm2\s*安全限制|contextified object|Operation not allowed|REQUIRE_UNSAFE_VM/i.test(raw)) {
          const error = new Error("UNSAFE_VM_REQUIRED");
          error.code = "unsafe_vm_required";
          throw error;
        }
        const error = new Error("SOURCE_INIT_FAILED");
        error.code = "initialization_failed";
        error.detail = sanitizeError(result && result.error ? result.error : raw);
        throw error;
      }
      const api = result.apiInstance;
      const sourceSpec = api.info && api.info.sources && typeof api.info.sources === "object"
        ? api.info.sources : {};
      const platforms = Object.keys(sourceSpec).filter(Boolean);
      const qualities = {};
      for (const platform of platforms) {
        const qualityList = sourceSpec[platform] && Array.isArray(sourceSpec[platform].qualitys)
          ? sourceSpec[platform].qualitys.map(String).filter(Boolean)
          : [];
        if (qualityList.length) qualities[platform] = qualityList;
      }
      const handler = api.handlers && api.handlers.get ? api.handlers.get("request") : null;
      if (typeof handler !== "function") throw new Error("REQUEST_HANDLER_MISSING");
      const info = api.info || {};
      const afterSha = crypto.createHash("sha256").update(fs.readFileSync(userApiPath)).digest("hex");
      if (beforeSha !== afterSha) throw new Error("UPSTREAM_MODIFIED");
      return {
        api,
        handler,
        async invoke({ platform, quality, songInfo, callback }) {
          return handler({
            action: "musicUrl",
            source: String(platform || ""),
            info: {
              musicInfo: mapSongInfo(songInfo),
              quality: String(quality || ""),
              type: String(quality || ""),
            },
            callback,
          }, callback);
        },
        metadata: {
          name: safeName(info.name, displayName),
          version: String(info.version || metadata.version || "1.0.0"),
          author: String(info.author || metadata.author || ""),
          description: String(info.description || metadata.description || ""),
          homepage: String(info.homepage || metadata.homepage || ""),
        },
        platforms,
        qualities,
        upstreamSha256: afterSha,
        sourceId,
        runtimeId,
      };
    },
  };
}

module.exports = {
  PROTOCOL_VERSION,
  MAX_SCRIPT_BYTES,
  createLoader,
  installVmCompat,
  clearActiveContext,
  mapSongInfo,
  normalizeResult,
  parseScriptMetadata,
  safeSourceId,
  sanitizeError,
  setActiveContext,
};
