"""Build a (description -> KGJS YAML) corpus from the kgjs documentation.

Every example in the kgjs docs sits inside a <div class="codePreview"> block, and the prose
immediately above it says what the graph is meant to show. That gives a labelled corpus of
natural-language descriptions paired with correct YAML, which is exactly what a
description-to-YAML tool needs for retrieval, few-shot prompting and evaluation.

Usage: extract_corpus.py <kgjs-checkout> <out.jsonl>
"""
import json, re, sys, pathlib, html

repo = pathlib.Path(sys.argv[1])
out = pathlib.Path(sys.argv[2])

BLOCK = re.compile(r'<div[^>]*class="(?:codePreview|kg-container)"[^>]*>(.*?)</div>', re.S)

def clean_prose(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'`([^`]*)`', r'\1', text)
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)   # markdown links -> label
    text = re.sub(r'\s+', ' ', text).strip()
    return text

records = []
for md in sorted(repo.glob("docs/**/*.md")):
    raw = md.read_text(encoding="utf-8", errors="ignore")
    # strip jekyll front matter
    body = re.sub(r'\A---\n.*?\n---\n', '', raw, flags=re.S)
    pos = 0
    heading = ""
    for m in BLOCK.finditer(body):
        before = body[pos:m.start()]
        for h in re.finditer(r'^#{1,4}\s+(.*)$', before, re.M):
            heading = h.group(1).strip()
        prose = clean_prose(before)
        yaml_text = m.group(1)
        # drop leading/trailing blank lines but keep indentation
        lines = [l for l in yaml_text.split("\n")]
        while lines and not lines[0].strip(): lines.pop(0)
        while lines and not lines[-1].strip(): lines.pop()
        yaml_text = "\n".join(lines)
        if "layout:" not in yaml_text and "params" not in yaml_text:
            pos = m.end(); continue
        records.append({
            "source": str(md.relative_to(repo)),
            "heading": heading,
            "description": prose[-600:],          # the prose right before the example
            "yaml": yaml_text,
        })
        pos = m.end()

with out.open("w") as f:
    for r in records:
        f.write(json.dumps(r) + "\n")

print(f"{len(records)} examples from {len(set(r['source'] for r in records))} files -> {out}")
longest = max(records, key=lambda r: len(r["yaml"]))
print(f"longest example: {longest['source']} ({len(longest['yaml'])} chars, heading '{longest['heading']}')")
withprose = sum(1 for r in records if len(r["description"]) > 80)
print(f"examples with substantial prose above them: {withprose}")
