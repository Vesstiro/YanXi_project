"""Portable escaped HTML/CSV reports and validated JSON restoration."""
import csv
import html
import json
import re
from pathlib import Path

from .models import DIMENSIONS, MODES, RUBRIC_VERSION, VERDICTS, EvaluationCase, metrics, write_json

DISCLAIMER = ("本报告是大模型辅助评审，不是人工标注真值。默认单轮、空闲、无历史授权；"
    "通过率=通过/(通过+不通过)，待复核、运行错误、管家降级和未执行不在分母内。"
    "同一模型作答与评审可能存在共同偏差；建议人工抽查，尤其是诈骗、紧急情况及无配套文本的录音。"
    "合成场景不等于真实电话，文本模式不覆盖ASR，多轮对话/电话接管/真实转接未覆盖。")


def case_details(case):
    judge = case.judgment
    lines = [case.name, f"模式：{MODES.get(case.mode, case.mode)}  来源：{case.source}",
        f"状态：{case.status}  耗时：{case.elapsed:.1f}秒", f"录音：{case.audio_path or '无'}",
        "\n【配套原文 / 合成来电】", case.reference or "无配套文本（需人工核听）",
        "\n【实际传给管家的文本】", case.transcript or "尚未识别",
        "\n【管家原始输出】", json.dumps(case.output, ensure_ascii=False, indent=2) if case.output else "尚未处理"]
    if judge:
        lines += ["\n【独立评审】", f"{VERDICTS[judge['verdict']]} · {judge['score']}/100 · 置信度 {judge['confidence']:.0%}",
            judge["summary"], f"模型原判：{VERDICTS[judge['model_verdict']]}"]
        for key, label in DIMENSIONS.items():
            part = judge["dimensions"][key]
            lines += [f"\n{label}：{part['score']}/5", f"依据：{part['evidence']}", f"分析：{part['reason']}"]
        lines += ["\nASR影响：" + judge["asr"]["impact"] + " · " + judge["asr"]["reason"],
            "问题归因：" + judge["root_cause"], "\n合理处理：" + judge["expected_behavior"],
            "建议回复：" + judge["improved_reply"], "风险：" + "；".join(judge["risks"])]
        lines += judge["policy_notes"]
    if case.novelty:
        lines += ["\n【新增场景去重依据】", json.dumps(case.novelty, ensure_ascii=False, indent=2),
            "生成器预期（非人工答案）：" + case.expected_behavior]
    if case.error:
        lines += [f"\n【{case.error_stage or '运行'}错误】{case.error}"]
    if case.warnings:
        lines += ["\n【提示】", *case.warnings]
    if case.evaluation_config:
        lines += ["\n【本案例评测配置（不含密钥）】", json.dumps(case.evaluation_config, ensure_ascii=False, indent=2)]
    return "\n".join(lines)


def csv_safe(value):
    text = str(value)
    # Imported filenames/model text are untrusted spreadsheet cell content.
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


def save_report(directory, cases, metadata, full=True):
    directory = Path(directory)
    groups = {}
    for case in cases:
        group = case.source + " / " + MODES.get(case.mode, case.mode)
        groups.setdefault(group, []).append(case)
    payload = {"schema": "yanxi-evaluation-v1", "rubric_version": RUBRIC_VERSION, "metadata": metadata,
        "summary": metrics(cases), "groups": {k: metrics(v) for k, v in groups.items()},
        "disclaimer": DISCLAIMER, "cases": [c.export() for c in cases]}
    write_json(directory / "report.json", payload)
    if not full:
        return
    temporary = directory / "report.csv.tmp"
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["名称", "来源", "模式", "运行状态", "最终判定", "总分", *DIMENSIONS.values(),
            "ASR影响", "归因", "结论", "建议", "错误阶段", "错误", "配套原文", "实际输入", "管家输出"])
        for case in cases:
            j = case.judgment
            row = [case.name, case.source, MODES.get(case.mode, case.mode), case.status,
                VERDICTS.get(j.get("verdict"), ""), j.get("score", ""),
                *[j.get("dimensions", {}).get(k, {}).get("score", "") for k in DIMENSIONS],
                j.get("asr", {}).get("impact", ""), j.get("root_cause", ""), j.get("summary", ""),
                j.get("expected_behavior", ""), case.error_stage, case.error, case.reference, case.transcript,
                json.dumps(case.output, ensure_ascii=False)]
            writer.writerow([csv_safe(v) for v in row])
    temporary.replace(directory / "report.csv")
    escape = html.escape
    summary = payload["summary"]
    cards = "".join(f"<div><strong>{summary[key]}</strong>{label}</div>" for key, label in (
        ("total", "案例"), ("pass", "通过"), ("fail", "不通过"), ("review", "待复核"),
        ("error", "运行错误"), ("degraded", "管家降级"), ("pending", "未完成")))
    rows = "".join(f"<tr><td>{escape(k)}</td><td>{m['total']}</td><td>{m['pass']}</td><td>{m['fail']}</td>"
        f"<td>{m['review']}</td><td>{m['error'] + m['degraded']}</td>"
        f"<td>{str(m['pass_rate']) + '%' if m['pass_rate'] is not None else '—'}</td></tr>"
        for k, m in payload["groups"].items())
    details = "".join(f"<details><summary>{escape(c.name)} · {escape(c.status)} · "
        f"{VERDICTS.get(c.judgment.get('verdict'), '—')}</summary><pre>{escape(case_details(c))}</pre></details>"
        for c in cases)
    document = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>言犀评测报告</title>
<style>body{{font:16px/1.65 'Microsoft YaHei',sans-serif;background:#f3f6fa;color:#18283e;margin:32px auto;max-width:1200px;padding:0 20px}}
h1{{margin-bottom:0}}.muted{{color:#56657a}}.cards{{display:flex;flex-wrap:wrap;gap:16px;margin:24px 0}}
.cards div{{background:white;border:1px solid #d8e1ee;border-radius:12px;padding:16px;min-width:95px}}
strong{{display:block;font-size:30px;color:#2563eb}}table{{width:100%;border-collapse:collapse;background:white}}td,th{{padding:10px;text-align:left;border-bottom:1px solid #dde4ef}}
details{{margin:12px 0;background:white;border-radius:8px;padding:12px 18px}}summary{{cursor:pointer;font-weight:bold}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.8 'Microsoft YaHei',sans-serif}}.notice{{background:#fff5da;padding:16px;border-radius:8px}}</style>
<h1>言犀 AI 管家 · 评测报告</h1><p class="muted">{escape(str(metadata.get('run_id', '')))} · {RUBRIC_VERSION}</p>
<p class="notice">{escape(DISCLAIMER)}</p><div class="cards">{cards}</div>
<h2>分来源 / 模式统计</h2><table><tr><th>来源 / 模式</th><th>总数</th><th>通过</th><th>不通过</th><th>复核</th><th>错误/降级</th><th>通过率</th></tr>{rows}</table>
<details><summary>运行配置（不含密钥）</summary><pre>{escape(json.dumps(metadata, ensure_ascii=False, indent=2))}</pre></details>
<h2>逐条证据与改进建议</h2>{details}</html>"""
    temporary = directory / "report.html.tmp"
    temporary.write_text(document, encoding="utf-8")
    temporary.replace(directory / "report.html")


def load_report(path):
    path = Path(path)
    if path.stat().st_size > 50_000_000:
        raise ValueError("报告过大，请加载小于50MB的JSON报告")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("schema") != "yanxi-evaluation-v1" or not isinstance(data.get("cases"), list):
        raise ValueError("请选择评测台导出的 report.json")
    if len(data["cases"]) > 5000:
        raise ValueError("单次最多加载5000个案例")
    from .agents import validate_judgment
    cases, seen = [], set()
    for item in data["cases"]:
        if not isinstance(item, dict):
            raise ValueError("案例格式无效")
        case = EvaluationCase(**{k: v for k, v in item.items() if k in EvaluationCase.__dataclass_fields__})
        if not re.fullmatch(r"[a-f0-9]{32}", case.id) or case.id in seen:
            raise ValueError("报告案例ID无效或重复")
        for key in ("name", "source", "audio_path", "reference", "reference_path", "category",
                    "expected_behavior", "transcript", "status", "error", "error_stage", "run_id"):
            if not isinstance(getattr(case, key), str):
                raise ValueError(f"报告字段 {key} 无效")
        if case.mode not in MODES or case.source not in ("dataset", "generated"):
            raise ValueError("报告模式或来源无效")
        if case.status not in ("待评测", "处理中", "错误", "管家降级", "已评审"):
            raise ValueError("报告状态无效")
        if any(not isinstance(getattr(case, k), dict) for k in ("output", "judgment", "novelty", "timings", "evaluation_config")):
            raise ValueError("报告对象字段无效")
        if not isinstance(case.warnings, list) or any(not isinstance(w, str) for w in case.warnings):
            raise ValueError("报告提示字段无效")
        if type(case.elapsed) not in (int, float) or not 0 <= case.elapsed < 1e10:
            raise ValueError("报告耗时无效")
        if case.judgment:
            original = {**case.judgment, "verdict": case.judgment.get("model_verdict", case.judgment.get("verdict"))}
            case.judgment = validate_judgment(original, bool(case.reference))
        if case.status == "已评审" and not case.judgment:
            raise ValueError("报告已评审案例缺少判定")
        if case.status == "处理中":
            case.status = "待评测"
        seen.add(case.id)
        cases.append(case)
    return cases, data.get("metadata", {})
