"""「⚛️ 量子で比べる」— 小さな部分問題での古典 vs 量子方式（OpenQARP QAOA シミュレーション）比較。

- シフト全体は古典ソルバ（slot_optimizer）が作る。ここは比較実験のみ。
- 部分問題: 1日 × 1枠の「誰を入れるか」（候補 ≤ 8名 = 8 qubit）。
- 同じ部分問題を 古典（貪欲法）・参考の全探索（厳密解）・量子方式（QUBO→Ising→QAOA を
  OpenQARP の状態ベクトルシミュレータで実行）で解き、同じ評価関数で採点する。
- 数値はすべて実測。量子方式が負け／引き分けでもそのまま表示する。実機の量子コンピュータではない。
"""

from __future__ import annotations

import itertools
import os
import time
from collections import Counter
from typing import Any

import shift_rules as sr
import slot_optimizer as so

MAX_QUBITS = 8
N_LAYERS = 2
SHOTS = 1000
SEED = 1234
PENALTY = 4.0
ALPHA_COST = 1.0
BETA_PREF = 0.6
BACKEND_JA = "OpenQARP 状態ベクトルシミュレータ（CPU・実機ではありません）"


# ---- subproblem -------------------------------------------------------------

def choose_subproblem(problem: dict[str, Any], plan: dict[str, Any] | None = None, *, max_qubits: int = MAX_QUBITS) -> dict[str, Any] | None:
    assign = so.assign_from_plan(plan) if plan else {}
    staff = {s["wid"]: s for s in problem["staff"]}
    best = None
    for d in problem["dates"]:
        for slot in problem["slots"]:
            cands = [w for w in staff if problem["elig"].get((d, slot["name"], w)) != "no"]
            if len(cands) < 2 or int(slot["required"]) < 1:
                continue
            key = (min(len(cands), max_qubits), len(assign.get((d, slot["name"])) or []) < int(slot["required"]))
            if best is None or key > best[0]:
                best = (key, d, slot, cands)
    if best is None:
        return None
    _, d, slot, cands = best
    cands.sort(key=lambda w: (0 if problem["elig"][(d, slot["name"], w)] == "ok" else 1, so.shift_cost(staff[w], slot), w))
    cands = cands[:max_qubits]
    rules = problem["rules"]
    R = int(slot["required"])
    items = [{
        "wid": w,
        "name": staff[w]["name"],
        "cost": int(round(so.shift_cost(staff[w], slot))),
        "pref": 1 if problem["elig"][(d, slot["name"], w)] == "ok" else 0,
        "level": staff[w].get("level"),
    } for w in cands]
    idx = {c["wid"]: i for i, c in enumerate(items)}
    ng = [(idx[a], idx[b]) for a, b in (rules.get("ng_pairs") or []) if a in idx and b in idx]
    mix = bool(rules.get("require_mix"))
    cond_in = [f"必要人数 {R}人（ちょうど{R}人）", "人件費（シフト1回分の予定人件費）", "シフト希望（入れます＝加点）"]
    if ng:
        cond_in.append("同じ時間NGの組み合わせ " + "、".join(f"{items[i]['name']}×{items[j]['name']}" for i, j in ng))
    if mix:
        cond_in.append("新人とベテランを1人ずつ以上" + ("" if R == 2 else "（QUBO には入れず採点のみ）"))
    cond_out = ["連勤上限・週の上限時間・1日の枠数（他の日と関係するため）", "人件費上限（週・月の合計条件のため）"]
    if len([s for s in staff if problem["elig"].get((d, slot["name"], s)) != "no"]) > max_qubits:
        cond_out.append(f"候補 {max_qubits} 名を超えるスタッフ（希望あり→人件費の安い順で {max_qubits} 名に絞り込み）")
    return {
        "date": d, "date_label": sr.date_label(d), "slot": slot["name"], "slot_time": f"{slot['start']}〜{slot['end']}",
        "required": R, "items": items, "ng": ng, "mix": mix, "mix_in_qubo": mix and R == 2,
        "conditions_in": cond_in, "conditions_out": cond_out,
    }


def sub_evaluate(sub: dict[str, Any], x: tuple[int, ...]) -> dict[str, Any]:
    items = sub["items"]
    on = [i for i, b in enumerate(x) if b]
    n = len(on)
    v = {"shortage": max(0, sub["required"] - n), "excess": max(0, n - sub["required"]),
         "ng_pair": sum(1 for i, j in sub["ng"] if x[i] and x[j]), "mix": 0}
    if sub["mix"] and n:
        lv = {items[i]["level"] for i in on}
        if "新人" not in lv or "ベテラン" not in lv:
            v["mix"] = 1
    hits = sum(items[i]["pref"] for i in on)
    any_pref = any(c["pref"] for c in items)
    return {
        "x": list(x),
        "members": [items[i]["name"] for i in on],
        "labor_cost": sum(items[i]["cost"] for i in on),
        "pref_hits": hits,
        "assigned": n,
        "pref_rate": (hits / n) if n and any_pref else None,
        "violations": v,
        "violation_total": sum(v.values()),
        "energy": qubo_energy(sub, x),
    }


def qubo_terms(sub: dict[str, Any]) -> tuple[list[float], dict[tuple[int, int], float]]:
    items = sub["items"]
    n = len(items)
    R = sub["required"]
    mean_cost = (sum(c["cost"] for c in items) / n) or 1.0
    a = [ALPHA_COST * c["cost"] / mean_cost - BETA_PREF * c["pref"] for c in items]
    b: dict[tuple[int, int], float] = {}

    def add_b(i: int, j: int, w: float) -> None:
        key = (min(i, j), max(i, j))
        b[key] = b.get(key, 0.0) + w

    for i in range(n):
        a[i] += PENALTY * (1 - 2 * R)
        for j in range(i + 1, n):
            add_b(i, j, 2 * PENALTY)
    for i, j in sub["ng"]:
        add_b(i, j, PENALTY)
    if sub.get("mix_in_qubo"):
        for lvl in ("新人", "ベテラン"):
            g = [i for i, c in enumerate(items) if c["level"] == lvl]
            for i in g:
                a[i] += -PENALTY
            for i, j in itertools.combinations(g, 2):
                add_b(i, j, 2 * PENALTY)
    return a, b


def qubo_energy(sub: dict[str, Any], x: tuple[int, ...]) -> float:
    a, b = qubo_terms(sub)
    e = sum(a[i] for i, v in enumerate(x) if v)
    e += sum(w for (i, j), w in b.items() if x[i] and x[j])
    return round(e, 6)


def _rank(ev: dict[str, Any]) -> tuple:
    return (ev["violation_total"], ev["energy"])


def solve_greedy(sub: dict[str, Any]) -> dict[str, Any]:
    """本体と同じ考え方の貪欲法（人件費が安く・希望ありを優先、NG組は避ける）。"""
    t0 = time.perf_counter()
    items = sub["items"]
    order = sorted(range(len(items)), key=lambda i: (ALPHA_COST * items[i]["cost"] - 1000 * BETA_PREF * items[i]["pref"], i))
    chosen: list[int] = []
    if sub["mix"] and sub["required"] >= 2:
        for lvl in ("ベテラン", "新人"):
            for i in order:
                if items[i]["level"] == lvl and i not in chosen and not any((min(i, c), max(i, c)) in set(sub["ng"]) for c in chosen):
                    chosen.append(i)
                    break
    for i in order:
        if len(chosen) >= sub["required"]:
            break
        if i in chosen or any((min(i, c), max(i, c)) in set(sub["ng"]) for c in chosen):
            continue
        chosen.append(i)
    x = tuple(1 if i in chosen else 0 for i in range(len(items)))
    sec = time.perf_counter() - t0
    return {**sub_evaluate(sub, x), "seconds": sec, "algorithm": "greedy heuristic（貪欲法）"}


def solve_exact(sub: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    n = len(sub["items"])
    best = None
    for bits in itertools.product((0, 1), repeat=n):
        ev = sub_evaluate(sub, bits)
        if best is None or _rank(ev) < _rank(best):
            best = ev
    sec = time.perf_counter() - t0
    return {**best, "seconds": sec, "algorithm": f"参考: 全探索（2^{n}={2 ** n}通りの厳密解）"}


def _ising(sub: dict[str, Any]):
    from qarp.operators import QubitOperator

    a, b = qubo_terms(sub)
    n = len(a)
    h = [-a[i] / 2.0 for i in range(n)]
    J: dict[tuple[int, int], float] = {}
    for (i, j), w in b.items():
        h[i] -= w / 4.0
        h[j] -= w / 4.0
        J[(i, j)] = w / 4.0
    op = None
    for (i, j), w in J.items():
        if abs(w) > 1e-12:
            t = QubitOperator(f"Z{i} Z{j}", float(w))
            op = t if op is None else op + t
    for i, w in enumerate(h):
        if abs(w) > 1e-12:
            t = QubitOperator(f"Z{i}", float(w))
            op = t if op is None else op + t
    return op, len(J), sum(1 for w in h if abs(w) > 1e-12)


def solve_quantum(sub: dict[str, Any], *, shots: int = SHOTS, seed: int = SEED, layers: int = N_LAYERS) -> dict[str, Any]:
    os.environ.setdefault("QARP_SKIP_ABI_CHECK", "1")
    t0 = time.perf_counter()
    try:
        from qarp import config
        from qarp.algorithms import QAOA, Sampler
        from qarp.engines import QarpEngine
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": f"OpenQARP を読み込めません: {exc}"}
    import contextlib
    import io

    n = len(sub["items"])
    config.seed = seed
    op, n_zz, n_z = _ising(sub)
    with contextlib.redirect_stdout(io.StringIO()):
        qaoa = QAOA(problem=op, n_layers=layers, use_rzz=True, verbose=False,
                    initial_parameters=[0.1] * (2 * layers)).build()
        energy, params = qaoa.run()
    t_opt = time.perf_counter() - t0
    circuit = qaoa.get_final_state_block()
    circuit.measure([(q, q) for q in range(circuit.n_qubits)])
    engine = QarpEngine(seed=seed)
    engine.build([Sampler(ket=circuit, n_shots=shots)])
    probs = engine.run()[0]
    freq = Counter({tuple(int(b) for b in bits): float(p) for bits, p in probs.items()})
    sec = time.perf_counter() - t0
    sampled = [(bits[:n], p) for bits, p in freq.items() if p > 0]
    best_bits, best_p = min(sampled, key=lambda bp: (_rank(sub_evaluate(sub, bp[0])), -bp[1]))
    # 量子方式の「答え」は最頻出の測定結果（ショット中で一番多く出た割当）。
    # 全ショットから良いものを後で選ぶと古典の全探索に近くなるため、それは参考値として別表示。
    top_bits, top_p = max(sampled, key=lambda bp: (bp[1], [-b for b in bp[0]]))
    ev = sub_evaluate(sub, tuple(top_bits))
    return {
        **ev,
        "available": True,
        "seconds": sec,
        "seconds_optimize": t_opt,
        "algorithm": f"量子方式（変分・p={layers}・{circuit.n_qubits}qubit・シミュレータ）",
        "algorithm_detail": f"QAOA p={layers}（OpenQARP qarp, 最適化: SciPy CG, {shots}ショット測定）",
        "qubits": int(circuit.n_qubits),
        "layers": layers,
        "shots": shots,
        "expectation_energy": float(energy),
        "parameters": [round(float(p), 4) for p in params],
        "top_prob": round(top_p, 4),
        "best_sample": {"x": list(best_bits), "prob": round(best_p, 4), **{k: v for k, v in sub_evaluate(sub, tuple(best_bits)).items() if k in ("members", "labor_cost", "violation_total")}},
        "ising_terms": {"zz": n_zz, "z": n_z},
        "backend": BACKEND_JA,
    }


def verdict(classical: dict[str, Any], quantum: dict[str, Any]) -> str:
    if not quantum.get("available"):
        return "量子方式は実行できませんでした（古典の結果のみ）。"
    c, q = _rank(classical), _rank(quantum)
    if abs(c[1] - q[1]) < 1e-9 and c[0] == q[0]:
        return "引き分け（同じ品質の解）"
    return "量子方式の勝ち（この小問題では）" if q < c else "古典の勝ち（この小問題では）"


def run_compare(store: dict[str, Any], plan_key: str | None, pending: dict[str, Any] | None) -> dict[str, Any]:
    problem = so.build_problem(store, (pending or {}).get("dates"))
    plan = None
    for p in (pending or {}).get("plans") or []:
        if p["key"] == plan_key:
            plan = p
    sub = choose_subproblem(problem, plan)
    if sub is None:
        return {"ok": False, "error": "比較できる小問題がありません（候補スタッフが2名以上いる枠が必要です）。"}
    classical = solve_greedy(sub)
    exact = solve_exact(sub)
    quantum = solve_quantum(sub)
    return {
        "ok": True,
        "plan_key": plan_key,
        "plan_label": (plan or {}).get("label"),
        "sub": {k: v for k, v in sub.items() if k != "items"} | {
            "candidates": [{"name": c["name"], "cost": c["cost"], "pref": c["pref"], "level": c["level"]} for c in sub["items"]]
        },
        "classical": classical,
        "exact": exact,
        "quantum": quantum,
        "verdict": verdict(classical, quantum),
        "measured_at": sr.now_jst().isoformat(timespec="seconds"),
    }


# ---- presentation -----------------------------------------------------------

def _ms(sec: float | None) -> str:
    if sec is None:
        return "—"
    return f"{sec * 1000:.2f}ms" if sec < 1 else f"{sec:.2f}s"


def _rate(ev: dict[str, Any]) -> str:
    return "—" if ev.get("pref_rate") is None else f"{ev['pref_rate'] * 100:.0f}%"


def panel_rows(res: dict[str, Any]) -> list[tuple[str, str, str]]:
    c, q = res["classical"], res["quantum"]
    qa = q.get("available")
    return [
        ("人件費", f"{c['labor_cost']:,}円", f"{q['labor_cost']:,}円" if qa else "—"),
        ("希望充足率", _rate(c), _rate(q) if qa else "—"),
        ("制約違反数", f"{c['violation_total']}件", f"{q['violation_total']}件" if qa else "—"),
        ("評価スコア(小さいほど良)", f"{c['energy']:.3f}", f"{q['energy']:.3f}" if qa else "—"),
        ("計算時間(実測)", _ms(c["seconds"]), _ms(q.get("seconds")) if qa else "—"),
        ("選ばれた人", "・".join(c["members"]) or "なし", "・".join(q.get("members") or []) or "なし" if qa else "—"),
        ("使用アルゴリズム", "古典: greedy heuristic", f"量子方式: 変分 p={q['layers']}・{q['qubits']}qubit・シミュレータ" if qa else "—"),
    ]


def build_panel_flex(res: dict[str, Any]) -> dict[str, Any]:
    sub = res["sub"]

    def cell(t: str, *, bold: bool = False, color: str = "#0f172a", flex: int = 4, align: str = "start") -> dict[str, Any]:
        return {"type": "text", "text": t, "size": "xs", "wrap": True, "weight": "bold" if bold else "regular", "color": color, "flex": flex, "align": align}

    rows = [{"type": "box", "layout": "horizontal", "contents": [
        cell("指標", bold=True, color="#64748b", flex=3), cell("古典", bold=True, color="#1e40af"), cell("量子方式", bold=True, color="#7c3aed")]}]
    for k, a, b in panel_rows(res):
        rows.append({"type": "box", "layout": "horizontal", "margin": "sm", "contents": [cell(k, color="#64748b", flex=3), cell(a), cell(b)]})
    ex = res["exact"]
    rows.append({"type": "separator", "margin": "md"})
    rows.append({"type": "text", "text": f"参考 全探索の最適（評価スコア {ex['energy']:.3f}）: {'・'.join(ex['members'])}／{ex['labor_cost']:,}円・希望{_rate(ex)}・違反{ex['violation_total']}件（{_ms(ex['seconds'])}）", "size": "xxs", "color": "#64748b", "wrap": True, "margin": "md"})
    rows.append({"type": "text", "text": "判定: " + res["verdict"], "size": "sm", "weight": "bold", "wrap": True, "margin": "md"})
    return {
        "type": "flex",
        "altText": f"量子で比べる｜{sub['date_label']} {sub['slot']}｜{res['verdict']}",
        "contents": {
            "type": "bubble", "size": "giga",
            "header": {"type": "box", "layout": "vertical", "backgroundColor": "#1e1b4b", "paddingAll": "14px", "contents": [
                {"type": "text", "text": "⚛️ 量子で比べる", "weight": "bold", "size": "lg", "color": "#ffffff"},
                {"type": "text", "text": f"小問題: {sub['date_label']} {sub['slot']}（{sub['slot_time']}）必要{sub['required']}人・候補{len(sub['candidates'])}名",
                 "size": "xs", "color": "#c7d2fe", "wrap": True, "margin": "sm"},
            ]},
            "body": {"type": "box", "layout": "vertical", "paddingAll": "14px", "contents": rows},
            "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px", "contents": [
                {"type": "button", "style": "secondary", "height": "sm",
                 "action": {"type": "postback", "label": "詳細", "data": "v=1&action=qcompare_detail", "displayText": "量子比較の詳細"}},
                {"type": "text", "text": "シフト全体は古典ソルバで作成しています。量子方式はシミュレータ上の比較実験です（実機ではありません）。",
                 "size": "xxs", "color": "#94a3b8", "wrap": True},
            ]},
        },
    }


def detail_text(res: dict[str, Any]) -> str:
    sub, c, q, ex = res["sub"], res["classical"], res["quantum"], res["exact"]
    lines = [
        "【量子で比べる — 詳細】",
        f"対象: {sub['date_label']} {sub['slot']}（{sub['slot_time']}）の「誰を入れるか」だけ（1日・1枠の小問題）",
        f"比較元の案: {res.get('plan_label') or '—'}",
        "候補: " + "、".join(f"{x['name']}（{x['cost']:,}円{'・希望あり' if x['pref'] else ''}{'・' + x['level'] if x['level'] else ''}）" for x in sub["candidates"]),
        "",
        "■ 量子計算に入れた条件",
        *("・" + t for t in sub["conditions_in"]),
        "■ 入れなかった条件",
        *("・" + t for t in sub["conditions_out"]),
        "",
        "■ 量子方式",
    ]
    if q.get("available"):
        lines += [
            f"方式: {q['algorithm_detail']}",
            f"符号化: QUBO → Ising（Z項 {q['ising_terms']['z']}・ZZ項 {q['ising_terms']['zz']}）, {q['qubits']} qubit",
            f"実行環境: {q['backend']}",
            f"期待エネルギー: {q['expectation_energy']:.4f} ／ 最適化パラメータ: {q['parameters']}",
            f"採用した答え: {q['shots']}ショット中で最も多く出た割当（出現確率 {q['top_prob'] * 100:.1f}%）→ {'・'.join(q['members']) or 'なし'}",
            f"参考: ショット中の最良の割当 {'・'.join(q['best_sample']['members']) or 'なし'}（{q['best_sample']['labor_cost']:,}円・違反{q['best_sample']['violation_total']}件・出現確率 {q['best_sample']['prob'] * 100:.1f}%）",
            f"計算時間: {_ms(q['seconds'])}（うち変分最適化 {_ms(q['seconds_optimize'])}）",
        ]
    else:
        lines.append(f"実行できませんでした: {q.get('error')}")
    lines += [
        "",
        f"■ 古典: {c['algorithm']} … {_ms(c['seconds'])}",
        f"■ {ex['algorithm']} … {_ms(ex['seconds'])}",
        "",
        "判定ルール: 制約違反数が少ない方 → 同数なら評価スコア（人件費を平均で割った値＋ペナルティ−希望加点0.6）が小さい方。",
        f"判定: {res['verdict']}",
        "※ シミュレータ（古典コンピュータ上の量子回路シミュレーション）であり、実機の量子コンピュータではありません。",
        "※ シフト全体の作成は古典ソルバ（貪欲法＋局所探索）です。",
    ]
    return "\n".join(lines)
