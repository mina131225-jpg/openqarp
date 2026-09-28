#!/usr/bin/env python3
"""シフトデモのソルバを UI から呼び出す薄い橋渡し。

古典ソルバは常に利用可能。QAOA は qarp (OpenQARP) が import できるときだけ動く。
"""

from __future__ import annotations

import sys
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from typing import Any

# community_mina の親を path に入れ、既存デモを再利用する
_COMMUNITY = Path(__file__).resolve().parent.parent
if str(_COMMUNITY) not in sys.path:
    sys.path.insert(0, str(_COMMUNITY))

import shift_scheduling_demo as ssd  # noqa: E402


def default_scenario() -> dict[str, Any]:
    return deepcopy(ssd.DEFAULT_SCENARIO)


def load_scenario_json(text: str) -> dict[str, Any]:
    import json

    data = json.loads(text)
    merged = default_scenario()
    merged.update(data)
    return merged


def validate(scenario: dict[str, Any]) -> None:
    ssd.validate_scenario(scenario)


def qarp_available() -> bool:
    return ssd.qarp_available()


def run_classical_week(scenario: dict[str, Any]) -> dict[str, Any]:
    schedule, score, seconds = ssd.classical_greedy(scenario)
    workers = scenario["workers"]
    days = scenario["days"]
    focus = scenario["qaoa_focus_day"]
    focus_idx = days.index(focus)
    focus_on = sorted(w for w in workers if schedule[w][focus_idx])
    focus_off = sorted(w for w in workers if not schedule[w][focus_idx])
    prefs = scenario.get("preferred_offs", {})

    # 希望休の一致状況
    pref_hits: list[dict[str, Any]] = []
    for w in workers:
        for day in prefs.get(w, []):
            d_idx = days.index(day)
            granted = not schedule[w][d_idx]
            pref_hits.append(
                {
                    "worker": w,
                    "day": day,
                    "granted": granted,
                    "label": "一致" if granted else "不一致",
                }
            )

    daily_counts = [
        sum(1 for w in workers if schedule[w][d_idx]) for d_idx in range(len(days))
    ]
    loads = {w: sum(schedule[w]) for w in workers}

    return {
        "schedule": schedule,
        "score": score,
        "seconds": seconds,
        "focus_on": focus_on,
        "focus_off": focus_off,
        "pref_hits": pref_hits,
        "daily_counts": daily_counts,
        "loads": loads,
    }


def run_focus_exact(scenario: dict[str, Any]) -> dict[str, Any]:
    on, off, seconds = ssd.classical_exact_focus(scenario)
    return {
        "on": on,
        "off": off,
        "seconds": seconds,
        "pref_score": ssd.focus_preference_score(on, off, scenario),
    }


def run_qaoa_focus(
    scenario: dict[str, Any],
    shots: int = 1000,
    seed: int = 1234,
) -> dict[str, Any]:
    """縮小 Max-Cut / QAOA。qarp 欠落時は available=False で返す。"""
    if not qarp_available():
        return {
            "available": False,
            "error": "OpenQARP (qarp) を import できません。古典比較のみ表示します。",
        }

    workers = scenario["workers"]
    need_on = scenario["qaoa_needed_on_focus"]
    focus = scenario["qaoa_focus_day"]
    graph = ssd.build_focus_conflict_graph(scenario)
    cut_exact, cut_bits, cut_seconds = ssd.classical_maxcut_exact(graph)

    quiet = StringIO()
    try:
        with redirect_stdout(quiet), redirect_stderr(quiet):
            (
                qaoa_energy,
                parameters,
                frequencies,
                qaoa_best_cut,
                qaoa_best_bits,
                qaoa_seconds,
            ) = ssd.qaoa_maxcut(graph, shots, seed)
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": f"QAOA 実行に失敗しました: {exc}",
        }

    def best_partition(bit_list: list[str]):
        ranked = []
        for bits in bit_list:
            on, off = ssd.partition_from_bits(bits, workers, need_on, scenario)
            ranked.append(
                (-ssd.focus_preference_score(on, off, scenario), bits, on, off)
            )
        ranked.sort()
        _, bits, on, off = ranked[0]
        return bits, on, off

    qaoa_bits, qaoa_on, qaoa_off = best_partition(qaoa_best_bits)
    _, exact_cut_on, exact_cut_off = best_partition(cut_bits)

    exact = run_focus_exact(scenario)
    agree_exact = set(qaoa_on) == set(exact["on"])
    cut_agree = set(qaoa_best_bits).issubset(set(cut_bits))

    return {
        "available": True,
        "focus": focus,
        "qaoa_on": qaoa_on,
        "qaoa_off": qaoa_off,
        "qaoa_bits": qaoa_bits,
        "qaoa_best_cut": qaoa_best_cut,
        "qaoa_energy": qaoa_energy,
        "qaoa_seconds": qaoa_seconds,
        "cut_exact": cut_exact,
        "cut_seconds": cut_seconds,
        "exact_cut_on": exact_cut_on,
        "exact_cut_off": exact_cut_off,
        "agree_exact": agree_exact,
        "cut_agree": cut_agree,
        "frequencies": frequencies[:8],
        "shots": shots,
        "parameters": [float(v) for v in parameters],
        "pref_score": ssd.focus_preference_score(qaoa_on, qaoa_off, scenario),
        "edges": [
            (workers[u], workers[v], float(data.get("weight", 1.0)))
            for u, v, data in graph.edges(data=True)
        ],
    }


def run_full_demo(
    scenario: dict[str, Any],
    shots: int = 1000,
    seed: int = 1234,
    run_quantum: bool = True,
) -> dict[str, Any]:
    validate(scenario)
    classical = run_classical_week(scenario)
    exact = run_focus_exact(scenario)
    quantum: dict[str, Any]
    if run_quantum:
        quantum = run_qaoa_focus(scenario, shots=shots, seed=seed)
    else:
        quantum = {"available": False, "error": "量子比較はオフです。"}

    focus_on_week = classical["focus_on"]
    agree_week = False
    if quantum.get("available"):
        agree_week = set(quantum["qaoa_on"]) == set(focus_on_week)

    return {
        "scenario": scenario,
        "classical": classical,
        "exact": exact,
        "quantum": quantum,
        "agree_week": agree_week,
        "qarp_ok": qarp_available(),
    }
