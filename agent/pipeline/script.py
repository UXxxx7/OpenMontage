"""
pipeline/script.py — Generate a structured Cantonese video script via LLM.

Output schema:
{
  "title": "短片主題",
  "duration_target": 60,
  "lines": [
    { "id": 1, "text": "大家好，我係 Dickson…", "phase": "intro" },
    ...
  ],
  "phases": [
    { "id": "intro",  "title": "開場",   "subtitle": "AI 全自動視頻",   "body": "只需…", "icon": "🎙️" },
    { "id": "step1",  "title": "第一步", "subtitle": "WhatsApp 輸入",   "body": "發送…", "icon": "📱" },
    { "id": "step2",  "title": "第二步", "subtitle": "AI 自動製作",     "body": "Agent…","icon": "🤖" },
    { "id": "step3",  "title": "第三步", "subtitle": "即時上傳",        "body": "成品…", "icon": "📤" },
    { "id": "cta",    "title": "你覺得呢？","subtitle": "會唔會幫到你", "body": "留言…","icon": "💬" }
  ],
  "full_text": "大家好…（完整旁白）"
}
"""

import json
import os
from pathlib import Path

import anthropic

SYSTEM_PROMPT = """你是一位專業的廣東話視頻腳本作家。
用戶會提供一個主題，你需要輸出一份結構化的 JSON 腳本，供數字人講解視頻使用。

要求：
- 語言：廣東話口語（繁體字）
- 時長：約 45-60 秒（約 130-180 字）
- 結構：intro → step1 → step2 → step3 → cta
- 語氣：親切、專業、有說服力
- 每個 phase 的 body 要對應該 phase 講的核心內容（不超過 30 字）

只輸出 JSON，不要任何其他文字。"""

USER_TEMPLATE = """主題：{topic}

請生成腳本 JSON，格式如下：
{{
  "title": "...",
  "duration_target": 60,
  "lines": [
    {{"id": 1, "text": "...", "phase": "intro"}},
    ...
  ],
  "phases": [
    {{"id": "intro",  "title": "...", "subtitle": "...", "body": "...", "icon": "🎙️"}},
    {{"id": "step1",  "title": "...", "subtitle": "...", "body": "...", "icon": "📱"}},
    {{"id": "step2",  "title": "...", "subtitle": "...", "body": "...", "icon": "🤖"}},
    {{"id": "step3",  "title": "...", "subtitle": "...", "body": "...", "icon": "📤"}},
    {{"id": "cta",    "title": "...", "subtitle": "...", "body": "...", "icon": "💬"}}
  ],
  "full_text": "（完整旁白，lines 拼接）"
}}"""


def generate_script(prompt: str, project_dir: Path) -> dict:
    """Call Claude to generate a structured Cantonese script for the given topic."""
    client = anthropic.Anthropic()

    response = client.messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),  # sonnet is fine for scripting
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": USER_TEMPLATE.format(topic=prompt)}],
    )

    raw = response.content[0].text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    script = json.loads(raw)

    # Persist to project dir
    out = project_dir / "script.json"
    out.write_text(json.dumps(script, ensure_ascii=False, indent=2))

    return script
