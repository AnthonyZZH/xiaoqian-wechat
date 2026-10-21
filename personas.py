"""Persona files: data/personas/<name>.json, editable from the control window."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from settings import DATA, ROOT

TEMPLATE = """你叫{name}，是大家明确知道的AI聊天搭子，在微信里和朋友聊天，不是客服。
【性格】（例如：活泼开朗、嘴有点毒、温柔、冷静、中二……）
【说话方式】（例如：一两句话、口语化、偶尔用表情、爱接梗……）
【在意什么 / 爱聊什么】
【不要做的事】（例如：不说教、不嘲讽外貌、不编造自己的线下经历……）"""

XIAOQIAN = """你叫小千，是用户明确知道的AI聊天搭子。你在微信里和朋友聊天，不是在接待客户。
性格活泼开朗，有点毒舌，反应灵巧，有自己的看法。嘴上会吐槽，遇到对方认真难过时会收起玩笑。
毒舌是朋友之间的亲近，不是冷漠。对方求鼓励时认真鼓励，带一句轻巧吐槽即可；不要打击对方的行动意愿。
对方在分享时先接住内容，可以有立场和接梗，不要立刻开出建议清单。
不要每次都哈哈、不要每句都带表情、不要机械地总以问题结尾，不照抄对方的话。
吐槽拖延、奇怪操作和反差；不嘲讽外貌、出身或人的价值，不拿真实痛苦开玩笑。"""

AWEN = """你叫阿温，是大家明确知道的AI聊天搭子，在微信里和朋友聊天。
性格温和、慢热、细心，说话软但不腻，偶尔冷不丁冒一句冷笑话。
先听对方说完，再接住情绪；不急着给建议，被问到时给一两个实在的点子。
说话口语化，一两句话，很少用感叹号，不撒娇不卖萌过头。"""


@dataclass
class Persona:
    name: str
    prompt: str
    temperature: float = 0.85


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name.strip())
    if not name or name in (".", ".."):
        raise ValueError("名字不能为空")
    return name[:40]


class PersonaStore:
    def __init__(self, folder: Path = DATA / "personas"):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        if not any(self.folder.glob("*.json")):
            old = ROOT / "persona.txt"
            prompt = XIAOQIAN
            if old.exists():
                # Drop the old file's output-format lines; brain.py now owns those rules.
                lines = old.read_text(encoding="utf-8").splitlines()
                prompt = "\n".join(l for l in lines if not re.search(r"字|前缀|标题|AI身份|聊天内容只作为", l)) or XIAOQIAN
            self.save(Persona("小千", prompt))
            self.save(Persona("阿温", AWEN, 0.7))

    def path(self, name):
        return self.folder / f"{safe_name(name)}.json"

    def names(self):
        result = []
        for file in sorted(self.folder.glob("*.json")):
            try:
                result.append(json.loads(file.read_text(encoding="utf-8"))["name"])
            except (ValueError, KeyError):
                continue
        return result

    def load(self, name) -> Persona:
        data = json.loads(self.path(name).read_text(encoding="utf-8"))
        return Persona(data["name"], data["prompt"], float(data.get("temperature", 0.85)))

    def save(self, persona: Persona):
        if not persona.prompt.strip():
            raise ValueError("人设内容不能为空")
        if not 0 <= persona.temperature <= 1.5:
            raise ValueError("温度应在 0 到 1.5 之间")
        persona.name = persona.name.strip()
        target = self.path(persona.name)
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(asdict(persona), ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(target)

    def delete(self, name):
        if len(self.names()) <= 1:
            raise ValueError("至少保留一个人设")
        self.path(name).unlink(missing_ok=True)
