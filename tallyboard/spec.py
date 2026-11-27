"""Job spec files: a committed JSON description of a repeatable job.

    {
      "name": "my-render",                 # key that links repeat runs together
      "title": "Render all scenes",
      "base_dir": "..",                    # relative to the spec file (default: see below)
      "vars": {"batch": "1"},              # {batch} is substituted in strings; --var overrides
      "items": [ {"id": "a", "group": "G", "output": "out/a.png", "cmd": "make a"} ],
      "item_sources": [                    # expand items from files that exist at start
        {"glob": "src/*/*.blend", "id": "{parent}/{stem}", "group": "{parent}",
         "label": "{stem}", "label_sub": {"_final$": ""},
         "output": "out/{parent}/{stem}.png"}
      ],
      "workers": [ {"name": "w1", "pgrep": "blender .*--batch 1"} ]
    }

Default base_dir: the parent of a `.tallyboard/` (or `.jobboard/`) folder
holding the spec, else the spec file's own folder.
"""
import glob
import json
import os
import re

from .store import StoreError

_VAR = re.compile(r"\{(\w+)\}")


def fill(value, mapping):
    """Replace {key} for keys present in mapping; leave other braces alone."""
    if isinstance(value, str):
        return _VAR.sub(lambda m: str(mapping[m.group(1)]) if m.group(1) in mapping
                        else m.group(0), value)
    if isinstance(value, list):
        return [fill(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, mapping) for k, v in value.items()}
    return value


def _natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def default_base_dir(spec_path):
    d = os.path.dirname(os.path.abspath(spec_path))
    return os.path.dirname(d) if os.path.basename(d) in (".tallyboard", ".jobboard") else d


def load_spec(path, base_dir=None, overrides=None):
    """Return a dict ready for Store.create(**...) plus 'spec' metadata."""
    with open(path) as fh:
        try:
            raw = json.load(fh)
        except ValueError as e:
            raise StoreError(f"{path}: invalid JSON: {e}") from None
    return build_from_spec(raw, path, base_dir=base_dir, overrides=overrides)


def build_from_spec(raw, path, base_dir=None, overrides=None):
    variables = dict(raw.get("vars") or {})
    variables.update(overrides or {})
    if base_dir is None:
        if raw.get("base_dir"):
            base_dir = os.path.join(os.path.dirname(os.path.abspath(path)),
                                    os.path.expanduser(fill(raw["base_dir"], variables)))
        else:
            base_dir = default_base_dir(path)
    base_dir = os.path.abspath(os.path.expanduser(base_dir))
    variables.setdefault("base_dir", base_dir)

    items = [fill(dict(it) if isinstance(it, dict) else {"id": it}, variables)
             for it in raw.get("items") or []]
    for src in raw.get("item_sources") or []:
        items.extend(expand_source(fill(src, variables), base_dir))

    name = raw.get("name") or os.path.splitext(os.path.basename(path))[0]
    return {
        "title": fill(raw.get("title") or name, variables),
        "items": items,
        "workers": [fill(w, variables) for w in raw.get("workers") or []],
        "base_dir": base_dir,
        "auto_finish": raw.get("auto_finish", True),
        "spec": {"key": name, "name": name, "path": os.path.abspath(path),
                 "vars": {k: v for k, v in variables.items() if k != "base_dir"}},
    }


def expand_source(src, base_dir):
    """Expand one item_sources entry into concrete items."""
    pattern = src.get("glob")
    if not pattern:
        raise StoreError(f"item_sources entry needs 'glob': {src!r}")
    matches = sorted(glob.glob(os.path.join(base_dir, pattern)), key=_natural_key)
    out = []
    for m in matches:
        rel = os.path.relpath(m, base_dir)
        name = os.path.basename(m)
        ctx = {
            "path": rel, "name": name, "stem": os.path.splitext(name)[0],
            "ext": os.path.splitext(name)[1], "parent": os.path.basename(os.path.dirname(m)),
            "dir": os.path.dirname(rel),
        }
        item = {
            "id": fill(src.get("id", "{path}"), ctx),
            "label": fill(src.get("label", "{stem}"), ctx),
            "group": fill(src["group"], ctx) if src.get("group") else None,
            "output": fill(src["output"], ctx) if src.get("output") else None,
            "cmd": fill(src["cmd"], ctx) if src.get("cmd") else None,
            "min_files": src.get("min_files", 1),
        }
        for rx, repl in (src.get("label_sub") or {}).items():
            item["label"] = re.sub(rx, repl, item["label"])
        out.append(item)
    if not out and not src.get("allow_empty"):
        raise StoreError(f"item_sources glob matched nothing: {os.path.join(base_dir, pattern)}")
    return out
