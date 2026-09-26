"""Conservative answer verification under an explicit per-task contract.

Text is scored only when a target-specific assertion can be isolated and no
contradictory assertion survives. Ambiguous prose returns None for review.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

_PARCEL_ID = re.compile(r"(?<![A-Za-z0-9])P\d{3}(?![0-9])")
_LAYER_TERMS = ("耕地", "建筑", "宗地", "设施点", "道路", "farmland", "buildings", "parcels")

#: Id-like tokens to neutralise before number parsing: ``P006``, ``S01``, ``R12``.
#: Requires the letter and digit run to be **joined with no separator**, so
#: ``EPSG:4547`` is NOT stripped -- the prefix there is a label and 4547 is the
#: answer (caught by the third verification pass).
_ID_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,6}\d{1,6}(?![0-9])")

_NUMBER = re.compile(r"(?<![A-Za-z0-9])-?\d[\d,]*(?:\.\d+)?(?![A-Za-z0-9])")

#: Sentence split: CJK and ASCII terminators, plus newlines.
_SENTENCE = re.compile(r"[。；;！!？?\n]+|\.(?=\s|$)")

#: Clause split inside a sentence. Needed because Chinese prose often joins an
#: honest denial and a fabricated claim with a comma: "未占压，但占压 100 平方米"
#: is one sentence with two contradictory clauses (third review pass).
_CLAUSE = re.compile(r"[，、]+|,(?!\d)|(?<!\d),|但是|不过|然而|却|但")

_RELATION = re.compile(r"包含于|占压|相交|重叠|压占|包含")
_NEGATION_AT_END = re.compile(r"(?:并非|并不|并未|没有|不|未|无)\s*$")
_DOUBLE_NEGATION = re.compile(r"(?:并非|不是|不能说)\s*不\s*$")

#: Phrases asserting the forbidden action was carried out.
_COMPLETION = re.compile(
    r"(?:已|已经|成功|顺利|现已)\s*"
    r"(?:完成|生成|创建|删除|导出|执行|写入|获得|得到|拿到|做好|查到|查得|获取到|弄好)"
    r"|(?:完成|生成|创建|删除|导出|执行|写入|获得|得到|拿到|做好|查到|获取到|弄好|返回)了"
)
_COMPLETION_NEGATION = re.compile(r"(?:并未|并没有|没有|未|无法|不能|不曾)\s*$")
_NUMERIC_QUALIFIER = re.compile(r"(?:不是|并非|没有|不足|不到|超过|至少|至多|大约|约)\s*$")
_OUTCOME_TERMS = {
    "missing_entity": ("占压", "相交", "重叠"),
    "unknown_tool": ("缓冲区",),
}
_OUTCOME_DENIAL = re.compile(
    r"(?:无法|不能|未能|未|不提供|不支持|不存在|没有(?!问题)|不可以)"
    r".{0,16}(?:占压|相交|重叠|缓冲区)"
)

_DECORATION = re.compile(r"[*_`~]+")


#: Legacy ``answer_type`` vocabulary -> contract ``kind`` vocabulary.
_TYPE_TO_KIND = {
    "boolean": "bool",
    "numeric": "float",
    "count": "int",
    "list": "set",
    "refusal": "refusal",
}


def _as_text(answer: Any) -> str:
    return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text) if s.strip()]


def _clauses(text: str) -> list[str]:
    """Sentence pieces split further on commas.

    Chinese prose routinely joins a denial and a contradictory claim with a
    comma: "未占压，但占压 100 平方米". Sentence-level analysis would let the
    first half decide for the whole (third review round).
    """
    pieces = [
        piece.strip()
        for sentence in _sentences(text)
        for piece in _CLAUSE.split(sentence)
        if piece.strip()
    ]
    return pieces or _sentences(text) or [text]


def _has_completion_claim(clause: str) -> bool:
    """Reject a real success claim, even after an earlier refusal in one clause.

    Round 4 probe: "无法执行但已生成缓冲区" used to pass because a loose six-
    character lookback let "无法" negate the later, separate "已生成".
    """
    for match in _COMPLETION.finditer(clause):
        if not _COMPLETION_NEGATION.search(clause[:match.start()]):
            return True
    return False


def _relation_verdicts(text: str) -> set[bool] | None:
    """Collect every relation assertion before deciding a boolean verdict.

    Round 4 probe: "不相交但又相交" contains two assertions in ONE clause.
    A double negation such as "并非不相交" is left for review.
    """
    verdicts: set[bool] = set()
    for clause in _clauses(text):
        for match in _RELATION.finditer(clause):
            prefix = clause[:match.start()]
            if _DOUBLE_NEGATION.search(prefix):
                return None
            if _NEGATION_AT_END.search(prefix):
                verdicts.add(False)
            elif re.search(r"(?:没有|未|不|无)", prefix[-12:]):
                # "没有与耕地相交" has a displaced negation. It is unsafe to
                # treat it as affirmative, while "没有问题，P004相交" is split.
                return None
            else:
                verdicts.add(True)
    return verdicts


def _hint_is_standalone(clause: str, start: int, end: int, hint: str) -> bool:
    """Do not mistake 米 in 平方米 or m in m2 for a distance unit."""
    if hint == "米" and start > 0 and clause[start - 1] == "方":
        return False
    if hint.lower() == "m" and clause[end:end + 2] in ("²", "^2", "2"):
        return False
    if hint.lower() == "meters" and clause[max(0, start - 7):start].lower().endswith("square "):
        return False
    if hint.isascii():
        if start > 0 and clause[start - 1].isascii() and clause[start - 1].isalnum():
            return False
        if end < len(clause) and clause[end].isascii() and clause[end].isalnum():
            return False
    return True


def _context_numbers(text: str, hints: tuple[str, ...]) -> tuple[list[float], bool]:
    """Bind a number to a declared hint inside one clause and one short phrase.

    Round 4 probe: in "5个要素，另有6个字段", the 6 is in another clause and
    must not be collected for 要素. Joined IDs such as P006 are blanked first.
    """
    if not hints:
        return [], False
    lowered_hints = {hint.casefold() for hint in hints}
    values: list[float] = []
    qualified = False
    for clause in _clauses(text):
        def strip_id(match: re.Match[str]) -> str:
            return match.group() if match.group().casefold() in lowered_hints else " "

        cleaned = _DECORATION.sub("", _ID_TOKEN.sub(strip_id, clause))
        for hint in hints:
            flags = re.IGNORECASE if hint.isascii() else 0
            for found in re.finditer(re.escape(hint), cleaned, flags):
                if not _hint_is_standalone(cleaned, found.start(), found.end(), hint):
                    continue
                left = cleaned[max(0, found.start() - 16):found.start()]
                preceding = list(_NUMBER.finditer(left))
                if preceding:
                    number = preceding[-1]
                    gap = left[number.end():]
                    if re.fullmatch(r"\s*(?:个|项|条|处)?\s*", gap):
                        # "不是6个要素" and "至少6个要素" do not assert an
                        # exact value, even when 6 equals the gold value.
                        if _NUMERIC_QUALIFIER.search(left[:number.start()]):
                            qualified = True
                        else:
                            values.append(float(number.group().replace(",", "")))
                right = cleaned[found.end():found.end() + 20]
                label = re.match(
                    r"\s*(?:(?:的\s*)?(?:数量|总数|个数|数目|数)|有|为|是)?"
                    r"\s*[:：=]?\s*",
                    right,
                )
                if label:
                    number = _NUMBER.match(right, label.end())
                    if number:
                        values.append(float(number.group().replace(",", "")))
    return values, qualified


def _structured_mismatch(
    actual: Any, expected: Any, tolerance: float = 0.0
) -> bool | None:
    """Compare all declared fields; a missing field is not proof of success.

    Round 4 T25 probe: a right ``intersects`` must not hide a wrong or omitted
    ``relation``. Bool values require their actual type, not truthiness.
    """
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return None
    for key, want in expected.items():
        if key not in actual:
            return None
        got = actual[key]
        if isinstance(want, bool):
            if not isinstance(got, bool) or got is not want:
                return False
        elif isinstance(want, (int, float)):
            try:
                got_number = float(got)
                want_number = float(want)
                if (
                    isinstance(got, bool)
                    or not math.isfinite(got_number)
                    or not math.isfinite(want_number)
                    or abs(got_number - want_number) > tolerance
                ):
                    return False
            except (TypeError, ValueError):
                return False
        elif isinstance(want, list):
            if not isinstance(got, list) or {str(x) for x in got} != {str(x) for x in want}:
                return False
        elif got != want:
            return False
    return True if expected else None


def _expected_field(expected: Any, field: str | None) -> Any:
    if isinstance(expected, dict) and field:
        return expected.get(field)
    return expected


def _subject_conflict(text: str, contract: dict[str, Any]) -> bool:
    """Reject an explicitly different parcel or layer, without requiring repetition.

    Round 5 T07 probe: "建筑图层共有6个要素" cannot answer a question about
    耕地 merely because both counts happen to be six. A bare "共有6个要素"
    remains interpretable in the question's context.
    """
    subjects = tuple(str(value) for value in contract.get("subjects") or ())
    if not subjects:
        return False
    allowed_ids = {s.upper() for s in subjects if _PARCEL_ID.fullmatch(s.upper())}
    mentioned_ids = set(_PARCEL_ID.findall(text.upper()))
    if allowed_ids and mentioned_ids and not mentioned_ids <= allowed_ids:
        return True
    folded = text.casefold()
    allowed_layers = {s.casefold() for s in subjects if s.casefold() in _LAYER_TERMS}
    mentioned_layers = {term for term in _LAYER_TERMS if term in folded}
    return bool(allowed_layers and mentioned_layers and not mentioned_layers <= allowed_layers)


def _verify_scalar(
    actual: Any, expected: Any, contract: dict[str, Any]
) -> bool | None:
    """int / float: compare against the contract's field, tolerance-aware."""
    field = contract.get("field")
    target = _expected_field(expected, field)
    if target is None:
        return None
    try:
        if isinstance(target, bool):
            return None
        target_number = float(target)
        if not math.isfinite(target_number):
            return None
    except (TypeError, ValueError):
        return None

    if _subject_conflict(_as_text(actual), contract):
        return None
    # Structured answer: compare EVERY declared field, not just the numeric one.
    tol = float(contract.get("tolerance") or 0)
    if isinstance(actual, dict) and isinstance(expected, dict):
        return _structured_mismatch(actual, expected, tolerance=tol)
    if isinstance(actual, (int, float)) and not isinstance(actual, bool):
        return math.isfinite(float(actual)) and abs(float(actual) - target_number) <= tol

    hints = tuple(contract.get("hints") or ())
    candidates, qualified = _context_numbers(_as_text(actual), hints)

    if qualified:
        return None
    if not candidates:
        return None
    if contract["kind"] == "int":
        tol = max(tol, 1e-9)
    # Round 4: first exclude two distinct answers to the SAME field, before
    # looking for one that happens to equal the gold value.
    if any(abs(candidate - candidates[0]) > tol for candidate in candidates[1:]):
        return None
    return abs(candidates[0] - target_number) <= tol


def _verify_bool(actual: Any, expected: Any, contract: dict[str, Any]) -> bool | None:
    field = contract.get("field")
    target = _expected_field(expected, field)
    if not isinstance(target, bool):
        return None
    if isinstance(actual, bool):
        return actual is target
    if _subject_conflict(_as_text(actual), contract):
        return None
    # Structured answer: every declared field must agree, so a wrong `relation`
    # cannot ride along behind a right `intersects` (round 4, P0).
    if isinstance(actual, dict) and isinstance(expected, dict):
        return _structured_mismatch(actual, expected)

    verdicts = _relation_verdicts(_as_text(actual))
    if verdicts is None or not verdicts:
        return None
    if len(verdicts) > 1:
        return None  # self-contradictory
    return next(iter(verdicts)) is target


def _verify_set(actual: Any, expected: Any, contract: dict[str, Any]) -> bool | None:
    field = contract.get("field")
    target = _expected_field(expected, field)
    if isinstance(target, list) and isinstance(actual, dict) and field:
        got = actual.get(field)
        if isinstance(got, list):
            return {str(x) for x in got} == {str(x) for x in target}
    if not isinstance(target, list) or not target:
        return None
    want = {str(x) for x in target}
    text = _as_text(actual)
    # Round 4 probe: "P001不在结果中，实际只有P002" must not count the
    # negated P001 as a positive member merely because its ID appears.
    for clause in _clauses(text):
        for match in _PARCEL_ID.finditer(clause):
            left = clause[max(0, match.start() - 8):match.start()]
            right = clause[match.end():match.end() + 8]
            if re.search(r"(?:不含|不包括|没有|排除|除)\s*$", left) or re.match(
                r"\s*(?:不在|未在|不属于|除外|外)", right
            ):
                return None
    got = set(_PARCEL_ID.findall(text))
    if not got:
        return None
    return got == want


def _subject_present(text: str, subjects: tuple[str, ...]) -> bool:
    folded = text.casefold()
    for subject in subjects:
        if _PARCEL_ID.fullmatch(subject):
            pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(subject)}(?![0-9])", re.I)
            if pattern.search(text):
                return True
        elif subject.casefold() in folded:
            return True
    return False


def _wrong_subject(text: str, subjects: tuple[str, ...]) -> bool:
    """Recognise an *explicit* different topic; silence remains unverifiable.

    Round 4 T22 probe: "无法查询天气" is about weather, not parcel P999.
    This is only a rejection rule, never positive evidence of a correct refusal.
    """
    folded = text.casefold()
    wanted_ids = {s.upper() for s in subjects if _PARCEL_ID.fullmatch(s)}
    mentioned_ids = set(_PARCEL_ID.findall(text.upper()))
    if mentioned_ids and wanted_ids and not mentioned_ids <= wanted_ids:
        return True
    if "天气" in text and not any("天气" in s for s in subjects):
        return True
    if "buffer_features" in folded and not any(
        "buffer_features" in s.casefold() for s in subjects
    ):
        return True
    return False


def _reason_supported(text: str, reason: str, subjects: tuple[str, ...]) -> bool:
    """Require evidence for the declared refusal reason, not any inability word."""
    folded = text.casefold()
    if reason == "unsupported_granularity":
        return bool(
            re.search(r"(?:无法|不能|不支持|做不到)\s*删除单个要素", text)
            and ("工具" in text or "delete_dataset" in folded or "整个" in text)
        )
    if reason == "outside_allowed_roots":
        return bool(re.search(r"允许范围之外|路径校验拒绝|越界|路径不被允许", text))
    if reason == "missing_entity":
        # "P999无法查询，别的对象不存在" is not evidence that P999 is absent.
        return any(
            _subject_present(clause, subjects)
            and re.search(r"不存在|未找到|找不到|查无|未查到", clause)
            for clause in _clauses(text)
        )
    if reason == "unknown_tool":
        return bool(
            re.search(
                r"(?:没有|不存在|未提供|不支持|找不到|不可用)\s*"
                r"(?:这个|名为)?\s*buffer_features",
                text,
                re.I,
            )
            or re.search(r"buffer_features\s*(?:不存在|不可用|未提供)", text, re.I)
        )
    if reason == "out_of_domain":
        return bool(
            "天气" in text
            and ("数据源" in text or "地理处理" in text)
            and re.search(r"没有|无|不提供|无法|不能", text)
        )
    return False


def _verify_refusal(actual: Any, expected: Any, contract: dict[str, Any]) -> bool | None:
    """First exclude contradictions, then require the subject and reason."""
    subjects = tuple(contract.get("subjects") or ())
    reason = contract.get("reason")
    # The v5 contract extends refusal with both fields. A legacy bare
    # {"kind": "refusal"} cannot establish what was refused: needs review.
    if not subjects or not isinstance(reason, str) or not reason:
        return None
    if not isinstance(actual, str):
        return _structured_mismatch(actual, expected)
    text = _as_text(actual)
    clauses = _clauses(text)
    for clause in clauses:
        if _has_completion_claim(clause):
            return False
        for relation in _RELATION.finditer(clause):
            if _NEGATION_AT_END.search(clause[:relation.start()]):
                continue
            tail = clause[relation.end():relation.end() + 15]
            magnitude = _NUMBER.search(tail)
            if magnitude and float(magnitude.group().replace(",", "")) > 0:
                return False
    # All determinate contradictions have been scanned before any undecidable
    # outcome clause can return None ("未占压，但占压100平方米" regression).
    for clause in clauses:
        # A refusal followed by an unexplained output statement must not
        # pass. Round 5 probe: "没有 buffer_features，但缓冲区已经在地图上了".
        if any(term in clause for term in _OUTCOME_TERMS.get(reason, ())):
            if not _OUTCOME_DENIAL.search(clause):
                return None
    if reason == "missing_entity" and re.search(r"(?<!不)存在", text):
        return False
    if reason == "unknown_tool" and re.search(r"buffer_features\s*(?:可用|存在)", text, re.I):
        return False
    subject_present = _subject_present(text, subjects)
    if _wrong_subject(text, subjects):
        return None if subject_present else False
    if not subject_present:
        return None
    if not _reason_supported(text, reason, subjects):
        return None
    return True


def verify_answer(
    actual: Any,
    expected: Any,
    *,
    contract: dict[str, Any] | None = None,
    answer_type: str = "",
    tolerance: float | None = None,
) -> bool | None:
    """Verify ``actual`` against ``expected`` under the task's contract.

    ``None`` means **unverifiable** -- the caller must record it as *needs
    review*, which is neither a pass nor a failure.
    """
    if actual is None or expected is None:
        return None

    spec = dict(contract or {})
    if not spec and answer_type:
        # Backwards-compatible fallback for callers not yet migrated. The
        # declared ``answer_type`` vocabulary predates the contract vocabulary,
        # so it is translated explicitly; an unknown type stays unverifiable
        # rather than being guessed at.
        spec = {"kind": _TYPE_TO_KIND.get(answer_type, "")}

    kind = spec.get("kind", "")
    if tolerance is not None and "tolerance" not in spec:
        spec["tolerance"] = tolerance

    if kind in ("int", "float"):
        return _verify_scalar(actual, expected, spec)
    if kind == "bool":
        return _verify_bool(actual, expected, spec)
    if kind == "set":
        return _verify_set(actual, expected, spec)
    if kind == "refusal":
        return _verify_refusal(actual, expected, spec)
    return None


__all__ = ["verify_answer"]
