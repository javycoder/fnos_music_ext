#!/usr/bin/env node
"use strict";

const crypto = require("crypto");
const net = require("net");
const path = require("path");
const bridge = require("./lx_vm_bridge.cjs");

const MAX_RESOLVE_FRAME_BYTES = 64 * 1024;
const MAX_INIT_FRAME_BYTES = 16 * 1024 * 1024;
const MAX_RESOLVE_RESPONSE_BYTES = 64 * 1024;
const MAX_INIT_RESPONSE_BYTES = 16 * 1024 * 1024;
const MAX_SCRIPT_BYTES = 2 * 1024 * 1024;

function parseArgs(argv) {
  const result = {};
  for (let index = 2; index < argv.length; index += 1) {
    const key = argv[index];
    if ((key === "--fd" || key === "--lxserver-dir") && argv[index + 1]) {
      result[key.slice(2)] = argv[index + 1];
      index += 1;
    }
  }
  return result;
}

const args = parseArgs(process.argv);
const fd = Number(args.fd);
const lxserverDir = args["lxserver-dir"] ? path.resolve(args["lxserver-dir"]) : "";
if (!Number.isInteger(fd) || fd < 3 || !lxserverDir) {
  process.stderr.write("source_worker: missing --fd or --lxserver-dir\n");
  process.exit(2);
}

process.title = "fnmusic-source-worker";
process.env.NODE_ENV = process.env.NODE_ENV || "production";
global.lx = global.lx || {};
global.lx.config = Object.assign({}, global.lx.config, {
  "system.allowUnsafeVM": false,
  "debug.enabled": false,
});

const socket = new net.Socket({ fd, readable: true, writable: true });
let readBuffer = "";
let initialized = null;
let currentContext = null;
let currentRequestId = "";
let shuttingDown = false;

function writeMessage(message, maxBytes = MAX_RESOLVE_RESPONSE_BYTES) {
  if (socket.destroyed) return;
  const line = `${JSON.stringify(message)}\n`;
  if (Buffer.byteLength(line, "utf8") > maxBytes) {
    const error = boundedError(
      "response_too_large",
      "ipc",
      message && message.request_id,
      message && message.runtime_id,
      message && message.source_id,
    );
    socket.write(`${JSON.stringify(error)}\n`, "utf8");
    return;
  }
  socket.write(line, "utf8");
}

function boundedError(code, stage, requestId, runtimeId, sourceId) {
  return {
    type: "error",
    protocol: bridge.PROTOCOL_VERSION,
    request_id: requestId || "",
    runtime_id: runtimeId || "",
    source_id: sourceId || "",
    code,
    stage,
  };
}

function clearTimers(context) {
  if (!context) return;
  for (const handle of context.timers) {
    try {
      clearTimeout(handle);
      clearInterval(handle);
    } catch {
      /* best effort */
    }
  }
  context.timers.clear();
}

function abortContext(context) {
  if (!context) return;
  context.cancelled = true;
  try {
    if (context.controller) context.controller.abort();
  } catch {
    /* best effort */
  }
  for (const cancel of context.cancelers) {
    try {
      cancel();
    } catch {
      /* best effort */
    }
  }
  context.cancelers.clear();
  clearTimers(context);
  if (context.cancelResolve) context.cancelResolve(Symbol.for("cancelled"));
}

function requestContext() {
  let cancelResolve;
  const cancelPromise = new Promise((resolve) => {
    cancelResolve = resolve;
  });
  const context = {
    controller: new AbortController(),
    timers: new Set(),
    cancelers: new Set(),
    cancelled: false,
    cancelPromise,
    cancelResolve,
  };
  currentContext = context;
  return context;
}

async function runResolve(message) {
  const requestId = String(message.request_id || "");
  const runtimeId = String(message.runtime_id || "");
  const sourceId = String(message.source_id || "");
  if (!initialized) {
    writeMessage(boundedError("source_not_initialized", "resolve", requestId, runtimeId, sourceId));
    return;
  }
  if (runtimeId !== initialized.runtimeId || sourceId !== initialized.sourceId) {
    writeMessage(boundedError("runtime_identity_mismatch", "resolve", requestId, runtimeId, sourceId));
    return;
  }
  if (currentRequestId) {
    writeMessage(boundedError("source_process_busy", "resolve", requestId, runtimeId, sourceId));
    return;
  }

  const budgetMs = Math.max(1, Math.min(300_000, Number(message.budget_ms) || 1));
  const context = requestContext();
  currentRequestId = requestId;
  let callbackSeen = false;
  let callbackValue;
  let callbackResolve;
  const callbackPromise = new Promise((resolve) => {
    callbackResolve = resolve;
  });
  const callback = (value) => {
    if (context.cancelled || callbackSeen) return;
    callbackSeen = true;
    callbackValue = value;
    callbackResolve(value);
  };

  let timer = null;
  let invocationSettled = false;
  try {
    bridge.setActiveContext(context);
    const invocation = Promise.resolve().then(() => initialized.invoke({
      platform: String(message.platform || ""),
      quality: String(message.quality || ""),
      songInfo: message.song_info,
      callback,
    }));
    const execution = invocation.then(
      (value) => {
        invocationSettled = true;
        return { kind: "return", value };
      },
      (error) => {
        invocationSettled = true;
        return { kind: "error", error };
      },
    );
    const timeout = new Promise((resolve) => {
      timer = setTimeout(() => resolve(Symbol.for("timeout")), budgetMs);
      timer.unref?.();
    });

    const timeoutToken = Symbol.for("timeout");
    const cancelledToken = Symbol.for("cancelled");
    let outcome = await Promise.race([
      execution,
      callbackPromise.then((value) => ({ kind: "callback", value })),
      timeout,
      context.cancelPromise.then(() => cancelledToken),
    ]);
    if (outcome && outcome.kind === "return" && bridge.normalizeResult(outcome.value) == null) {
      outcome = await Promise.race([
        callbackPromise.then((value) => ({ kind: "callback", value })),
        timeout,
        context.cancelPromise.then(() => cancelledToken),
      ]);
    }
    if (outcome === cancelledToken || context.cancelled) {
      if (!invocationSettled) context.awaitingSettle = true;
      return;
    }
    if (outcome === timeoutToken) {
      abortContext(context);
      writeMessage(boundedError("resolve_timeout", "request", requestId, runtimeId, sourceId));
      if (!invocationSettled) context.awaitingSettle = true;
      return;
    }
    if (outcome && outcome.kind === "error") {
      if (!callbackSeen) {
        writeMessage(boundedError("source_script_failed", "request", requestId, runtimeId, sourceId));
        return;
      }
    }
    const result = bridge.normalizeResult(outcome && outcome.value)
      || (callbackSeen ? bridge.normalizeResult(callbackValue) : null);
    if (context.cancelled) {
      return;
    }
    if (!result || !/^https?:\/\//i.test(result.url || "")) {
      writeMessage(boundedError("source_returned_no_url", "request", requestId, runtimeId, sourceId));
      return;
    }
    const response = {
      type: "result",
      protocol: bridge.PROTOCOL_VERSION,
      request_id: requestId,
      runtime_id: runtimeId,
      source_id: sourceId,
      ok: true,
      result,
    };
    if (Buffer.byteLength(JSON.stringify(response), "utf8") > MAX_RESOLVE_RESPONSE_BYTES) {
      writeMessage(boundedError("response_too_large", "request", requestId, runtimeId, sourceId));
      return;
    }
    writeMessage(response);
    abortContext(context);
    if (!invocationSettled) context.awaitingSettle = true;
  } finally {
    if (timer) clearTimeout(timer);
    if (context.awaitingSettle) {
      Promise.resolve()
        .then(() => new Promise((resolve) => {
          const poll = () => {
            if (invocationSettled) resolve();
            else setTimeout(poll, 25).unref?.();
          };
          poll();
        }))
        .finally(() => {
          bridge.clearActiveContext(context);
          abortContext(context);
          if (currentContext === context) currentContext = null;
          if (currentRequestId === requestId) currentRequestId = "";
          if (!shuttingDown) {
            writeMessage({
              type: "settled",
              protocol: bridge.PROTOCOL_VERSION,
              request_id: requestId,
              runtime_id: runtimeId,
              source_id: sourceId,
            });
          }
        });
    } else {
      bridge.clearActiveContext(context);
      abortContext(context);
      if (currentContext === context) currentContext = null;
      if (currentRequestId === requestId) currentRequestId = "";
    }
  }
}

async function handleInit(message) {
  if (initialized) {
    writeMessage(boundedError("already_initialized", "init", message.request_id, message.runtime_id, message.source_id));
    return;
  }
  if (Number(message.protocol) !== bridge.PROTOCOL_VERSION) {
    writeMessage(boundedError("protocol_mismatch", "init", message.request_id, message.runtime_id, message.source_id));
    return;
  }
  const sourceId = String(message.source_id || "");
  const runtimeId = String(message.runtime_id || "");
  if (typeof message.script !== "string" || Buffer.byteLength(message.script, "utf8") > MAX_SCRIPT_BYTES) {
    writeMessage(boundedError("script_too_large", "init", message.request_id, runtimeId, sourceId));
    return;
  }
  try {
    const loader = bridge.createLoader(lxserverDir);
    initialized = await loader.load({
      sourceId,
      runtimeId,
      filename: String(message.filename || sourceId),
      script: message.script,
    });
    const response = {
      type: "ready",
      protocol: bridge.PROTOCOL_VERSION,
      request_id: String(message.request_id || ""),
      runtime_id: runtimeId,
      source_id: sourceId,
      ok: true,
      metadata: initialized.metadata,
      platforms: initialized.platforms,
      qualities: initialized.qualities,
      upstream_sha256: initialized.upstreamSha256,
    };
    const bytes = Buffer.byteLength(JSON.stringify(response), "utf8");
    if (bytes > MAX_INIT_RESPONSE_BYTES) {
      writeMessage(boundedError("init_response_too_large", "init", message.request_id, runtimeId, sourceId));
      return;
    }
    writeMessage(response, MAX_INIT_RESPONSE_BYTES);
  } catch (error) {
    const code = error && error.code ? String(error.code) : "initialization_failed";
    const response = {
      ...boundedError(code, "init", message.request_id, runtimeId, sourceId),
      error_name: bridge.sanitizeError(error).name,
    };
    if (error && error.detail && typeof error.detail === "object") {
      response.error_name = String(error.detail.name || response.error_name).slice(0, 80);
    }
    writeMessage(response);
  }
}

function handleCancel(message) {
  const requestId = String(message.request_id || "");
  const runtimeId = String(message.runtime_id || "");
  const sourceId = String(message.source_id || "");
  if (!initialized || runtimeId !== initialized.runtimeId || sourceId !== initialized.sourceId) {
    writeMessage(boundedError("runtime_identity_mismatch", "cancel", requestId, runtimeId, sourceId));
    return;
  }
  if (requestId && requestId === currentRequestId) abortContext(currentContext);
  writeMessage({
    type: "cancel_ack",
    protocol: bridge.PROTOCOL_VERSION,
    request_id: requestId,
    runtime_id: runtimeId,
    source_id: sourceId,
  });
}

function dispatch(line) {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    writeMessage(boundedError("invalid_json", "ipc", "", "", ""));
    return;
  }
  const frameLimit = message && message.type === "init"
    ? MAX_INIT_FRAME_BYTES
    : MAX_RESOLVE_FRAME_BYTES;
  if (Buffer.byteLength(line, "utf8") > frameLimit) {
    writeMessage(boundedError(
      message && message.type === "init" ? "init_frame_too_large" : "request_too_large",
      "ipc",
      message && message.request_id,
      message && message.runtime_id,
      message && message.source_id,
    ));
    return;
  }
  if (!message || Number(message.protocol) !== bridge.PROTOCOL_VERSION) {
    writeMessage(boundedError("protocol_mismatch", "ipc", message && message.request_id, message && message.runtime_id, message && message.source_id));
    return;
  }
  if (message.type === "init") {
    handleInit(message).catch(() => {
      writeMessage(boundedError("initialization_failed", "init", message.request_id, message.runtime_id, message.source_id));
    });
  } else if (message.type === "resolve") {
    runResolve(message).catch(() => {
      writeMessage(boundedError("internal_error", "resolve", message.request_id, message.runtime_id, message.source_id));
    });
  } else if (message.type === "cancel") {
    handleCancel(message);
  } else if (message.type === "shutdown") {
    shuttingDown = true;
    abortContext(currentContext);
    writeMessage({
      type: "shutdown_ack",
      protocol: bridge.PROTOCOL_VERSION,
      request_id: String(message.request_id || ""),
      runtime_id: String(message.runtime_id || ""),
      source_id: String(message.source_id || ""),
    });
    setTimeout(() => {
      socket.end();
      process.exit(0);
    }, 10).unref();
  } else {
    writeMessage(boundedError("unknown_operation", "ipc", message.request_id, message.runtime_id, message.source_id));
  }
}

socket.setEncoding("utf8");
socket.on("data", (chunk) => {
  readBuffer += chunk;
  if (Buffer.byteLength(readBuffer, "utf8") > MAX_INIT_RESPONSE_BYTES) {
    writeMessage(boundedError("ipc_buffer_overflow", "ipc", "", "", ""));
    socket.destroy();
    return;
  }
  let newline;
  while ((newline = readBuffer.indexOf("\n")) >= 0) {
    const line = readBuffer.slice(0, newline);
    readBuffer = readBuffer.slice(newline + 1);
    if (line.trim()) dispatch(line);
  }
});
socket.on("error", () => {
  process.exit(1);
});
socket.on("close", () => {
  if (!shuttingDown) process.exit(0);
});

process.on("uncaughtException", () => {
  const requestId = currentRequestId;
  writeMessage(boundedError("uncaught_exception", "worker", requestId, initialized?.runtimeId, initialized?.sourceId));
  setTimeout(() => process.exit(1), 10).unref();
});
process.on("unhandledRejection", () => {
  const requestId = currentRequestId;
  writeMessage(boundedError("unhandled_rejection", "worker", requestId, initialized?.runtimeId, initialized?.sourceId));
});
