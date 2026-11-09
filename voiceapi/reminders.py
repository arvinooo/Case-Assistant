# -*- coding: utf-8 -*-
"""零延迟提醒引擎：不调模型，纯规则，收到一句就能立刻出结果。

设计原则（和 calc.py 一致）：硬信息不许等模型。
- 清单覆盖：靠每项的 hints 关键词命中，即时
- 数字提醒：正则抓金额/日期/时长，缺口径就提醒确认
- 风险提醒：抓不利表述，提示固定证据
"""
import re
from typing import List, Dict, Optional

# ---------------- 硬信息正则 ----------------
RE_AMOUNT = re.compile(r"(\d[\d,\.]*\s*(?:万|元|块|块钱)|[一二两三四五六七八九十百千]+万[一二三四五六七八九]?\s*(?:元|块)?|[一二两三四五六七八九十百千]+(?:元|块钱))")
RE_DATE = re.compile(r"(\d{4}\s*年|\d{1,2}\s*月\s*\d{1,2}\s*[日号]|\d{1,2}\s*月|(?<!\d)\d{1,2}\s*[日号]|去年|前年|上年|上月|上个月|上周|本周|今天|昨天|前天)")
RE_DURATION = re.compile(r"(\d+\s*个?\s*[多余几]?\s*(?:月|年|天|小时|日)|[一二两三四五六七八九十百]+\s*个?\s*[多余几]?\s*(?:月|年|天|小时))")
RE_DOC = re.compile(r"(工资条|考勤|打卡|钉钉|企业微信|微信|聊天记录|录音|截屏|截图|劳动合同|解除通知|通知书|协议|社保|公积金|银行流水|流水|邮件|邮箱|Excel|excel)")

# 金额口径缺失时提醒
RE_WAGE_BASIS = re.compile(r"(税前|税后|到手|实发|应发|含绩效|不含绩效|含补贴)")

# B 类：不利表述 / 矛盾风险
RISK_PATTERNS = [
    (re.compile(r"(签了|签字|签收)"), "客户已签字", "签字=签收不等于同意，但要有证据证明签字时被催/没看内容"),
    (re.compile(r"(协商一致)"), "出现「协商一致」", "这词对己方不利，要问清是谁提的、有没有书面依据"),
    (re.compile(r"(放弃|不再主张|再无任何|追诉)"), "出现放弃权利表述", "协议里的弃权条款要拍原件，评估是否可撤销"),
    (re.compile(r"(违纪|严重违反|旷工|警告|处分)"), "出现违纪类表述", "公司若反诉违纪，要固定绩效/考勤等反证"),
    (re.compile(r"(现金|没签合同|没有合同|口头)"), "出现无书面凭据的表述", "缺书面凭据的给付/约定，要补聊天记录或录音"),
]

# 欠付类表述：没发 / 没结清 / 拖欠
RE_OWED = re.compile(r"(拖欠|没发|没给|没结清|没结算|欠着|一分钱没|迟迟不)")

# C 类：数字口径提醒
NUM_BASIS_HINT = "金额口径要问清：税前还是到手？含不含绩效/补贴？"


# "2021年""3月"这种是日期，不是工龄。老版本把它们当期限，于是几乎每句话都弹"期限口径"。
RE_DATE_ONLY = re.compile(r"^(?:(?:19|20)\d{2}年|\d{1,2}月|\d{1,2}[日号])$")


def extract(text: str) -> Dict[str, List[str]]:
    durations = _uniq(m.group(0).strip() for m in RE_DURATION.finditer(text))
    return {
        "amounts": _uniq(m.group(1).strip() for m in RE_AMOUNT.finditer(text)),
        "dates": _uniq(m.group(1).strip() for m in RE_DATE.finditer(text)),
        "durations": [d for d in durations if not RE_DATE_ONLY.match(d)],
        "docs": _uniq(m.group(1) for m in RE_DOC.finditer(text)),
    }


def _uniq(it):
    seen, out = set(), []
    for x in it:
        x = re.sub(r"\s+", "", x)
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def analyze(text: str, seg_id: str) -> List[dict]:
    """对一句新转写产出即时提醒（0 延迟）。"""
    out = []
    facts = extract(text)

    # C1 金额：说了钱但没说口径
    if facts["amounts"] and not RE_WAGE_BASIS.search(text):
        out.append({
            "type": "confirm",
            "title": "金额口径",
            "body": "客户提到「%s」，但没说税前还是到手。追问：%s" % ("、".join(facts["amounts"][:2]), NUM_BASIS_HINT),
            "src": seg_id,
            "priority": 60,
        })

    # C2 加班时长：直接决定倍率，问清是工作日/休息日/法定节假日
    if facts["durations"] and "小时" in " ".join(facts["durations"]) and "加班" in text:
        out.append({
            "type": "confirm",
            "title": "加班时长",
            "body": "提到「%s」加班——问清：工作日/休息日/法定节假日各多少小时？有没有考勤或加班审批记录？" % "、".join(facts["durations"][:2]),
            "src": seg_id,
            "priority": 58,
        })

    # C2 时长：说了几个月/几年，可能直接影响 N 的算法
    if facts["durations"] and re.search(r"(个月|年)", " ".join(facts["durations"])):
        out.append({
            "type": "confirm",
            "title": "期限口径",
            "body": "提到「%s」，确认是否精确到日（算 N 要用满 6 个月/1 年的分界）。" % "、".join(facts["durations"][:2]),
            "src": seg_id,
            "priority": 50,
        })

    # C2b 欠付：说了没发/拖欠，当场问清月份和金额
    if RE_OWED.search(text):
        out.append({
            "type": "confirm",
            "title": "欠付确认",
            "body": "出现「%s」——问清：欠哪几个月、各月多少、有没有工资流水或欠条能固定。" % RE_OWED.search(text).group(0),
            "src": seg_id,
            "priority": 65,
        })

    # C3 凭证：说到证据载体，提醒当场固定
    if facts["docs"]:
        out.append({
            "type": "evidence",
            "title": "证据固定",
            "body": "提到「%s」，当场确认：还在手上吗？能导出/拍照吗？原件几份？" % "、".join(facts["docs"][:3]),
            "src": seg_id,
            "priority": 55,
        })

    # B 类风险
    for pat, title, why in RISK_PATTERNS:
        m = pat.search(text)
        if m:
            out.append({
                "type": "risk",
                "title": title,
                "body": "%s —— %s" % (why, m.group(0)),
                "src": seg_id,
                "priority": 70,
            })
            break

    return out


def score_item_state(hits: int) -> int:
    """0 红 / 1 黄 / 2 绿"""
    if hits >= 2:
        return 2
    if hits >= 1:
        return 1
    return 0
