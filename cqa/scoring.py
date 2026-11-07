"""Deterministic scoring of checklist answers.

Rules (from the organizers' checklist, page 2):
1. N/A only where the criterion allows it.
2. "Yes" gives the full weight, "No" gives zero. Scale criteria take one of the listed steps.
3. Scale values are converted to points proportionally to the weight; the top step gives the full weight.
4. N/A criteria are excluded and their weight is redistributed proportionally among the
   applicable criteria, so the maximum stays 100.
5. The total is rounded to two decimals. If no weighted criterion is applicable, no total.
   Critical errors are flagged separately (they carry no points).

The model only *classifies* each criterion; every number here is computed in code.
"""
from decimal import Decimal, ROUND_HALF_UP

YES, NO, NA = "yes", "no", "na"


def round_half_up(value, digits=2):
    if value is None:
        return None
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def scale_values(criterion):
    return sorted(int(s["value"]) for s in criterion.get("scale") or [])


def allowed_answers(criterion):
    """List of answer strings the model / reviewer may use for this criterion."""
    if criterion["type"] == "scale":
        out = [str(v) for v in sorted(scale_values(criterion), reverse=True)]
    else:
        out = [YES, NO]
    if criterion.get("allow_na"):
        out.append(NA)
    return out


def normalize_answer(criterion, answer):
    """Return the canonical answer string, or raise ValueError if not allowed."""
    if answer is None:
        raise ValueError("пустой ответ")
    a = str(answer).strip().lower()
    aliases = {"да": YES, "иә": YES, "true": YES, "нет": NO, "жоқ": NO, "false": NO,
               "n/a": NA, "н/д": NA, "n\\a": NA, "не применимо": NA}
    a = aliases.get(a, a)
    if criterion["type"] == "scale" and a not in (NA,):
        try:
            a = str(int(float(a)))
        except ValueError:
            pass
    if a not in allowed_answers(criterion):
        raise ValueError("ответ «%s» не разрешён для критерия %s (разрешено: %s)"
                         % (answer, criterion["id"], ", ".join(allowed_answers(criterion))))
    return a


def is_weighted(criterion):
    return not criterion.get("critical") and float(criterion.get("weight") or 0) > 0


def raw_fraction(criterion, answer):
    """Share of the criterion's weight earned (0..1), None for N/A."""
    if answer == NA:
        return None
    if criterion["type"] == "scale":
        top = max(scale_values(criterion))
        return (int(answer) / top) if top else 0.0
    return 1.0 if answer == YES else 0.0


def compute_score(checklist, answers):
    """answers: {criterion_id: answer_string}. Returns a full breakdown.

    Missing answers are treated as errors (the criterion is reported with status "missing"
    and excluded from the total), so a broken model response never silently counts as N/A.
    """
    digits = int(checklist.get("rounding", 2))
    criteria = checklist["criteria"]

    applicable_weight = 0.0
    rows = []
    for c in criteria:
        ans = answers.get(c["id"])
        row = {"criterion_id": c["id"], "title": c["title"], "section": c.get("section"),
               "type": c["type"], "weight": float(c.get("weight") or 0),
               "critical": bool(c.get("critical")), "answer": ans}
        if ans is None:
            row["status"] = "missing"
        elif ans == NA:
            row["status"] = "na"
        else:
            row["status"] = "scored"
        if is_weighted(c) and row["status"] == "scored":
            applicable_weight += row["weight"]
        rows.append(row)

    critical_flag = False
    earned_total = 0.0
    for c, row in zip(criteria, rows):
        if row["critical"]:
            row["points"] = None
            row["max_points"] = None
            if row["answer"] == YES:
                critical_flag = True
            continue
        if not is_weighted(c) or row["status"] != "scored":
            row["points"] = None
            row["max_points"] = None
            row["effective_weight"] = 0.0
            continue
        frac = raw_fraction(c, row["answer"])
        base_points = frac * row["weight"]                      # before redistribution
        eff_weight = row["weight"] * 100.0 / applicable_weight  # weight after redistribution
        row["base_points"] = round_half_up(base_points, digits)
        row["effective_weight"] = round_half_up(eff_weight, digits)
        row["max_points"] = row["effective_weight"]
        row["points"] = round_half_up(frac * eff_weight, digits)
        earned_total += frac * eff_weight

    total = round_half_up(earned_total, digits) if applicable_weight > 0 else None

    sections = []
    for s in checklist.get("sections") or []:
        srows = [r for r in rows if r["section"] == s["id"] and r.get("max_points") is not None]
        if not srows:
            continue
        got = sum(r["points"] for r in srows)
        mx = sum(r["max_points"] for r in srows)
        sections.append({"section_id": s["id"], "title": s["title"],
                         "points": round_half_up(got, digits), "max_points": round_half_up(mx, digits),
                         "percent": round_half_up(got / mx * 100, digits) if mx else None})

    return {
        "total": total,
        "max_total": 100 if applicable_weight > 0 else None,
        "applicable_weight": applicable_weight,
        "critical_error": critical_flag,
        "missing": [r["criterion_id"] for r in rows if r["status"] == "missing"],
        "criteria": rows,
        "sections": sections,
    }
