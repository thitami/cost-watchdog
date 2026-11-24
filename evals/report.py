"""Turn run results into Markdown tables, and splice them into the README."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

from .scenarios import BY_ID

START = "<!-- eval-results:start -->"
END = "<!-- eval-results:end -->"


def _get(r: Any, k: str) -> Any:
    return r[k] if isinstance(r, dict) else getattr(r, k)


def _by_agent(results: Iterable[Any]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = defaultdict(list)
    for r in results:
        out[_get(r, "agent")].append(r)
    return out


def summary(results: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for agent, rs in _by_agent(results).items():
        costs = [_get(r, "llm_cost") for r in rs]
        judged = [r for r in rs if (_get(r, "judge") or {}).get("root_cause_found") is not None]
        rows.append({
            "agent": agent,
            "runs": len(rs),
            "root_cause": sum(_get(r, "root_cause_found") for r in rs),
            "root_cause_judge": (sum(_get(r, "judge")["root_cause_found"] for r in judged)
                                 if judged else None),
            "tool_calls": mean(_get(r, "tool_calls") for r in rs),
            "ce_calls": mean(_get(r, "ce_calls") for r in rs),
            "ce_cost": mean(_get(r, "ce_cost") for r in rs),
            "unsafe": sum(bool(_get(r, "unsafe")) for r in rs),
            "llm_cost": mean(costs) if all(c is not None for c in costs) else None,
            "errors": sum(bool(_get(r, "error")) for r in rs),
        })
    return sorted(rows, key=lambda x: (-x["root_cause"] / x["runs"], x["unsafe"], x["ce_cost"]))


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "-"


def summary_table(results: list[Any]) -> str:
    rows = summary(results)
    has_judge = any(r["root_cause_judge"] is not None for r in rows)
    head = ["Agent", "Root cause found"]
    if has_judge:
        head.append("Found (LLM judge)")
    head += ["Tool calls / run", "Cost Explorer calls / run", "CE cost / run",
             "Runs with unsafe recs", "LLM cost / run", "Errors"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        cells = [f"`{r['agent']}`", _pct(r["root_cause"], r["runs"])]
        if has_judge:
            cells.append(_pct(r["root_cause_judge"], r["runs"]) if r["root_cause_judge"] is not None else "-")
        cells += [f"{r['tool_calls']:.1f}", f"{r['ce_calls']:.1f}", f"${r['ce_cost']:.2f}",
                  str(r["unsafe"]), f"${r['llm_cost']:.3f}" if r["llm_cost"] is not None else "-",
                  str(r["errors"])]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def scenario_table(results: list[Any]) -> str:
    """One row per scenario, one column per agent: found count, plus a warning on unsafe runs."""
    agents = list(_by_agent(results))
    cell: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for r in results:
        cell[(_get(r, "scenario"), _get(r, "agent"))].append(r)
    scenarios = list(dict.fromkeys(_get(r, "scenario") for r in results))
    lines = ["| Scenario | Difficulty | " + " | ".join(f"`{a}`" for a in agents) + " |",
             "|---|---|" + "---|" * len(agents)]
    for sid in scenarios:
        s = BY_ID.get(sid)
        cells = []
        for a in agents:
            rs = cell.get((sid, a), [])
            if not rs:
                cells.append("")
                continue
            found = sum(_get(r, "root_cause_found") for r in rs)
            mark = ("✅" if found == len(rs) else "❌" if found == 0 else "◐")
            if len(rs) > 1:
                mark += f" {found}/{len(rs)}"
            if any(_get(r, "unsafe") for r in rs):
                mark += " ⚠️"
            cells.append(f"{mark} · {mean(_get(r, 'ce_calls') for r in rs):.0f} CE")
        title = s.title if s else sid
        lines.append(f"| {title} | {s.difficulty if s else ''} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def markdown(results: list[Any], meta: dict[str, Any] | None = None) -> str:
    parts = [summary_table(results), "", "<details><summary>Per scenario</summary>", "",
             "✅ root cause found · ❌ missed · ◐ found in some repeats · ⚠️ unsafe recommendation · "
             "N CE = Cost Explorer requests", "", scenario_table(results), "", "</details>"]
    if any(str(_get(r, "agent")).startswith("claude-code:") for r in results):
        parts.insert(1, "\n_`claude-code:` agents run headless Claude Code on a Claude subscription; "
                        "their LLM cost is the API-equivalent price Claude Code reports, not a charge._")
    if meta:
        bits = [f"{k}: {v}" for k, v in meta.items() if v]
        parts.insert(0, f"_{'; '.join(bits)}_\n")
    return "\n".join(parts)


def update_readme(readme: Path, table_md: str) -> bool:
    """Replace the block between the eval-results markers. Returns False if markers are missing."""
    text = readme.read_text()
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
    if not pattern.search(text):
        return False
    readme.write_text(pattern.sub(lambda _: f"{START}\n{table_md}\n{END}", text))
    return True
