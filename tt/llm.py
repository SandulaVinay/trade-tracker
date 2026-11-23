"""Optional free LLM (Gemini free tier, Groq free tier, or local Ollama).

The LLM only rewrites/explains facts it is given. Every number in its answer must appear in the
supplied context; otherwise the answer is rejected and the rule-based text is used instead.
"""
from __future__ import annotations

import json
import re

import requests

from .config import (GEMINI_API_KEY, GEMINI_MODEL, GROQ_API_KEY, GROQ_MODEL, LLM_PROVIDER, OLLAMA_MODEL,
                     OLLAMA_URL)

SYSTEM = ("You are a careful Indian equity research assistant. Answer ONLY from the JSON context. "
          "Never invent prices, dates, percentages or news. If the context lacks something, say 'data missing'. "
          "Separate FACTS (with their date and source) from JUDGMENT. Keep under 250 words. "
          "This is analysis, not advice; the user decides.")


def enabled() -> bool:
    return (LLM_PROVIDER == "gemini" and bool(GEMINI_API_KEY)) or (LLM_PROVIDER == "groq" and bool(GROQ_API_KEY)) \
        or LLM_PROVIDER == "ollama"


def _call(prompt: str) -> str:
    if LLM_PROVIDER == "gemini":
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                          params={"key": GEMINI_API_KEY}, timeout=60,
                          json={"systemInstruction": {"parts": [{"text": SYSTEM}]},
                                "contents": [{"parts": [{"text": prompt}]}],
                                "generationConfig": {"temperature": 0.1}})
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    if LLM_PROVIDER == "groq":
        r = requests.post("https://api.groq.com/openai/v1/chat/completions", timeout=60,
                          headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                          json={"model": GROQ_MODEL, "temperature": 0.1,
                                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=120,
                      json={"model": OLLAMA_MODEL, "stream": False, "options": {"temperature": 0.1},
                            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]})
    r.raise_for_status()
    return r.json()["message"]["content"]


NUM = re.compile(r"(?<![\w.])-?\d[\d,]*\.?\d*")


def _numbers(s: str) -> set[str]:
    out = set()
    for m in NUM.findall(s):
        v = m.replace(",", "").rstrip(".")
        if v in ("", "-"):
            continue
        try:
            f = float(v)
        except ValueError:
            continue
        out.add(f"{f:.2f}")
        out.add(f"{round(f):d}" if abs(f) < 1e12 else v)
    return out


def verify_numbers(answer: str, context: str) -> list[str]:
    """Return numbers in the answer that are not present in the context (hallucination guard)."""
    ctx = _numbers(context)
    bad = []
    for m in NUM.findall(answer):
        v = m.replace(",", "").rstrip(".")
        try:
            f = float(v)
        except ValueError:
            continue
        if abs(f) <= 10 and float(f).is_integer():
            continue  # list numbering, small counts
        if f"{f:.2f}" not in ctx and f"{round(f):d}" not in ctx and f"{f * 100:.1f}" not in {f"{float(x):.1f}" for x in ctx if x.replace('.', '', 1).lstrip('-').isdigit()}:
            bad.append(m)
    return bad


def ask(question: str, context: dict) -> dict:
    if not enabled():
        return {"ok": False, "reason": "LLM not configured"}
    ctx = json.dumps(context, default=str)[:60000]
    try:
        ans = _call(f"Context JSON:\n{ctx}\n\nQuestion: {question}")
    except Exception as e:
        return {"ok": False, "reason": f"LLM call failed: {e}"}
    bad = verify_numbers(ans, ctx)
    if bad:
        return {"ok": False, "reason": f"LLM answer rejected: numbers not found in verified data: {bad[:5]}", "raw": ans}
    return {"ok": True, "text": ans}
