# -*- coding: utf-8 -*-
"""谈案助手（基于 voiceapi 改造）

P0 范围：实时转写（本地 SenseVoice）+ 必问清单零延迟提醒。
不做：说话人分离、当事人画像、TTS。
"""
import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import numpy as np
import soundfile as sf
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from voiceapi.asr import ASRResult, ASRStream, process_asr_file_streaming, start_asr_stream
from voiceapi.session import Session

logger = logging.getLogger(__name__)
app = FastAPI(title="谈案助手")

ROOT = Path(__file__).parent
DATA = ROOT / "data"
CONSULT = ROOT / "consultations"
CONSULT.mkdir(exist_ok=True)
FFMPEG = os.environ.get("VC_FFMPEG", "ffmpeg")

CHECKLISTS = json.loads((DATA / "checklists.json").read_text(encoding="utf-8"))
CASES = CHECKLISTS["caseTypes"]
try:
    ASKS = json.loads((DATA / "asks.json").read_text(encoding="utf-8"))
except Exception:                             # noqa: BLE001
    logger.warning("data/asks.json 读不到，话题追问降级成按缺口排序")
    ASKS = {}

args = None                      # 由 __main__ 填入
current: Optional[Session] = None
current_lock = threading.Lock()
import_state = {"running": False, "ready": False, "total": 0, "done": 0, "error": "", "segments": 0}


# ------------------------------------------------------------------ 场次
def _retire(s: Optional[Session]) -> None:
    """关掉旧场次。正在录音的那个不能关——关了录音连接就 ValueError: seek of closed file。

    录音连接自己攥着这个 Session，它会继续把声音写进自己的目录，
    等连接断开时再由 websocket_asr 的 finally 收尾。
    """
    if s is None:
        return
    if s.recording:
        logger.warning("session: %s 还在录音，先不关（新场次另开目录）", s.dir.name)
        s.save()
        return
    s.close()


def new_session(case_id: str, case_name: str) -> Session:
    global current
    with current_lock:
        _retire(current)
        current = Session(CONSULT, case_id, case_name, CASES[case_id], ASKS.get(case_id))
        logger.info("session: new %s (%s)", current.dir.name, case_name)
        return current


def load_latest() -> Optional[Session]:
    """重启后把最近一场接回来，别丢正在谈的案子。"""
    # 优先恢复真有内容的场次（空场次是自动开出来还没用过的）
    def has_content(d: Path) -> bool:
        try:
            return bool(json.loads((d / "transcript.json").read_text(encoding="utf-8")))
        except Exception:
            return False

    dirs = sorted([d for d in CONSULT.iterdir() if d.is_dir()], key=lambda d: d.name)
    dirs = [d for d in dirs if has_content(d)] or dirs
    for d in reversed(dirs[-5:]):
        case_id = d.name.rsplit("-", 1)[-1]
        if case_id in CASES:
            try:
                s = Session.load(d, case_id, CASES[case_id]["name"], CASES[case_id],
                                 ASKS.get(case_id))
                logger.info("session: restored %s (%d 段)", d.name, len(s.segments))
                return s
            except Exception as exc:              # noqa: BLE001
                logger.warning("session: restore %s failed: %s", d.name, exc)
    return None


def ensure_session() -> Session:
    """首屏没有场次：先试恢复最近一场，恢复不了就开新的。"""
    global current
    with current_lock:
        if current is None:
            current = load_latest()
        if current is None:
            case_id = "labor" if "labor" in CASES else list(CASES)[0]
            current = Session(CONSULT, case_id, CASES[case_id]["name"], CASES[case_id],
                              ASKS.get(case_id))
        return current


@app.get("/api/state")
async def api_state():
    s = ensure_session()
    st = s.state()
    st["asrModel"] = getattr(args, "asr_model", "")
    st["import"] = dict(import_state)
    return st


@app.post("/api/reset")
async def api_reset(payload: dict):
    case_id = payload.get("case") or "labor"
    if case_id not in CASES:
        raise HTTPException(400, "unknown case: " + case_id)
    s = new_session(case_id, CASES[case_id]["name"])
    return {"ok": True, "state": s.state()}


@app.get("/api/cases")
async def api_cases():
    return [{"id": k, "name": v["name"],
             "groups": [{"id": g["id"], "name": g["name"], "count": len(g["items"])}
                        for g in v["groups"]]} for k, v in CASES.items()]


@app.get("/api/sessions")
async def api_sessions():
    """历史场次列表（新的在前）。"""
    rows = []
    for d in sorted(CONSULT.iterdir(), key=lambda p: p.name, reverse=True):
        if not d.is_dir():
            continue
        try:
            segs = json.loads((d / "transcript.json").read_text(encoding="utf-8"))
        except Exception:
            segs = []
        case_id = d.name.rsplit("-", 1)[-1]
        if case_id not in CASES:
            continue
        rows.append({
            "dir": d.name,
            "case": case_id,
            "caseName": CASES[case_id]["name"],
            "segments": len(segs),
            "chars": sum(len(s["text"]) for s in segs),
            "duration": round(segs[-1]["end"], 1) if segs else 0,
            "mtime": d.stat().st_mtime,
        })
    return rows


@app.post("/api/sessions/load")
async def api_session_load(payload: dict):
    """切回一个历史场次（文件一直在磁盘上，这里只是重新加载到界面）。"""
    global current
    name = str(payload.get("dir") or "")
    d = CONSULT / name
    if not d.is_dir():
        raise HTTPException(404, "没有这个场次")
    case_id = name.rsplit("-", 1)[-1]
    if case_id not in CASES:
        raise HTTPException(400, "场次里的案件类型不认识")
    with current_lock:
        _retire(current)
        current = Session.load(d, case_id, CASES[case_id]["name"], CASES[case_id],
                                ASKS.get(case_id))
        logger.info("session: load %s (%d 段)", name, len(current.segments))
        return {"ok": True, "state": current.state()}


STATE_CN = {0: "红·缺", 1: "黄·部分", 2: "绿·齐"}
TAG_CN = {"confirm": "数字确认", "risk": "风险", "evidence": "证据", "ask": "补问"}


def _mmss(sec) -> str:
    sec = max(0, int(sec or 0))
    return "%d:%02d" % (sec // 60, sec % 60)


def _markdown(s: Session) -> str:
    """把一场会谈拼成 Markdown：清单、待补问、提醒、全文。"""
    st = s.state()
    t = st["totals"]
    out = ["# 谈案助手 · 会谈纪要", "",
           "- 案件类型：%s" % st["caseName"],
           "- 场次：%s" % st["sessionDir"],
           "- 时长：%s　段数：%d　字数：%d" % (_mmss(st["duration"]), st["segments"], st["chars"]),
           "- 导出时间：%s" % time.strftime("%Y-%m-%d %H:%M"), "",
           "## 必问清单（覆盖 %d/%d，黄 %d，红 %d）" % (t["covered"], t["total"], t["partial"], t["missing"]),
           "", "| 分组 | 清单项 | 状态 | 命中关键词 |", "|---|---|---|---|"]
    for g in st["groups"]:
        for it in g["items"]:
            hits = "、".join(it.get("matched") or []) or "—"
            out.append("| %s | %s | %s | %s |" % (g["name"], it["label"],
                                                   STATE_CN.get(it["state"], it["state"]), hits))
    out.append("")
    if st["asks"]:
        out += ["## 本轮还该补问", ""]
        for a in st["asks"]:
            out.append("- **%s**：%s" % (a["label"], a["need"]))
            out.append("  - 话术：%s" % a["ask"])
        out.append("")
    if s.recent_reminders:
        out += ["## 实时提醒（%d 条）" % len(s.recent_reminders), ""]
        for r in sorted(s.recent_reminders, key=lambda x: x.get("at", 0)):
            out.append("- `%s` **%s · %s**：%s" % (
                _mmss(r.get("at")), TAG_CN.get(r.get("type"), r.get("type")),
                r.get("title", ""), r.get("body", "")))
        out.append("")
    out += ["## 全文转写", ""]
    for seg in s.segments:
        out.append("- `%s` %s" % (_mmss(seg.get("start")), seg.get("text", "")))
    out += ["", "---", "由谈案助手本地生成；转写为机器识别，可能有错字，以录音为准。"]
    return "\n".join(out)


@app.get("/api/export")
async def api_export(fmt: str = Query("md")):
    """导出这一场的会谈纪要（md / txt）。"""
    s = ensure_session()
    if fmt not in ("md", "txt"):
        raise HTTPException(400, "只支持 fmt=md 或 fmt=txt")
    text = _markdown(s)
    if fmt == "txt":
        text = re.sub(r"[#*`|]", "", text)
    fname = "会谈纪要-%s.%s" % (s.dir.name, fmt)
    media = "text/markdown; charset=utf-8" if fmt == "md" else "text/plain; charset=utf-8"
    return Response(content=text, media_type=media,
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''%s" % quote(fname)})


@app.get("/api/transcript")
async def api_transcript():
    s = ensure_session()
    return {"segments": s.segments, "state": s.state()}


@app.post("/api/override")
async def api_override(payload: dict):
    s = ensure_session()
    item_id = payload.get("id")
    st = int(payload.get("state", 0))
    if item_id not in s.by_id:
        raise HTTPException(400, "unknown item")
    if st < 0:
        s.override.pop(item_id, None)
    else:
        s.override[item_id] = max(0, min(2, st))
    s.save()
    return {"ok": True, "state": s.state()}


RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _parse_range(rng: str, size: int):
    """解析 Range 头。返回 (start, end)；"unsat" 表示越界（要回 416）；None 表示看不懂（按整文件给）。

    上游只认 bytes=起点-，碰到 bytes=-500（要最后 500 字节）或起点越界时，
    end 会小于 start，Content-Length 变成负数，h11 直接抛 bad Content-Length 把连接掐断。
    """
    m = RANGE_RE.fullmatch((rng or "").strip())
    if not m or (not m.group(1) and not m.group(2)):
        return None
    a, b = m.group(1), m.group(2)
    if not a:                                  # bytes=-500：最后 500 字节
        n = int(b)
        if n <= 0 or size == 0:
            return "unsat"
        return max(0, size - n), size - 1
    start = int(a)
    if start >= size:                          # 起点比文件还大：416
        return "unsat"
    end = int(b) if b else size - 1
    if end < start:
        return "unsat"
    return start, min(end, size - 1)


@app.get("/api/audio")
async def api_audio(request: Request):
    s = ensure_session()
    path = s.dir / "audio.wav"
    if not path.exists():
        raise HTTPException(404, "no audio")
    size = path.stat().st_size
    rng = request.headers.get("range")
    if not rng:
        return FileResponse(path, media_type="audio/wav", headers={"Accept-Ranges": "bytes"})
    parsed = _parse_range(rng, size)
    if parsed == "unsat":
        # 越界：规范要求 416。绝不能让它带着负的 Content-Length 发出去
        return Response(status_code=416, headers={"Content-Range": "bytes */%d" % size})
    if not parsed:
        return FileResponse(path, media_type="audio/wav", headers={"Accept-Ranges": "bytes"})
    start, end = parsed
    length = end - start + 1

    def stream():
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    return StreamingResponse(stream(), status_code=206, media_type="audio/wav",
                             headers={"Content-Range": "bytes %d-%d/%d" % (start, end, size),
                                      "Accept-Ranges": "bytes",
                                      "Content-Length": str(length)})


# ------------------------------------------------------------------ 实时转写
@app.websocket("/asr")
async def websocket_asr(websocket: WebSocket, samplerate: int = Query(16000)):
    await websocket.accept()
    s = ensure_session()
    stream: ASRStream = await start_asr_stream(samplerate, args)
    if not stream:
        await websocket.close()
        return
    # 二次开录时把时间轴接上，不要从 0 重新计时
    stream.base_offset = s.audio.data_len / (2 * samplerate)
    s.active_streams += 1
    logger.info("asr: stream start at offset %.1fs", stream.base_offset)

    async def task_recv_pcm():
        while True:
            pcm = await websocket.receive_bytes()
            if not pcm:
                return
            s.write_pcm(pcm)
            await stream.write(pcm)

    async def task_send_result():
        while True:
            result: ASRResult = await stream.read()
            if not result:
                return
            if not result.finished:
                await websocket.send_json({"type": "partial", "text": result.text})
                continue
            seg = s.add_segment(result.text, result.start, result.end)
            await websocket.send_json({"type": "segment", "segment": seg})

    try:
        await asyncio.gather(task_recv_pcm(), task_send_result())
    except WebSocketDisconnect:
        logger.info("asr: disconnected")
    finally:
        await stream.close()          # 这里会 flush VAD，最后一句不丢
        # 把 flush 出来的尾巴补发出去
        while not stream.outbuf.empty():
            result = stream.outbuf.get_nowait()
            if result and result.finished:
                s.add_segment(result.text, result.start, result.end)
        s.active_streams = max(0, s.active_streams - 1)
        s.save()
        try:
            await websocket.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 导入录音
def _to_wav(src: Path, dst: Path) -> float:
    proc = subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "1",
         "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
        capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError("ffmpeg 转码失败: " + (proc.stderr or "")[:300])
    return float(sf.info(str(dst)).duration)


def _import_worker(wav: Path, src: Path, total: float, s):
    global import_state
    try:
        data, sr = sf.read(str(wav), dtype="float32")
        if data.ndim > 1:
            data = data[:, 0]
        # 先把整段录音一次写进 audio.wav —— 前端马上就能开播，
        # 转写再在后台慢慢跑，文字跟着播放进度露出来（跟视频 demo 一个效果）
        s.write_pcm((np.asarray(data, dtype=np.float32) * 32767).astype(np.int16).tobytes())
        s.save()
        import_state.update({"ready": True, "total": round(total, 1), "done": 0.0})

        def on_segment(r):
            s.add_segment(r.text, r.start, r.end)      # 每句立刻进清单和提醒

        def on_progress(pos, _pcm):
            import_state.update({"done": pos})

        count = process_asr_file_streaming(
            np.asarray(data, dtype=np.float32), sr, args,
            on_segment=on_segment, on_progress=on_progress,
            step_seconds=0.05, progress_every=0.5,
            should_stop=lambda: not import_state["running"])
        s.save()
        import_state.update({"running": False, "done": round(total, 1), "segments": count})
    except Exception as exc:                      # noqa: BLE001
        logger.exception("import failed")
        import_state.update({"running": False, "error": str(exc)[:300]})
    finally:
        for p in (wav, src):                       # 临时文件别留在 /tmp
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    global import_state
    if import_state["running"]:
        raise HTTPException(409, "已有导入在进行")
    raw = await file.read()
    suffix = Path(file.filename or "a").suffix or ".bin"
    tmp_in = Path(tempfile.mkstemp(suffix=suffix)[1])
    tmp_in.write_bytes(raw)
    tmp_wav = tmp_in.with_name(tmp_in.stem + "_16k.wav")   # 不能和输入同名，否则 ffmpeg 拒绝
    try:
        total = await asyncio.to_thread(_to_wav, tmp_in, tmp_wav)   # 转码开线程，别堵住轮询
    except Exception as exc:                      # noqa: BLE001
        tmp_in.unlink(missing_ok=True)
        raise HTTPException(400, str(exc))
    # 导入录音一律新开一场（避免和历史场次/录音混在一起）
    global current
    cur = ensure_session()          # 先确保有场次（这一步会先尝试恢复最近一场）
    # 有内容、或者正有录音连接在往里写，都要另开一场：
    # 刚开录还没出第一句时 segments 是空的，但 audio.wav 已经在写，混在一起会串音。
    if cur.segments or cur.recording:
        with current_lock:
            _retire(cur)
            current = Session(CONSULT, cur.case_id, CASES[cur.case_id]["name"], CASES[cur.case_id],
                              ASKS.get(cur.case_id))
            cur = current
            logger.info("session: new %s (导入录音)", cur.dir.name)
    import_state = {"running": True, "ready": False, "total": round(total, 1), "done": 0,
                    "error": "", "segments": 0, "dir": cur.dir.name}
    threading.Thread(target=_import_worker, args=(tmp_wav, tmp_in, total, cur), daemon=True).start()
    return {"ok": True, "duration": round(total, 1)}


@app.post("/api/upload/cancel")
async def api_upload_cancel():
    import_state["running"] = False
    return {"ok": True}


@app.exception_handler(Exception)
async def _any_exc(request: Request, exc: Exception):
    logger.exception("unhandled: %s", exc)
    return Response(content=json.dumps({"error": str(exc)}), status_code=500,
                    media_type="application/json")


def main():
    global args
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--addr", type=str, default="127.0.0.1")
    parser.add_argument("--asr-provider", type=str, default="cpu")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--models-root", type=str, default=str(ROOT / "models"))
    parser.add_argument("--asr-model", type=str, default="sensevoice-int8")
    parser.add_argument("--asr-lang", type=str, default="zh")
    args = parser.parse_args()

    logging.basicConfig(format="%(levelname)s: %(asctime)s %(name)s:%(lineno)s %(message)s",
                        level=logging.INFO)

    app.mount("/", StaticFiles(directory=str(ROOT / "assets"), html=True), name="assets")
    import uvicorn
    uvicorn.run(app, host=args.addr, port=args.port)


if __name__ == "__main__":
    main()
