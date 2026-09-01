"""Cases, dataset discovery, deterministic checks and resumable snapshots."""
import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from src.desktop.audio import AUDIO_EXTENSIONS

RUBRIC_VERSION = "yanxi-single-turn-v1"
MODES = {"audio": "录音 → ASR", "reference": "配套文本（跳过ASR）",
         "text": "合成文本", "tts": "合成语音 → ASR"}
VERDICTS = {"pass": "通过", "fail": "不通过", "review": "待复核"}
DIMENSIONS = {"scenario": "场景匹配", "correctness": "处理正确性",
              "appropriateness": "回复恰当性", "safety": "安全与权限"}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


@dataclass
class EvaluationCase:
    name: str
    source: str = "dataset"
    mode: str = "audio"
    audio_path: str = ""
    reference: str = ""
    reference_path: str = ""
    category: str = "未分组"
    expected_behavior: str = ""
    novelty: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "待评测"
    transcript: str = ""
    output: dict = field(default_factory=dict)
    judgment: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    error: str = ""
    error_stage: str = ""
    elapsed: float = 0.0
    timings: dict = field(default_factory=dict)
    run_id: str = ""
    evaluation_config: dict = field(default_factory=dict)

    def export(self):
        return asdict(self)


def read_reference(path):
    data = Path(path).read_bytes()
    encodings = ("utf-16",) if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            return data.decode(encoding).strip()
        except UnicodeError:
            pass
    raise ValueError(f"无法解码配套文本：{Path(path).name}")


def discover_cases(paths, mode="audio"):
    if mode not in ("audio", "reference"):
        raise ValueError("请选择录音或配套文本模式")
    files, seen = [], set()
    for raw in paths:
        path = Path(raw)
        candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
        for candidate in candidates:
            identity = str(candidate.resolve()).casefold()
            if candidate.is_file() and candidate.suffix.lower() in AUDIO_EXTENSIONS and identity not in seen:
                files.append(candidate.resolve())
                seen.add(identity)
    cases = []
    for path in files:
        reference = ""
        reference_path = ""
        warning = []
        for sidecar in (path.with_suffix(".txt"), path.parent / "文本" / (path.stem + ".txt")):
            if sidecar.is_file():
                reference_path = str(sidecar)
                try:
                    reference = read_reference(sidecar)
                except ValueError as exc:
                    warning.append(str(exc))
                break
        # Filenames are only organizational metadata, not ground-truth classifications.
        category = path.stem.split("_")[0] if "_" in path.stem else "未分组"
        case = EvaluationCase(name=path.stem, audio_path=str(path), reference=reference,
            reference_path=reference_path, category=category, mode=mode, warnings=warning)
        if not reference:
            case.warnings.append("无有效配套文本：评审无法核实音频原意，结论需人工核听。")
        cases.append(case)
    return cases


def normalize(text):
    return "".join(re.findall(r"[\w\u4e00-\u9fff]", text.lower()))


def similarity(a, b):
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def corpus_entries(cases):
    entries, seen = [], set()
    for case in cases:
        identity = (case.name, case.reference)
        if identity in seen:
            continue
        seen.add(identity)
        entries.append({"name": case.name, "text": case.reference or "[仅有文件名，没有转写文本]"})
    return entries


def corpus_fingerprint(entries):
    return hashlib.sha256(json.dumps(entries, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def metrics(cases):
    counts = {"total": len(cases), "pass": 0, "fail": 0, "review": 0, "error": 0,
              "pending": 0, "degraded": 0}
    for case in cases:
        if case.status in ("待评测", "处理中"):
            counts["pending"] += 1
        elif case.status == "错误":
            counts["error"] += 1
        elif case.status == "管家降级":
            counts["degraded"] += 1
        elif case.judgment:
            counts[case.judgment["verdict"]] += 1
    counts["decided"] = counts["pass"] + counts["fail"]
    counts["pass_rate"] = round(counts["pass"] / counts["decided"] * 100, 1) if counts["decided"] else None
    return counts
