#!/usr/bin/env python3
"""
Convert declaration-guide Markdown files into structured policy rules.

This is intentionally separate from both the downloader and the guide generator:
download/parse -> declaration guide -> structured matching rules.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT_DIR = Path("data/xiongan_policy_files")
DEFAULT_GUIDE_DIR_NAME = "declaration_guides"
DEFAULT_RULES_FILE = Path("data/xiongan_policy_files/policy_rules.jsonl")
DEFAULT_API_BASE = "http://192.168.211.108:8000/v1"
DEFAULT_API_KEY = "EMPTY"
DEFAULT_MODEL = "/data3/yangsien/models/Qwen3.6-27B"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


class RuleBuildError(RuntimeError):
    pass


REQUIRED_KEYS = [
    "policy_id",
    "title",
    "source_guide_path",
    "policy_summary",
    "benefit",
    "applicant_objects",
    "hard_conditions",
    "soft_conditions",
    "exclusion_conditions",
    "required_materials",
    "process",
    "deadline",
    "contacts",
    "missing_info_needed",
    "industry_keywords",
    "region_requirements",
    "company_stage_requirements",
    "confidence",
    "notes",
]

LIST_KEYS = {
    "applicant_objects",
    "hard_conditions",
    "soft_conditions",
    "exclusion_conditions",
    "required_materials",
    "process",
    "contacts",
    "missing_info_needed",
    "industry_keywords",
    "region_requirements",
    "company_stage_requirements",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Structure declaration-guide Markdown files into policy_rules.jsonl."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--guide-dir", type=Path, default=None)
    parser.add_argument("--rules-file", type=Path, default=DEFAULT_RULES_FILE)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--max-input-chars", type=int, default=60000)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--enable-thinking", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--delay", type=float, default=0.2)
    return parser.parse_args()


def guide_files(guide_dir: Path) -> list[Path]:
    return sorted(guide_dir.glob("*_申报指南.md"))


def policy_id_from_path(path: Path) -> str:
    match = re.match(r"^(\d{3})_", path.name)
    return match.group(1) if match else path.stem


def title_from_path(path: Path) -> str:
    name = path.stem
    name = re.sub(r"^\d{3}_\d{4}-\d{2}-\d{2}_", "", name)
    return name.removesuffix("_申报指南")


def system_prompt() -> str:
    return (
        "你是政策申报规则结构化助手。你只根据用户提供的申报指南内容，"
        "抽取用于企业政策匹配的结构化规则。不要编造原文没有的信息。"
        "必须输出合法 JSON 对象，不要输出 Markdown、解释或代码块。"
    )


def user_prompt(*, policy_id: str, title: str, guide_path: Path, markdown_text: str) -> str:
    return f"""请把下面的申报指南结构化为一个 JSON 对象。

必须严格使用这些字段：
{json.dumps(REQUIRED_KEYS, ensure_ascii=False)}

字段要求：
- policy_id：使用给定 policy_id。
- title：使用给定标题。
- source_guide_path：使用给定路径。
- policy_summary：政策简介，1-3 句。
- benefit：奖励额度、补贴标准、支持内容；原文没有则写空字符串。
- applicant_objects：申请对象数组。
- hard_conditions：企业必须满足的硬性申报条件数组，保留数字阈值、比例、时间、资质要求。
- soft_conditions：方向匹配、产业相关、推荐类条件数组。
- exclusion_conditions：不予申报、不得推荐、失信、违法、安全环保质量等排除条件数组。
- required_materials：申报材料数组。
- process：办理流程数组。
- deadline：申报时间、截止时间、有效期；原文没有则写空字符串。
- contacts：联系人、电话、邮箱、技术支持电话数组。
- missing_info_needed：用于判断企业是否符合政策时还需要企业补充的信息数组。
- industry_keywords：从政策中提取的行业/产业/技术关键词数组。
- region_requirements：地域要求数组，例如雄安新区、河北省等。
- company_stage_requirements：企业阶段/规模/资质要求数组，例如中小企业、科技型中小企业、高新技术企业等。
- confidence：只能是 high、medium、low 之一。
- notes：补充说明；没有则写空字符串。

注意：
1. 数组字段必须输出数组，不能输出字符串。
2. 原文没有的信息不要猜测，留空字符串或空数组。
3. 不要把“原文未明确”当成真实条件。
4. 如果该指南只是通知延期、名单公示、培训活动等，不是常规政策申报，也要如实结构化，并在 notes 说明。

policy_id：{policy_id}
title：{title}
source_guide_path：{guide_path}

申报指南 Markdown：
{markdown_text}
"""


def chat_completion(
    *,
    api_base: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    timeout: float,
    max_tokens: int,
    retries: int,
    enable_thinking: bool,
) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "stream": False,
        "enable_thinking": enable_thinking,
        "chat_template_kwargs": {"enable_thinking": enable_thinking},
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_url = api_base.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": DEFAULT_USER_AGENT,
    }
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(request_url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            return str(result["choices"][0]["message"]["content"]).strip()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise RuleBuildError(f"LLM request failed: HTTP {exc.code}: {body[:500]}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise RuleBuildError(f"LLM request failed: {exc}") from exc
    raise RuleBuildError("LLM request failed after retries.")


def parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuleBuildError(f"Model did not return a JSON object: {raw[:300]}")
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise RuleBuildError(f"Invalid JSON from model: {exc}: {raw[:500]}") from exc
    if not isinstance(value, dict):
        raise RuleBuildError("Model JSON is not an object.")
    return value


def normalize_rule(
    rule: dict[str, Any],
    *,
    policy_id: str,
    title: str,
    guide_path: Path,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key in REQUIRED_KEYS:
        value = rule.get(key, [] if key in LIST_KEYS else "")
        if key in LIST_KEYS:
            if value is None or value == "":
                value = []
            elif isinstance(value, str):
                value = [value]
            elif not isinstance(value, list):
                value = [str(value)]
            value = [str(item).strip() for item in value if str(item).strip()]
        else:
            value = "" if value is None else str(value).strip()
        normalized[key] = value

    normalized["policy_id"] = policy_id
    normalized["title"] = title
    normalized["source_guide_path"] = str(guide_path)
    if normalized["confidence"] not in {"high", "medium", "low"}:
        normalized["confidence"] = "medium"
    return normalized


def build_rule_for_file(path: Path, args: argparse.Namespace) -> dict[str, Any]:
    markdown_text = path.read_text(encoding="utf-8")
    if args.max_input_chars and len(markdown_text) > args.max_input_chars:
        raise RuleBuildError(
            f"Guide is {len(markdown_text)} characters, exceeding --max-input-chars={args.max_input_chars}."
        )
    policy_id = policy_id_from_path(path)
    title = title_from_path(path)
    raw = chat_completion(
        api_base=args.api_base,
        api_key=args.api_key,
        model=args.model,
        messages=[
            {"role": "system", "content": system_prompt()},
            {
                "role": "user",
                "content": user_prompt(
                    policy_id=policy_id,
                    title=title,
                    guide_path=path,
                    markdown_text=markdown_text,
                ),
            },
        ],
        timeout=args.timeout,
        max_tokens=args.max_output_tokens,
        retries=args.retries,
        enable_thinking=args.enable_thinking,
    )
    return normalize_rule(
        parse_json_object(raw),
        policy_id=policy_id,
        title=title,
        guide_path=path,
    )


def main() -> int:
    args = parse_args()
    guide_dir = args.guide_dir or (args.output_dir / DEFAULT_GUIDE_DIR_NAME)
    paths = guide_files(guide_dir)
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        raise RuleBuildError(f"No declaration guides found in {guide_dir}")

    args.rules_file.parent.mkdir(parents=True, exist_ok=True)
    if args.rules_file.exists() and not args.force:
        raise RuleBuildError(f"Rules file exists, pass --force to overwrite: {args.rules_file}")

    rows: list[dict[str, Any]] = []
    errors = 0
    with args.rules_file.open("w", encoding="utf-8") as file:
        for index, path in enumerate(paths, start=1):
            try:
                rule = build_rule_for_file(path, args)
                rows.append(rule)
                file.write(json.dumps(rule, ensure_ascii=False, sort_keys=True) + "\n")
                file.flush()
                print(f"[{index}/{len(paths)}] Structured: {path.name}")
            except Exception as exc:
                errors += 1
                error_row = {
                    "policy_id": policy_id_from_path(path),
                    "title": title_from_path(path),
                    "source_guide_path": str(path),
                    "status": "error",
                    "error": str(exc),
                }
                file.write(json.dumps(error_row, ensure_ascii=False, sort_keys=True) + "\n")
                file.flush()
                print(f"[{index}/{len(paths)}] Error: {path.name}: {exc}", file=sys.stderr)
            if args.delay:
                time.sleep(args.delay)

    print(f"Rules written: {len(rows)}")
    print(f"Errors: {errors}")
    print(f"Rules file: {args.rules_file.resolve()}")
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
