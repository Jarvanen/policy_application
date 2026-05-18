#!/usr/bin/env python3
"""
Ask questions against the generated Xiongan policy LLM wiki.

Retrieval is local and tool-style: grep collection pages, follow wiki links,
grep candidate policies, then send the resulting evidence bundle to the model.
No vector search or LLM reranking is used.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from policy_wiki_search import (
    DEFAULT_WIKI_DIR,
    EvidenceBundle,
    build_evidence_bundle,
    evidence_context,
    print_bundle,
    rel,
)


DEFAULT_API_BASE = "http://192.168.211.108:8000/v1"
DEFAULT_API_KEY = "EMPTY"
DEFAULT_MODEL = "/data3/yangsien/models/Qwen3.6-27B"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


class WikiAskError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask a question against the Xiongan policy llm_wiki.")
    parser.add_argument("question", nargs="*", help="Question text. You can also pass --question.")
    parser.add_argument("--question", dest="question_option", default="")
    parser.add_argument("--wiki-dir", type=Path, default=DEFAULT_WIKI_DIR)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--retries", type=int, default=2)
    thinking_group = parser.add_mutually_exclusive_group()
    thinking_group.add_argument("--enable-thinking", action="store_true", help="Enable model thinking mode.")
    thinking_group.add_argument("--disable-thinking", action="store_true", help="Disable model thinking mode. This is the default.")
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument("--max-context-chars", type=int, default=50000)
    parser.add_argument("--show-sources", action="store_true")
    parser.add_argument("--trace-search", action="store_true")
    parser.add_argument("--no-stream-tools", action="store_true", help="Do not print live local tool-call events.")
    parser.add_argument("--no-stream-answer", action="store_true", help="Use non-streaming model output.")
    parser.add_argument("--no-llm", action="store_true", help="Only run local search and show evidence pages.")
    parser.add_argument("--save", action="store_true", help="Save the answer into llm_wiki/qa/.")
    return parser.parse_args()


def resolve_question(args: argparse.Namespace) -> str:
    question = args.question_option.strip() or " ".join(args.question).strip()
    if not question:
        raise WikiAskError("Question is required.")
    return question


def system_prompt() -> str:
    return (
        "你是雄安新区政策知识库问答助手。检索已经由本地工具完成，"
        "你只能根据给定 evidence bundle 回答。不要编造政策条件、时间、电话或企业资质。"
        "如果 evidence 不足，明确说明需要回查原文或人工核实。"
    )


def user_prompt(question: str, context: str) -> str:
    return f"""请根据下面的本地检索证据回答用户问题。

回答要求：
1. 先给出简短结论。
2. 如果是政策匹配或申报建议，按“推荐关注 / 暂不建议 / 需要补充的信息”组织。
3. 每个关键判断都标注来源，来源使用 evidence 中的 wiki 页面路径，例如 `policies/003_xxx.md`。
4. 区分“当前可关注”“未明确期限”“已过期”“非申报类”。
5. 如果证据不足，写“需回查原文或人工核实”，不要猜。

用户问题：
{question}

本地检索证据：
{context}
"""


def build_chat_payload(
    *,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    enable_thinking: bool,
    stream: bool,
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "stream": stream,
        "enable_thinking": enable_thinking,
        "chat_template_kwargs": {"enable_thinking": enable_thinking},
    }


def request_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": DEFAULT_USER_AGENT,
    }


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
    payload = build_chat_payload(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
        stream=False,
    )
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_url = api_base.rstrip("/") + "/chat/completions"
    headers = request_headers(api_key)
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
            raise WikiAskError(f"LLM request failed: HTTP {exc.code}: {body[:500]}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise WikiAskError(f"LLM request failed: {exc}") from exc
    raise WikiAskError("LLM request failed after retries.")


def chat_completion_stream(
    *,
    api_base: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    timeout: float,
    max_tokens: int,
    retries: int,
    enable_thinking: bool,
    output: TextIO,
) -> str:
    payload = build_chat_payload(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
        stream=True,
    )
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_url = api_base.rstrip("/") + "/chat/completions"
    headers = request_headers(api_key)

    for attempt in range(1, retries + 1):
        answer_parts: list[str] = []
        request = urllib.request.Request(request_url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    chunk_text = line[5:].strip()
                    if chunk_text == "[DONE]":
                        break
                    try:
                        chunk = json.loads(chunk_text)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if content is None:
                        content = (choices[0].get("message") or {}).get("content")
                    if not content:
                        continue
                    answer_parts.append(str(content))
                    print(str(content), end="", file=output, flush=True)
            print(file=output, flush=True)
            return "".join(answer_parts).strip()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code in (429, 500, 502, 503, 504) and not answer_parts and attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise WikiAskError(f"LLM streaming request failed: HTTP {exc.code}: {body[:500]}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            if not answer_parts and attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise WikiAskError(f"LLM streaming request failed: {exc}") from exc
    raise WikiAskError("LLM streaming request failed after retries.")


def print_tool_event(event: dict[str, Any], output: TextIO = sys.stdout) -> None:
    event_type = str(event.get("event") or "tool_result")
    name = str(event.get("name") or "unknown")
    payload = {key: value for key, value in event.items() if key not in {"event", "name"}}
    label = "[tool_call]" if event_type == "tool_call" else "[tool_result]"
    if payload:
        print(f"{label} {name} {json.dumps(payload, ensure_ascii=False)}", file=output, flush=True)
    else:
        print(f"{label} {name}", file=output, flush=True)


def print_source_list(bundle: EvidenceBundle, wiki_dir: Path) -> None:
    print("\n[sources]")
    for index, page in enumerate(bundle.pages, start=1):
        status = f" | {page.status}" if page.status else ""
        print(f"{index}. {rel(page.path, wiki_dir)}{status}")


def slug(value: str, max_len: int = 80) -> str:
    import re

    text = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", value)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text[:max_len].strip(" .") or "question")


def save_answer(wiki_dir: Path, question: str, answer: str, bundle: EvidenceBundle) -> Path:
    qa_dir = wiki_dir / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    stem = datetime.now().strftime("%Y%m%d_%H%M%S_") + slug(question)
    path = qa_dir / f"{stem}.md"
    sources = [rel(page.path, wiki_dir) for page in bundle.pages]
    text = "\n".join(
        [
            "---",
            "type: qa",
            f"created: {json.dumps(datetime.now().strftime('%Y-%m-%d %H:%M:%S'), ensure_ascii=False)}",
            f"question: {json.dumps(question, ensure_ascii=False)}",
            "sources:",
            *[f"  - {json.dumps(source, ensure_ascii=False)}" for source in sources],
            "query_terms:",
            *[f"  - {json.dumps(term, ensure_ascii=False)}" for term in bundle.query_terms],
            "---",
            "",
            f"# {question}",
            "",
            "## 回答",
            "",
            answer,
            "",
            "## 本地检索轨迹",
            "",
            *[f"- {step}" for step in bundle.trace],
            "",
            "## 来源页面",
            "",
            *[f"- `{source}`" for source in sources],
            "",
        ]
    )
    path.write_text(text, encoding="utf-8")

    log_path = wiki_dir / "log.md"
    old_log = log_path.read_text(encoding="utf-8") if log_path.exists() else "# 维护日志\n"
    log_entry = (
        f"\n## [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] query | {question[:80]}\n\n"
        f"- QA page: `qa/{path.name}`\n"
        f"- Sources: {', '.join(sources[:8])}\n"
    )
    log_path.write_text(old_log.rstrip() + "\n" + log_entry, encoding="utf-8")
    return path


def main() -> int:
    args = parse_args()
    question = resolve_question(args)
    stream_tools = not args.no_stream_tools
    stream_answer = not args.no_stream_answer
    enable_thinking = bool(args.enable_thinking and not args.disable_thinking)
    bundle = build_evidence_bundle(
        wiki_dir=args.wiki_dir,
        question=question,
        top_k=args.top_k,
        event_callback=print_tool_event if stream_tools else None,
    )

    if args.no_llm or args.trace_search:
        print()
        print_bundle(bundle, args.wiki_dir, include_trace=args.trace_search or args.no_llm)
        if args.no_llm:
            return 0

    if stream_tools:
        print_tool_event(
            {
                "event": "tool_call",
                "name": "build_evidence_context",
                "pages": len(bundle.pages),
                "max_context_chars": args.max_context_chars,
            }
        )
    context = evidence_context(bundle, args.wiki_dir, args.max_context_chars)
    if not context.strip():
        raise WikiAskError("Local search produced no evidence context.")
    if stream_tools:
        print_tool_event(
            {
                "event": "tool_result",
                "name": "build_evidence_context",
                "context_chars": len(context),
            }
        )

    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": user_prompt(question, context)},
    ]
    if stream_tools:
        print_tool_event(
            {
                "event": "tool_call",
                "name": "chat_completion",
                "model": args.model,
                "stream": stream_answer,
                "enable_thinking": enable_thinking,
                "max_output_tokens": args.max_output_tokens,
            }
        )

    print("\n[final_answer]", flush=True)
    if stream_answer:
        answer = chat_completion_stream(
            api_base=args.api_base,
            api_key=args.api_key,
            model=args.model,
            messages=messages,
            timeout=args.timeout,
            max_tokens=args.max_output_tokens,
            retries=args.retries,
            enable_thinking=enable_thinking,
            output=sys.stdout,
        )
    else:
        answer = chat_completion(
            api_base=args.api_base,
            api_key=args.api_key,
            model=args.model,
            messages=messages,
            timeout=args.timeout,
            max_tokens=args.max_output_tokens,
            retries=args.retries,
            enable_thinking=enable_thinking,
        )
        print(answer)

    if stream_tools:
        print_tool_event(
            {
                "event": "tool_result",
                "name": "chat_completion",
                "answer_chars": len(answer),
            }
        )

    if args.show_sources:
        print_source_list(bundle, args.wiki_dir)

    if args.save:
        saved_path = save_answer(args.wiki_dir, question, answer, bundle)
        print(f"\nSaved QA page: {saved_path.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
    except WikiAskError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
