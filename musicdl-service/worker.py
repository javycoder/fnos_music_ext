"""musicdl 单源 worker 子进程：一进程一音源，一次一任务。

JSON 行协议（UTF-8）：
  stdin   <- {"op": "search", "source": ..., "keyword": ..., "fetch_size": ...,
              "limit": ..., "song_id": str|null, "budget_s": float}
  stdout  -> {"type": "ready"}
          -> {"type": "entry", "entry": [item, download_headers, lyric]}  确认一首交一首
          -> {"type": "result", "count": n, "partial": bool}
          -> {"type": "error", "error": "TypeName: message"}

输出通道约定：进程启动后先把 fd 1 整体改道 stderr（dup2），第三方库的任何
print/日志都进独立日志通道（父进程读取后统一落盘、脱敏）；协议走 dup 出来的
原始 stdout，单写者单方向，保证协议流永不被库输出污染。
任务级失败只上报不断进程；卡死的任务由父进程超时后 TERM/KILL 整个进程回收。
"""
import json
import os
import sys
import threading


def _emit(proto_fd: int, message: dict) -> None:
    data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    view = memoryview(data)
    while view:
        view = view[os.write(proto_fd, view):]


def main() -> int:
    proto_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    import logging
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logging.getLogger("musicdl").setLevel(logging.ERROR)

    # 库 import 可能产生输出，必须在改道之后进行
    import search_core

    _emit(proto_fd, {"type": "ready"})
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            task = json.loads(raw)
            if not isinstance(task, dict):
                raise ValueError("task must be a JSON object")
        except ValueError as exc:
            _emit(proto_fd, {"type": "error", "error": "invalid task"})
            continue
        try:
            result = search_core.run_search_task(
                task, lambda entry: _emit(proto_fd, {"type": "entry", "entry": entry}))
            _emit(proto_fd, {"type": "result",
                             "count": int(result.get("count") or 0),
                             "partial": bool(result.get("partial")) or any(
                                 t.is_alive() and t is not threading.current_thread()
                                 for t in threading.enumerate())})
        except Exception as exc:  # noqa: BLE001 任务级失败上报后继续服务下一个任务
            _emit(proto_fd, {"type": "error", "error": "task failed"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
