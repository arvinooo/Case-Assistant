# -*- coding: utf-8 -*-
"""场次状态：转写段落、必问清单、录音存档。

一次面谈 = 一个 Session，落盘到 consultations/<时间>-<案件类型>/
"""
import json
import os
import re
import struct
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from voiceapi import reminders


def _new_id() -> str:
    return "u_" + uuid.uuid4().hex[:8]


# 否定词：客户说"没签合同/记不清身份证号"时，关键词照样命中，但这不是"问到了"。
_NEG_RE = re.compile(r"(没有|没[给发签交办休还做写出留存]|记不清|不记得|不知道|忘了|拒绝|拿不出|丢了|尚未|从未)")
# 同类提醒在"音频时间"上隔多久才允许再弹一次（不是墙钟：导入录音是快进的）
REMINDER_GAP = 45.0


class WavWriter:
    """边录边写的 WAV：先占位写头，之后追加并回填长度。崩溃了也能播。"""

    def __init__(self, path: Path, rate=16000, channels=1, width=2):
        self.path = path
        self.rate, self.channels, self.width = rate, channels, width
        if path.exists() and path.stat().st_size > 44:
            self.data_len = path.stat().st_size - 44   # 接着写，不覆盖已有录音
            self.fh = open(path, "r+b")
            self.fh.seek(0, os.SEEK_END)
            self._flush_header()
        else:
            self.data_len = 0
            self.fh = open(path, "wb")
            self.fh.write(b"\x00" * 44)
            self._flush_header()

    def _flush_header(self):
        block = self.channels * self.width
        byte_rate = self.rate * block
        header = b"RIFF" + struct.pack("<I", 36 + self.data_len) + b"WAVE"
        header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, self.channels,
                                        self.rate, byte_rate, block, self.width * 8)
        header += b"data" + struct.pack("<I", self.data_len)
        pos = self.fh.tell()
        self.fh.seek(0)
        self.fh.write(header)
        self.fh.seek(0, os.SEEK_END)
        self.fh.flush()
        return pos

    def write(self, pcm: bytes):
        self.fh.seek(0, os.SEEK_END)
        self.fh.write(pcm)
        self.data_len += len(pcm)
        self.fh.flush()
        self._flush_header()

    def close(self):
        try:
            self._flush_header()
            self.fh.close()
        except Exception:
            pass


class Session:
    def __init__(self, root: Path, case_id: str, case_name: str, checklist_def: dict,
                 asks_def: Optional[dict] = None):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.case_id = case_id
        self.case_name = case_name
        # 目录名只精确到秒：同一秒里开第二场会撞名，撞了就加序号。
        # 序号放在时间后面、案件类型前面，保证 "…-2-labor" 结尾仍是案件类型。
        self.dir = root / ("%s-%s" % (stamp, case_id))
        n = 2
        while self.dir.exists():
            self.dir = root / ("%s-%d-%s" % (stamp, n, case_id))
            n += 1
        self.dir.mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()

        self.active_streams = 0              # 有几条采集连接正在往本场写
        self.segments: List[dict] = []
        self.items: List[dict] = []          # 扁平清单项
        self.by_id: Dict[str, dict] = {}
        self.override: Dict[str, int] = {}   # 人工纠正
        self.recent_reminders: List[dict] = []
        self._reminder_seen = {}             # (type,title) -> 音频时间，用来限流
        self.asks_cfg = asks_def or {}       # data/asks.json 里本案件类型的那一段
        self.topics: List[dict] = []         # 刚刚聊到的话题（带音频时间）
        self._now = 0.0                      # 当前音频位置（最后一句的起点）

        self._build_items(checklist_def)
        self.audio = WavWriter(self.dir / "audio.wav")
        self.save()

    @property
    def recording(self) -> bool:
        """还有录音连接在用本场次——这种场次不能关，关了会 ValueError: seek of closed file。"""
        return self.active_streams > 0

    # ---------- 清单 ----------
    def _build_items(self, checklist_def: dict):
        for group in checklist_def.get("groups", []):
            for it in group.get("items", []):
                item = {
                    "id": it["id"],
                    "label": it.get("label", ""),
                    "need": it.get("need", ""),
                    "ask": it.get("ask", ""),
                    "group": group.get("id", ""),
                    "group_name": group.get("name", ""),
                    "hints": it.get("hints", []),
                    "hits": 0,                       # 有几句不同的话命中过（含否定）
                    "positive_hits": 0,              # 其中几句是正面回答
                    "matched_hints": [],             # 正面命中过的提示词（去重）
                    "negated_hints": [],             # 客户说"没有/记不清"的提示词
                    "evidence": [],
                }
                self.items.append(item)
                self.by_id[item["id"]] = item

    def state_of(self, item) -> int:
        """0 红 / 1 黄 / 2 绿。

        绿色要三条同时成立：至少两句不同的话命中、至少两个不同提示词命中、
        其中至少一个提示词长度 ≥2（"年/月/元"这种单词不算分量），而且客户不是在否定。
        达不到就停在黄——宁可让律师多看一眼，也别假装"问齐了"。
        """
        if item["id"] in self.override:
            return self.override[item["id"]]
        hits = item.get("hits", 0)
        if hits == 0:
            return 0
        positive = item.get("positive_hits", hits)   # 老场次没这个字段，按全部算
        matched = item.get("matched_hints") or []
        strong = any(len(h) >= 2 for h in matched)
        if positive >= 2 and len(matched) >= 2 and strong:
            return 2
        return 1

    # ---------- 转写 ----------
    def add_segment(self, text: str, start: float, end: float) -> dict:
        seg = {"id": _new_id(), "start": round(start, 3), "end": round(end, 3),
               "text": text, "ts": time.time()}
        self.segments.append(seg)
        self._now = seg["start"]
        self._match_checklist(text, seg["id"])
        self._match_topics(text, seg["start"])
        self._emit_reminders(text, seg["id"], seg["start"])
        self.save()
        return seg

    @staticmethod
    def _split_negated(text: str, matched: List[str]):
        """逐词看命中词前后 6 个字：有否定词就算"客户说没有/记不清"，不算正面回答。

        "记不清身份证号"不能把出借人身份点绿；"公司几个月没发工资"里的"没发"
        虽然否定，但同一句还会命中"几个月"，所以整项不会因此从绿掉成红。
        """
        pos, neg = [], []
        for h in matched:
            i = text.find(h)
            if i < 0:
                pos.append(h)
                continue
            window = text[max(0, i - 6): i + len(h) + 6]
            (neg if _NEG_RE.search(window) else pos).append(h)
        return pos, neg

    def _match_checklist(self, text: str, seg_id: str):
        for item in self.items:
            matched = [h for h in item["hints"] if h and h in text]
            if not matched:
                continue
            item["hits"] += 1
            pos, neg = self._split_negated(text, matched)
            if pos:
                item["positive_hits"] += 1
                seen = set(item.get("matched_hints") or [])
                seen.update(pos)
                item["matched_hints"] = sorted(seen)
            if neg:
                seen_neg = set(item.get("negated_hints") or [])
                seen_neg.update(neg)
                item["negated_hints"] = sorted(seen_neg)
            if seg_id not in item["evidence"]:
                item["evidence"].append(seg_id)
            item["evidence"] = item["evidence"][-8:]

    def _match_topics(self, text: str, start: float):
        """按 data/asks.json 的话题表，记住"客户刚聊到哪个话题"。"""
        for topic in (self.asks_cfg or {}).get("topics", []):
            matched = [kw for kw in topic.get("trigger", []) if kw in text]
            if not matched:
                continue
            row = {"id": topic["id"], "name": topic["name"],
                   "weight": topic.get("weight", 5),
                   "targets": topic.get("targets", []),
                   "at": round(start, 1), "matched": matched[:3]}
            self.topics = [t for t in self.topics if t["id"] != row["id"]]
            self.topics.append(row)
            self.topics = self.topics[-6:]

    def _emit_reminders(self, text: str, seg_id: str, start: float):
        for r in reminders.analyze(text, seg_id):
            key = (r["type"], r["title"])
            last = self._reminder_seen.get(key)
            # 按音频时间限流。导入录音 7 分半只要 20 秒转完，
            # 用墙钟限流会把整场压成一张卡（实测被吃掉 78%）。
            if last is not None and abs(start - last) < REMINDER_GAP:
                continue
            self._reminder_seen[key] = start
            r["at"] = round(start, 1)        # 卡片时间 = 这句话在录音里的位置
            self.recent_reminders.append(r)
        self.recent_reminders = self.recent_reminders[-200:]

    # ---------- 该补问 ----------
    def asks(self, limit: int = 3) -> List[dict]:
        """缺口越大越急；刚刚聊到的话题会把它对应的追问项顶上来。

        话题表来自 data/asks.json（跟 voice-consult 是同一份数据）。
        """
        cfg = self.asks_cfg or {}
        recent = [t for t in self.topics if self._now - t["at"] <= 300]   # 近 5 分钟说过
        bonus: Dict[str, int] = {}
        why: Dict[str, List[str]] = {}
        for t in recent:
            for target in t.get("targets", []):
                bonus[target] = bonus.get(target, 0) + t.get("weight", 5)
                names = why.setdefault(target, [])
                if t["name"] not in names:
                    names.append(t["name"])

        order = cfg.get("baseOrder") or [i["id"] for i in self.items]
        rank = {iid: i for i, iid in enumerate(order)}

        scored = []
        for item in self.items:
            st = self.state_of(item)
            if st == 2:                       # 已经绿的不推
                continue
            score = (2 - st) * 30 + bonus.get(item["id"], 0)
            scored.append((score, rank.get(item["id"], 999), item))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [{"id": it["id"], "label": it["label"], "need": it["need"],
                 "ask": it["ask"], "group": it["group_name"],
                 "state": self.state_of(it), "score": score,
                 "reasons": why.get(it["id"], [])} for score, _, it in scored[:limit]]

    def state(self) -> dict:
        groups = []
        order = []
        for item in self.items:
            if item["group"] not in order:
                order.append(item["group"])
        for gid in order:
            gs = [i for i in self.items if i["group"] == gid]
            covered = sum(1 for i in gs if self.state_of(i) == 2)
            groups.append({
                "id": gid,
                "name": gs[0]["group_name"],
                "items": [{
                    "id": i["id"], "label": i["label"], "need": i["need"], "ask": i["ask"],
                    "state": self.state_of(i), "evidence": i["evidence"],
                    "matched": i.get("matched_hints", []),
                    "negated": i.get("negated_hints", []),
                    "manual": i["id"] in self.override,
                } for i in gs],
                "covered": covered, "total": len(gs),
            })
        return {
            "case": self.case_id,
            "caseName": self.case_name,
            "sessionDir": self.dir.name,
            "startedAt": self.started_at,
            "segments": len(self.segments),
            "chars": sum(len(s["text"]) for s in self.segments),
            "duration": round(self.segments[-1]["end"], 1) if self.segments else 0,
            "groups": groups,
            "asks": self.asks(),
            # 风险卡（priority 70）永远排在前面，其次按音频时间倒序
            "reminders": sorted(self.recent_reminders[-40:],
                                key=lambda r: (-r.get("priority", 0), -r.get("at", 0)))[:12],
            "totals": {
                "covered": sum(1 for i in self.items if self.state_of(i) == 2),
                "partial": sum(1 for i in self.items if self.state_of(i) == 1),
                "missing": sum(1 for i in self.items if self.state_of(i) == 0),
                "total": len(self.items),
            },
        }

    # ---------- 落盘 ----------
    def save(self):
        try:
            (self.dir / "transcript.json").write_text(
                json.dumps(self.segments, ensure_ascii=False, indent=1), encoding="utf-8")
            (self.dir / "checklist.json").write_text(
                json.dumps({"ver": 2, "override": self.override,
                            "items": [{"id": i["id"], "hits": i["hits"],
                                       "positive_hits": i.get("positive_hits", 0),
                                       "matched_hints": i.get("matched_hints", []),
                                       "negated_hints": i.get("negated_hints", []),
                                       "state": self.state_of(i),
                                       "evidence": i["evidence"]} for i in self.items]},
                           ensure_ascii=False, indent=1), encoding="utf-8")
            (self.dir / "reminders.json").write_text(
                json.dumps(self.recent_reminders, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    @classmethod
    def load(cls, dirpath: Path, case_id: str, case_name: str, checklist_def: dict,
             asks_def: Optional[dict] = None) -> "Session":
        """从磁盘恢复一场（重启服务不丢当前场次）。"""
        self = cls.__new__(cls)
        stamp = dirpath.name.split("-")[0]
        self.case_id = case_id
        self.case_name = case_name
        self.dir = dirpath
        self.started_at = time.time()
        self.active_streams = 0
        self.segments = []
        self.items = []
        self.by_id = {}
        self.override = {}
        self.recent_reminders = []
        self._reminder_seen = {}
        self.asks_cfg = asks_def or {}
        self.topics = []
        self._now = 0.0
        self._build_items(checklist_def)
        try:
            self.segments = json.loads((dirpath / "transcript.json").read_text(encoding="utf-8"))
        except Exception:
            self.segments = []
        legacy = True
        try:
            saved = json.loads((dirpath / "checklist.json").read_text(encoding="utf-8"))
            legacy = saved.get("ver", 1) < 2
            self.override = saved.get("override", {})
            for row in saved.get("items", []):
                item = self.by_id.get(row.get("id"))
                if not item:
                    continue
                item["hits"] = row.get("hits", 0)
                item["positive_hits"] = row.get("positive_hits", row.get("hits", 0))
                item["matched_hints"] = row.get("matched_hints", [])
                item["negated_hints"] = row.get("negated_hints", [])
                item["evidence"] = row.get("evidence", [])
        except Exception:
            pass
        if legacy and self.segments:
            # 老场次是按旧规则（关键词一命中就算）算出来的，覆盖统计不准。
            # 转写一直在，直接按新规则重放一遍：不改一句原文，只重算状态和话题。
            for item in self.items:
                item.update({"hits": 0, "positive_hits": 0, "matched_hints": [],
                             "negated_hints": [], "evidence": []})
            for seg in self.segments:
                self._match_checklist(seg.get("text", ""), seg.get("id", ""))
                self._match_topics(seg.get("text", ""), seg.get("start", 0))
        try:
            self.recent_reminders = json.loads((dirpath / "reminders.json").read_text(encoding="utf-8"))
        except Exception:
            self.recent_reminders = []
        # 老场次里的提醒时间是用墙钟算的（错的），这里按 src 段号纠正成音频时间
        start_by_id = {s["id"]: s.get("start", 0) for s in self.segments}
        fixed = []
        for r in self.recent_reminders:
            st = start_by_id.get(r.get("src"))
            if st is not None:
                r["at"] = round(st, 1)
            fixed.append(r)
        self.recent_reminders = fixed
        if self.segments:
            self._now = self.segments[-1].get("start", 0)
        self.audio = WavWriter(dirpath / "audio.wav")   # 追加模式
        return self

    def write_pcm(self, pcm: bytes):
        self.audio.write(pcm)

    def close(self):
        self.audio.close()
        self.save()
