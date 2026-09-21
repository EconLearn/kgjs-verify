"""Turn a plain-English description of an economics graph into KGJS YAML.

The model is not trusted to get it right first time. Every candidate is run through the
checkers in this repo (schema, renderer, dangling references) and any failure is handed back
to the model as a repair instruction. The loop stops when the checks pass or the attempts run
out, and it reports honestly which of those happened.

Retrieval is plain TF-IDF over the descriptions in corpus/kgjs_examples.jsonl, so choosing the
few-shot examples costs nothing and needs no embedding call.

Needs OPENAI_API_KEY in the environment. Nothing is written outside --out.

Usage:
  generate.py "a supply and demand graph where demand shifts right" --out graph.yml
  generate.py --eval 12          # hold-out run over the docs corpus
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CORPUS = ROOT / "corpus" / "kgjs_examples.jsonl"

DEFAULT_MODEL = os.environ.get("KGJS_MODEL", "gpt-4o-mini")


# ---------------------------------------------------------------- retrieval


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


class Retriever:
    """Tiny TF-IDF index over the corpus descriptions."""

    def __init__(self, records: list[dict]):
        self.records = records
        self.docs = [_tokens(r["heading"] + " " + r["description"]) for r in records]
        df: Counter = Counter()
        for d in self.docs:
            df.update(set(d))
        n = len(self.docs)
        self.idf = {t: math.log(n / (1 + c)) for t, c in df.items()}
        self.vecs = [self._vec(d) for d in self.docs]

    def _vec(self, toks: list[str]) -> dict[str, float]:
        tf = Counter(toks)
        v = {t: (1 + math.log(c)) * self.idf.get(t, 0.0) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    def top(self, query: str, k: int = 4, exclude: int | None = None) -> list[dict]:
        q = self._vec(_tokens(query))
        scored = []
        for i, v in enumerate(self.vecs):
            if i == exclude:
                continue
            s = sum(q.get(t, 0.0) * x for t, x in v.items())
            scored.append((s, i))
        scored.sort(reverse=True)
        return [self.records[i] for _, i in scored[:k]]


# ---------------------------------------------------------------- checking


def run_checks(yaml_path: Path, repo: str, schema: str, bundle: str | None) -> dict:
    """Run check.js and refcheck.js and return a combined, machine-readable verdict."""
    out: dict = {}
    cmd = ["node", str(HERE / "check.js"), "--repo", repo, "--schema", schema]
    if bundle:
        cmd += ["--bundle", bundle]
    cmd.append(str(yaml_path))
    p = subprocess.run(cmd, capture_output=True, text=True)
    try:
        out["structure"] = json.loads(p.stdout)
    except json.JSONDecodeError:
        out["structure"] = {"error": (p.stderr or p.stdout)[:400]}

    p = subprocess.run(
        ["node", str(HERE / "refcheck.js"), "--repo", repo, str(yaml_path)],
        capture_output=True,
        text=True,
    )
    try:
        out["references"] = json.loads(p.stdout)
    except json.JSONDecodeError:
        out["references"] = {"error": (p.stderr or p.stdout)[:400]}
    return out


def verdict(checks: dict) -> tuple[bool, list[str]]:
    """Reduce the checker output to pass/fail plus instructions the model can act on."""
    problems: list[str] = []
    s = checks.get("structure", {})
    if isinstance(s, dict):
        if s.get("parse", {}).get("ok") is False:
            problems.append(f"The YAML does not parse: {s['parse'].get('error')}")
        sch = s.get("schema", {})
        if sch.get("ok") is False:
            for e in sch.get("errors", [])[:6]:
                off = e.get("offending")
                problems.append(
                    f"Schema: at {e.get('at')}, {e.get('problem')}"
                    + (f" (offending: {off})" if off else "")
                )
        rnd = s.get("render") or {}
        if rnd.get("ok") is False:
            problems.append(f"The engine threw while building the graph: {rnd.get('error')}")
        elif rnd.get("warning"):
            problems.append(f"Rendered but {rnd['warning']}.")
    r = checks.get("references", {})
    if isinstance(r, dict):
        for d in r.get("dangling", [])[:6]:
            hint = f" Did you mean {d['did_you_mean']}?" if d.get("did_you_mean") else ""
            problems.append(
                f"Reference {d['reference']} at {d['at']} is never declared.{hint}"
            )
    return (len(problems) == 0), problems


# ---------------------------------------------------------------- generation

SYSTEM = """You write KGJS (KineticGraphs) graph definitions in YAML. KGJS is the engine behind EconGraphs, used to draw economics diagrams.

Rules:
- Output YAML only. No prose, no markdown fences.
- The document's top-level keys are some of: schema, params, calcs, colors, layout.
- `layout` holds exactly one layout name (usually OneGraph), which holds `graph`, which holds `objects`.
- Each entry of `objects` is a single-key mapping whose key is the object type, e.g. `- Point:` or `- Line:` or `- Curve:`.
- Anything you reference as params.x, calcs.y or colors.z must be declared. params is a list of {name, value, min, max}; calcs and colors are maps. Naming an object also registers a calc under that name.
- The base colour palette (blue, orange, green, red, purple, brown, magenta, grey, gray, olive) is always available without declaring it."""


def build_prompt(description: str, examples: list[dict]) -> list[dict]:
    shots = []
    for ex in examples:
        shots.append({"role": "user", "content": ex["description"].strip()[-400:]})
        shots.append({"role": "assistant", "content": ex["yaml"]})
    return (
        [{"role": "system", "content": SYSTEM}]
        + shots
        + [{"role": "user", "content": description}]
    )


def strip_fences(text: str) -> str:
    text = re.sub(r"^\s*```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip() + "\n"


def call_model(messages: list[dict], model: str) -> str:
    from openai import OpenAI

    client = OpenAI()
    resp = client.chat.completions.create(
        model=model, messages=messages, temperature=0
    )
    return strip_fences(resp.choices[0].message.content or "")


def generate(
    description: str,
    retriever: Retriever,
    repo: str,
    schema: str,
    bundle: str | None,
    model: str,
    attempts: int = 3,
    exclude: int | None = None,
) -> dict:
    examples = retriever.top(description, k=4, exclude=exclude)
    messages = build_prompt(description, examples)
    trace = []
    yaml_text = ""
    for attempt in range(1, attempts + 1):
        yaml_text = call_model(messages, model)
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
            fh.write(yaml_text)
            tmp = Path(fh.name)
        checks = run_checks(tmp, repo, schema, bundle)
        tmp.unlink(missing_ok=True)
        ok, problems = verdict(checks)
        trace.append({"attempt": attempt, "ok": ok, "problems": problems})
        if ok:
            return {"ok": True, "yaml": yaml_text, "attempts": attempt, "trace": trace}
        messages = messages + [
            {"role": "assistant", "content": yaml_text},
            {
                "role": "user",
                "content": "These checks failed. Fix them and return the corrected YAML only:\n"
                + "\n".join(f"- {p}" for p in problems),
            },
        ]
    return {"ok": False, "yaml": yaml_text, "attempts": attempts, "trace": trace}


# ---------------------------------------------------------------- cli


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("description", nargs="?")
    ap.add_argument("--out")
    ap.add_argument("--repo", required=True, help="a kgjs checkout")
    ap.add_argument("--schema", required=True, help="kg.strict.schema.json")
    ap.add_argument("--bundle", default=None)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--eval", type=int, default=0, help="hold-out run over N corpus items")
    args = ap.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set in the environment.", file=sys.stderr)
        return 2

    records = [json.loads(line) for line in CORPUS.open()]
    retriever = Retriever(records)

    if args.eval:
        usable = [i for i, r in enumerate(records) if len(r["description"]) > 120]
        step = max(1, len(usable) // args.eval)
        picked = usable[::step][: args.eval]
        first_try = passed = 0
        for n, i in enumerate(picked, 1):
            rec = records[i]
            res = generate(
                rec["description"], retriever, args.repo, args.schema, args.bundle,
                args.model, args.attempts, exclude=i,
            )
            passed += res["ok"]
            first_try += res["ok"] and res["attempts"] == 1
            status = "pass" if res["ok"] else "FAIL"
            print(
                f"[{n}/{len(picked)}] {status} after {res['attempts']} attempt(s)"
                f"  <- {rec['source']} ({rec['heading'] or 'no heading'})",
                flush=True,
            )
            if not res["ok"]:
                for p in res["trace"][-1]["problems"][:3]:
                    print(f"        {p}", flush=True)
        print(
            f"\n{passed}/{len(picked)} produced a graph that passes every check "
            f"({first_try} on the first attempt, {passed - first_try} needed repair)."
        )
        return 0

    if not args.description:
        ap.error("give a description, or use --eval")
    res = generate(
        args.description, retriever, args.repo, args.schema, args.bundle,
        args.model, args.attempts,
    )
    for t in res["trace"]:
        mark = "ok" if t["ok"] else "failed"
        print(f"attempt {t['attempt']}: {mark}", file=sys.stderr)
        for p in t["problems"]:
            print(f"   {p}", file=sys.stderr)
    if args.out:
        Path(args.out).write_text(res["yaml"])
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(res["yaml"])
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
