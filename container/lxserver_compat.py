#!/usr/bin/env python3
"""lx-music-sync-server v2.1.2 用户源沙箱兼容补丁（构建期应用）。

上游 zip（Dockerfile 校验 sha256 后解压）的脚本沙箱与 lx-music 桌面端存在三处
协议/环境差异，导致按桌面端契约编写的自定义源脚本（如 pdone/lx-music-source
的六音/sixyin）在 lxserver 内无法解析直链：

1. 沙箱缺现代全局：fetch/performance/queueMicrotask/setImmediate —— 新脚本
   （混淆产物普遍 feature-detect fetch）在这些缺口上直接抛
   "_0x… is not a function"，整个平台分支瘫痪；
2. callRequest 未按桌面端协议在 request 事件载荷里携带 callback —— 回调式
   脚本 resolve(undefined)，lxserver 判"成功"却拿不到直链；
3. lxUtils.crypto.aesEncrypt 只认裸模式名（cbc/ecb），脚本传完整算法名
   （如 "aes-128-ecb"）时被拼成 "aes-128-aes-128-ecb" → Unknown cipher；
   且 ECB 档 iv 为 undefined 时未放行为 null。

用法：python3 lxserver_compat.py [userApi.js 路径]
幂等：已应用（或锚点不存在）时跳过；锚点失配说明上游结构变化，直接报错中止
构建，避免带着残缺补丁发布。
"""

from __future__ import annotations

import sys
from pathlib import Path

DEFAULT_PATH = "/srv/lxserver/server/server/userApi.js"

# ---- 补丁 1：沙箱补全现代全局（fetch/performance/queueMicrotask/setImmediate） ----
ANCHOR_BTOA = "        btoa: (s) => Buffer.from(s, 'binary').toString('base64'),"
PATCH_GLOBALS = ANCHOR_BTOA + """
        fetch: (input, init) => fetch(decontextify(input), decontextify(init)),
        performance: typeof performance !== 'undefined' ? performance : { now: () => Date.now() },
        queueMicrotask: (fn) => queueMicrotask(fn),
        setImmediate: (fn, ...args) => setImmediate(fn, ...args),"""

# ---- 补丁 2：callRequest 按桌面端协议携带 callback，回调式脚本取回调值 ----
ANCHOR_CALL_OLD = """                    const result = await handler(inputData);
                    return decontextify(result);"""
ANCHOR_CALL_NEW = """                    // lx-music 桌面端协议：request 事件载荷含 callback，回调式脚本
                    // 不靠返回值交直链。返回值为空时回退取 callback 收到的值。
                    const captured = { has: false, value: undefined };
                    inputData.callback = (data) => { captured.has = true; captured.value = data; };
                    const result = await handler(inputData);
                    const retVal = decontextify(result);
                    const cbValue = captured.has ? decontextify(captured.value) : undefined;
                    return (retVal !== undefined && retVal !== null && retVal !== '') ? retVal : cbValue;"""

# ---- 补丁 3：aesEncrypt 兼容完整算法名与字符串参数/ECB 无 IV ----
ANCHOR_AES_OLD = """            aesEncrypt: (buffer, mode, key, iv) => {
                const dKey = decontextify(key);
                const dIv = decontextify(iv);
                const dBuffer = decontextify(buffer);
                const algorithm = `aes-${dKey.length * 8}-${mode}`;
                const cipher = crypto.createCipheriv(algorithm, dKey, dIv);
                return Buffer.concat([cipher.update(dBuffer), cipher.final()]);
            },"""
ANCHOR_AES_NEW = """            aesEncrypt: (buffer, mode, key, iv) => {
                const dKey = decontextify(key);
                const dIv = decontextify(iv);
                const dBuffer = decontextify(buffer);
                // mode 兼容两种形态：裸模式名（cbc/ecb，按 key 长度推导位数）与
                // 完整算法名（如 aes-128-ecb，六音等脚本直接传完整名）
                let algorithm = String(decontextify(mode) || '');
                if (!/^aes-\\d{3}-/.test(algorithm)) algorithm = `aes-${dKey.length * 8}-${algorithm}`;
                const cipher = crypto.createCipheriv(algorithm, dKey, dIv == null ? null : dIv);
                return Buffer.concat([cipher.update(dBuffer), cipher.final()]);
            },"""


def apply(text: str) -> str:
    changed = False
    for marker, old, new in (
        ("globals", ANCHOR_BTOA, PATCH_GLOBALS),
        ("callback", ANCHOR_CALL_OLD, ANCHOR_CALL_NEW),
        ("aesEncrypt", ANCHOR_AES_OLD, ANCHOR_AES_NEW),
    ):
        if new in text:
            continue  # 已应用（新块完整存在；globals 补丁的新块包含原锚点属正常）
        if old not in text:
            raise SystemExit(f"ERROR: lxserver 兼容补丁[{marker}]锚点失配，上游 userApi.js 结构已变化，请人工核对")
        text = text.replace(old, new, 1)
        changed = True
    if changed:
        print("lxserver_compat: 已应用用户源沙箱兼容补丁")
    else:
        print("lxserver_compat: 补丁均已就绪，跳过")
    return text


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH)
    text = path.read_text(encoding="utf-8")
    patched = apply(text)
    if patched != text:
        path.write_text(patched, encoding="utf-8")


if __name__ == "__main__":
    main()
