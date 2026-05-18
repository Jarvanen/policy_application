#!/usr/bin/env python3
"""
Generate declaration guides from converted policy Markdown files.

This script is intentionally separate from xiongan_policy_pdf_to_md.py. It reads
the conversion manifest, sends each Markdown document to an OpenAI-compatible
chat-completions API, and writes one declaration-guide Markdown file per source
document.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
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
DEFAULT_API_BASE = "http://192.168.211.108:8000/v1"
DEFAULT_API_KEY = "EMPTY"
DEFAULT_MODEL = "/data3/yangsien/models/Qwen3.6-27B"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


class GuideError(RuntimeError):
    pass


class ContextTooLongError(GuideError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize converted policy Markdown files into declaration guides."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory containing manifest.jsonl. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--guide-dir",
        type=Path,
        default=None,
        help=f"Guide output directory. Default: <output-dir>/{DEFAULT_GUIDE_DIR_NAME}",
    )
    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help=f"OpenAI-compatible API base. Default: {DEFAULT_API_BASE}",
    )
    parser.add_argument(
        "--api-key",
        default=DEFAULT_API_KEY,
        help=f"API key. Default: {DEFAULT_API_KEY}.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model name. Default: {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600,
        help="Request timeout in seconds. Default: 600.",
    )
    parser.add_argument(
        "--max-input-chars",
        type=int,
        default=1000000,
        help="Skip a document when the prompt input exceeds this many characters. Use 0 to disable. Default: 1000000.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=4096,
        help="Maximum output tokens for each LLM response. Default: 4096.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="Retries per LLM request. Default: 2.",
    )
    parser.add_argument(
        "--enable-thinking",
        action="store_true",
        help="Enable model thinking mode. Disabled by default for faster guide generation.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only summarize the first N Markdown documents.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate existing guides.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay between LLM calls in seconds. Default: 1.",
    )
    return parser.parse_args()


def sanitize_filename(value: str, *, fallback: str, max_length: int = 120) -> str:
    value = value.strip() or fallback
    value = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    if len(value) <= max_length:
        return value
    return value[:max_length].rstrip()


def load_api_key(cli_key: str) -> str:
    if cli_key.strip():
        return cli_key.strip()
    for env_name in ("SILICONFLOW_API_KEY", "OPENAI_API_KEY"):
        env_key = os.environ.get(env_name, "").strip()
        if env_key:
            return env_key
    raise GuideError("API key not found. Set SILICONFLOW_API_KEY or pass --api-key.")


def read_manifest(output_dir: Path) -> list[dict[str, Any]]:
    manifest = output_dir / "manifest.jsonl"
    if not manifest.exists():
        raise GuideError(f"Manifest not found: {manifest}")
    return [
        json.loads(line)
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def resolve_manifest_path(path_value: str, output_dir: Path) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    if path.exists():
        return path
    return output_dir / path


def split_policy_sections(markdown_text: str) -> tuple[str, list[tuple[str, str]]]:
    lines = markdown_text.splitlines()
    intro: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current_lines: list[str] = []
    in_section = False

    for line in lines:
        heading_match = re.match(r"^##\s+(正文|主文件：.+|附件\s+\d+：.+|解析问题)\s*$", line)
        if heading_match:
            if in_section and current_title is not None:
                sections.append((current_title, current_lines))
            current_title = heading_match.group(1).strip()
            current_lines = []
            in_section = True
            continue
        if in_section:
            current_lines.append(line)
        else:
            intro.append(line)

    if in_section and current_title is not None:
        sections.append((current_title, current_lines))

    return "\n".join(intro).strip(), [
        (title, "\n".join(lines).strip()) for title, lines in sections
    ]


def is_meaningful_document_text(text: str) -> bool:
    cleaned = re.sub(r"\s+", "", text or "")
    if len(cleaned) >= 180:
        return True
    return any(
        keyword in text
        for keyword in ("申报", "认定", "奖励", "补贴", "支持", "条件", "流程", "咨询")
    ) and len(cleaned) >= 80


def attachment_score(policy_title: str, attachment_title: str, body: str) -> int:
    title = attachment_title.lower()
    score = 0
    if ".pdf" in title:
        score += 30
    if any(keyword in attachment_title for keyword in ("通知", "办法", "措施", "细则", "指南", "方案", "政策")):
        score += 35
    if policy_title and policy_title[:12] in attachment_title:
        score += 45
    extra_keywords = (
        "申报书",
        "申报表",
        "汇总表",
        "承诺书",
        "承诺函",
        "推荐表",
        "申请表",
        "信息表",
        "统计表",
        "名单",
        "清单",
        "模板",
        "样表",
        "附件",
    )
    if any(keyword in attachment_title for keyword in extra_keywords):
        score -= 50
    if is_meaningful_document_text(body):
        score += 10
    return score


def strip_attachment_metadata(section_body: str) -> str:
    lines = []
    for line in section_body.splitlines():
        if line.startswith("- 原始文件："):
            continue
        if line.startswith("- 来源 URL："):
            continue
        if line.startswith("- 解析方式："):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def extract_main_document_markdown(markdown_text: str, title: str) -> tuple[str, str]:
    intro, sections = split_policy_sections(markdown_text)
    body_sections: list[str] = []
    saw_body_section = False
    attachments: list[tuple[int, str, str]] = []

    for section_title, section_body in sections:
        if section_title == "正文" or section_title.startswith("主文件："):
            saw_body_section = True
            clean_body = strip_attachment_metadata(section_body)
            if clean_body.strip():
                body_sections.append(f"## {section_title}\n\n" + clean_body.strip())
            continue
        attachment_match = re.match(r"附件\s+(\d+)：(.+)", section_title)
        if attachment_match:
            attachment_index = int(attachment_match.group(1))
            attachment_name = attachment_match.group(2).strip()
            attachments.append(
                (attachment_index, attachment_name, strip_attachment_metadata(section_body))
            )

    main_body = "\n\n".join(body_sections).strip()
    if main_body:
        return "\n\n".join(part for part in (intro, main_body) if part).strip(), "detail_content"
    if saw_body_section:
        return intro, "detail_content"

    if not attachments:
        return "\n\n".join(part for part in (intro, main_body) if part).strip(), "detail_content"

    pdf_attachments = [
        item for item in attachments if ".pdf" in item[1].lower()
    ]
    candidates = pdf_attachments or attachments
    scored = sorted(
        (
            (attachment_score(title, attachment_name, body), attachment_index, attachment_name, body)
            for attachment_index, attachment_name, body in candidates
        ),
        reverse=True,
    )
    best_score, best_index, best_name, best_body = scored[0]
    if best_score < 0 and main_body:
        return "\n\n".join(part for part in (intro, main_body) if part).strip(), "detail_content"

    selected = f"## 主文件：{best_name}\n\n{best_body.strip()}"
    return "\n\n".join(part for part in (intro, selected) if part).strip(), f"main_attachment_{best_index}"


def chat_completion(
    *,
    api_base: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    timeout: float,
    max_tokens: int = 2048,
    temperature: float = 0.1,
    retries: int = 2,
    enable_thinking: bool = False,
) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "enable_thinking": enable_thinking,
        "chat_template_kwargs": {
            "enable_thinking": enable_thinking,
        },
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
            raise GuideError(f"LLM request failed: HTTP {exc.code}: {body[:500]}") from exc
        except urllib.error.URLError as exc:
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise GuideError(f"LLM request failed: {exc.reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise GuideError(f"LLM request timed out: {exc}") from exc

    raise GuideError("LLM request failed after retries.")


def system_prompt() -> str:
    return (
        "你是政策申报材料分析助手。你的任务是根据用户提供的政策文件 Markdown，"
        "结合正文和附件内容提炼面向企业或个人的申报指南。不要编造原文没有的信息。"
    )


def final_prompt(title: str, document_text: str) -> str:
    return f"""请阅读下面的政策文件 Markdown，输出一份结构化“申报指南”。

要求：
1. 只使用原文信息，不要推测或编造。
2. 优先使用以下一级标题，并按这个顺序输出；原文没有明确内容的栏目不要输出：
   政策简介
   奖励额度/补贴标准
   注意事项
   申请对象
   申报条件
   申报材料
   办理流程
   申报时间
   咨询电话
3. “政策简介”要写成自然段，概括政策用途、支持方向、认定类别或核心定义；有原文定义时优先保留原文关键表述。
4. “奖励额度/补贴标准”只在原文有金额、比例、补贴、奖励、资助标准时输出。
5. “注意事项”提取期限、不得重复享受、账户/材料/格式要求、限制条件、重要提醒等；没有明确内容则不输出该栏目。
6. “申请对象”提取申报主体、适用对象、载体方向、企业类型等。
7. “申报条件”要尽量完整保留编号条件、基础条件、专项条件、硬性指标和比例阈值。
8. “办理流程”用于申报、审核、评审、公示、入库、拨付等步骤；原文写“申报流程”也统一输出为“办理流程”。
9. “咨询电话”必须尽量原样提取电话、联系人、技术支持电话、邮箱等联系方式。
10. 输出 Markdown，不要添加代码块，不要添加“以下是”等开场白。

文件标题：{title}

文件内容：
{document_text}
"""


def summarize_document(
    *,
    title: str,
    markdown_text: str,
    api_base: str,
    api_key: str,
    model: str,
    timeout: float,
    max_input_chars: int,
    max_output_tokens: int,
    retries: int,
    enable_thinking: bool,
) -> str:
    if max_input_chars > 0 and len(markdown_text) > max_input_chars:
        raise ContextTooLongError(
            f"Input is {len(markdown_text)} characters, exceeding --max-input-chars={max_input_chars}."
        )
    system = {"role": "system", "content": system_prompt()}
    return chat_completion(
        api_base=api_base,
        api_key=api_key,
        model=model,
        messages=[system, {"role": "user", "content": final_prompt(title, markdown_text)}],
        timeout=timeout,
        max_tokens=max_output_tokens,
        retries=retries,
        enable_thinking=enable_thinking,
    )


def write_guide_manifest(guide_dir: Path, rows: list[dict[str, Any]]) -> None:
    jsonl_path = guide_dir / "guide_manifest.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    csv_path = guide_dir / "guide_manifest.csv"
    fields = [
        "index",
        "title",
        "md_path",
        "guide_path",
        "input_source",
        "input_chars",
        "status",
        "error",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_error_guide(guide_path: Path, *, title: str, status: str, error: str) -> None:
    lines = [
        f"# {title} 申报指南",
        "",
        "生成失败",
        "",
        f"- 状态：{status}",
        f"- 原因：{error}",
        "",
    ]
    guide_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    api_key = load_api_key(args.api_key)
    output_dir: Path = args.output_dir
    guide_dir: Path = args.guide_dir or (output_dir / DEFAULT_GUIDE_DIR_NAME)
    guide_dir.mkdir(parents=True, exist_ok=True)

    manifest_rows = read_manifest(output_dir)
    guide_rows: list[dict[str, Any]] = []
    work_items: list[tuple[dict[str, Any], Path, Path, dict[str, Any]]] = []
    completed = 0
    errors = 0
    skipped = 0
    attempted = 0

    for row in manifest_rows:
        md_value = row.get("md_path")
        if not md_value:
            continue
        md_path = resolve_manifest_path(str(md_value), output_dir)
        guide_name = sanitize_filename(
            f"{Path(str(md_value)).stem}_申报指南.md",
            fallback=f"{row.get('index', 'unknown')}_申报指南.md",
        )
        guide_path = guide_dir / guide_name
        guide_row = {
            "index": row.get("index"),
            "title": row.get("title"),
            "md_path": str(md_path),
            "guide_path": str(guide_path),
            "input_source": "",
            "input_chars": "",
            "status": "pending",
            "error": "",
        }
        guide_rows.append(guide_row)
        work_items.append((row, md_path, guide_path, guide_row))

    write_guide_manifest(guide_dir, guide_rows)

    for row, md_path, guide_path, guide_row in work_items:
        if not md_path.exists():
            guide_row["status"] = "missing_markdown"
            guide_row["error"] = f"Markdown file not found: {md_path}"
            write_error_guide(
                guide_path,
                title=str(row.get("title") or guide_path.stem),
                status="missing_markdown",
                error=str(guide_row["error"]),
            )
            errors += 1
            write_guide_manifest(guide_dir, guide_rows)
            continue
        if args.limit and attempted >= args.limit:
            guide_row["status"] = "not_attempted"
            write_guide_manifest(guide_dir, guide_rows)
            continue
        attempted += 1
        if guide_path.exists() and not args.force:
            guide_row["status"] = "skipped"
            write_guide_manifest(guide_dir, guide_rows)
            continue

        try:
            source_markdown = md_path.read_text(encoding="utf-8")
            guide_row["input_source"] = "full_markdown"
            guide_row["input_chars"] = len(source_markdown)
            if not source_markdown.strip():
                guide_row["status"] = "empty_input"
                guide_row["error"] = "Markdown file is empty."
                write_error_guide(
                    guide_path,
                    title=str(row.get("title") or md_path.stem),
                    status="empty_input",
                    error=str(guide_row["error"]),
                )
                errors += 1
                write_guide_manifest(guide_dir, guide_rows)
                continue
            guide = summarize_document(
                title=str(row.get("title") or md_path.stem),
                markdown_text=source_markdown,
                api_base=args.api_base,
                api_key=api_key,
                model=args.model,
                timeout=args.timeout,
                max_input_chars=args.max_input_chars,
                max_output_tokens=args.max_output_tokens,
                retries=args.retries,
                enable_thinking=args.enable_thinking,
            )
            guide_path.write_text(guide.rstrip() + "\n", encoding="utf-8")
            guide_row["status"] = "summarized"
            completed += 1
            print(f"Summarized: {guide_path.name}")
            if args.delay:
                time.sleep(args.delay)
        except ContextTooLongError as exc:
            guide_row["status"] = "skipped_context_too_long"
            guide_row["error"] = str(exc)
            write_error_guide(
                guide_path,
                title=str(row.get("title") or md_path.stem),
                status="skipped_context_too_long",
                error=str(exc),
            )
            skipped += 1
            print(f"Skipped: {md_path.name}: {exc}", file=sys.stderr)
        except Exception as exc:
            guide_row["status"] = "error"
            guide_row["error"] = str(exc)
            write_error_guide(
                guide_path,
                title=str(row.get("title") or md_path.stem),
                status="error",
                error=str(exc),
            )
            errors += 1
            print(f"Error: {md_path.name}: {exc}", file=sys.stderr)
        write_guide_manifest(guide_dir, guide_rows)

    write_guide_manifest(guide_dir, guide_rows)
    print(f"Guides generated: {completed}")
    print(f"Skipped: {skipped}")
    print(f"Errors: {errors}")
    print(f"Guide directory: {guide_dir.resolve()}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
