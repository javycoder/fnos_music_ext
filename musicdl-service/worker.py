"""musicdl 单源 worker 子进程：一进程一音源，一次一任务。

JSON 行协议（UTF-8）：
  stdin   <- {"op": "search", "source": ..., "keyword": ..., "fetch_size": ...,
              "limit": ..., "song_id": str|null, "budget_s": float}
  stdout  -> {"type": "ready"}
          -> {"type": "entry", "entry": [item, download_headers, lyric]}  确认一首交一首
          -> {"type": "result", "count": n, "partial": bool}
          -> {"type": "error", "error": "TypeName (stage=search)"}

输出通道约定：进程启动后先把fd1改道stderr，父进程按小块排空并丢弃库输出，
不把任意SDK打印内容当作安全诊断。协议走保留的原始stdout，由单一写入方输出，
避免第三方print或日志污染JSON行。
任务级失败只上报不断进程；卡死的任务由父进程超时后 TERM/KILL 整个进程回收。

错误协议只输出异常类型、原因类型和阶段，不传自由正文。媒体条目及必要私有请求头
另走受控数据协议，不能与可公开的诊断文本混用。
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
            # 记录实际进入过的阶段；失败时用它给出可定位的 search/probe 标记。
            stages: list = []
            result = search_core.run_search_task(
                task, lambda entry: _emit(proto_fd, {"type": "entry", "entry": entry}),
                on_stage=stages.append)
            _emit(proto_fd, {"type": "result",
                             "count": int(result.get("count") or 0),
                             "partial": bool(result.get("partial")) or any(
                                 t.is_alive() and t is not threading.current_thread()
                                 for t in threading.enumerate())})
        except Exception as exc:  # noqa: BLE001 任务级失败上报后继续服务下一个任务
            # 阶段取最后一次进入的阶段；异常自带 stage 时优先用它。
            stage = getattr(exc, "stage", None) or (stages[-1] if stages else "unspecified")
            _emit(proto_fd, {"type": "error",
                             "error": search_core.describe_task_error(exc, stage)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
