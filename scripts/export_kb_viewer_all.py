import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
try:
    import yaml
except ImportError:
    yaml = None

def _parse_args():
    ap = argparse.ArgumentParser(description="Export OKF KBs to the viewer JSON bundle (prototype; to be formalized as okf export).")
    ap.add_argument("--base", default=str(Path.home() / "project" / "hermes-agents-quickstart" / "kb"), help="directory containing the KB folders")
    ap.add_argument("--out", default=str(Path.home() / "kb-viewer-data.json"), help="output JSON path")
    return ap.parse_args()

ARGS = _parse_args()
SRC = Path(ARGS.base)
OUT = Path(ARGS.out)
KBS = [
    ("kb-semiconductor-thermal", "半導体熱材料KB"),
    ("kb-shana-ops", "シャナ運用KB（KB#1）"),
    ("kb-ai-research", "AI研究KB（KB#2）"),
]
FALLBACKS = []

def scalar(val):
    v = val.strip()
    if len(v) >= 2 and ((v[0] == chr(34) and v[-1] == chr(34)) or (v[0] == chr(39) and v[-1] == chr(39))):
        v = v[1:-1]
    return v

def fallback_parse(fm_text):
    fm = {}
    lines = fm_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line and not line.startswith(" ") and not line.startswith("-") and ":" in line:
            key, _, val = line.partition(":")
            key = key.strip(); val = val.strip()
            if val.startswith("[") and "]" in val:
                inner = val[1:val.rindex("]")]
                fm[key] = [scalar(x) for x in inner.split(",") if x.strip()]
            elif val == "":
                items = []; j = i + 1
                while j < len(lines) and lines[j].lstrip().startswith("- "):
                    items.append(scalar(lines[j].lstrip()[2:])); j += 1
                if items:
                    fm[key] = items; i = j - 1
                else:
                    fm[key] = None
            else:
                fm[key] = scalar(val)
        i += 1
    return fm

def parse_note(path):
    text = path.read_text(encoding="utf-8")
    fm = {}; body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                if yaml is None:
                    raise ImportError("PyYAML unavailable")
                fm = yaml.safe_load(parts[1]) or {}
            except Exception:
                FALLBACKS.append(path.name)
                fm = fallback_parse(parts[1])
            body = parts[2].lstrip("\n")
    return fm, body

def md_targets(body):
    out = []; i = 0
    while True:
        j = body.find("](", i)
        if j < 0: break
        k = body.find(")", j + 2)
        if k < 0: break
        t = body[j + 2:k].strip(); i = k + 1
        if t.startswith("http"): continue
        t = t.split("#")[0].split("?")[0]
        if t.endswith(".md"): out.append(t)
    return out

def export_kb(kb_dir):
    cdir = kb_dir / "concepts"
    concept_files = sorted(p for p in cdir.glob("*.md") if p.name != "index.md") if cdir.is_dir() else []
    pdir = kb_dir / "patents"
    patent_files = sorted(p for p in pdir.glob("*.md") if not p.stem.startswith("index")) if pdir.is_dir() else []
    concept_slugs = {p.stem for p in concept_files}
    concepts = []; patent_to_concepts = {}
    for p in concept_files:
        fm, body = parse_note(p)
        slug = p.stem; links = []
        for t in md_targets(body):
            base = t.rsplit("/", 1)[-1][:-3]
            if "/patents/" in t:
                lst = patent_to_concepts.setdefault(base, [])
                if slug not in lst: lst.append(slug)
            elif base in concept_slugs and base != slug and base not in links:
                links.append(base)
        sources = fm.get("sources") or []
        concepts.append({"slug": slug, "type": fm.get("type"), "title": fm.get("title"), "description": fm.get("description"), "tags": fm.get("tags") or [], "scope": fm.get("scope"), "basis": fm.get("basis"), "sources_count": len(sources), "verified_at": fm.get("verified_at"), "links": links, "body": body})
    patents = []
    for p in patent_files:
        fm, _b = parse_note(p)
        patents.append({"slug": p.stem, "title": fm.get("title"), "tags": fm.get("tags") or [], "publication": fm.get("publication"), "related_concepts": patent_to_concepts.get(p.stem, [])})
    groups = []
    syn_path = kb_dir / "synonyms.json"
    if syn_path.is_file():
        groups = (json.loads(syn_path.read_text(encoding="utf-8")).get("groups") or [])
    return {"stats": {"concepts": len(concepts), "patents": len(patents), "total_notes": len(concepts) + len(patents), "synonym_groups": len(groups)}, "concepts": concepts, "patents": patents, "synonyms": {"groups": groups}}

kbs = []
for kb_id, label in KBS:
    d = export_kb(SRC / kb_id)
    d = {"id": kb_id, "label": label, **d}
    kbs.append(d)
    print(kb_id, d["stats"])

semi = kbs[0]
data = {"generated_at": datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds"), "kb": semi["id"], "stats": semi["stats"], "concepts": semi["concepts"], "patents": semi["patents"], "synonyms": semi["synonyms"], "kbs": kbs}
OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
print("bytes", OUT.stat().st_size)
print("fallbacks", FALLBACKS)
