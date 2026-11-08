"""File-based storage: datasets (data/*.json), versioned checklists (checklists/*.json),
evaluations with appeals (storage/evaluations/*.json)."""
import datetime
import glob
import json
import os
import re
import threading
import uuid

from . import config
from .scoring import compute_score, normalize_answer

_lock = threading.RLock()


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- datasets

def _dataset_files():
    return sorted(glob.glob(os.path.join(config.DATA_DIR, "*.json")))


def _records_of(obj):
    """Accept the organizers' API envelope, a bare list, or {"records": [...]}."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        if isinstance(obj.get("data"), dict):
            return obj["data"].get("records") or []
        if "records" in obj:
            return obj["records"]
        if "transcript" in obj:
            return [obj]
    return []


def list_datasets():
    out = []
    for p in _dataset_files():
        name = os.path.splitext(os.path.basename(p))[0]
        try:
            n = len(_records_of(_read(p)))
        except (ValueError, OSError):
            n = 0
        out.append({"group_id": name.upper(), "file": os.path.basename(p), "count": n})
    return out


def all_conversations():
    convs = {}
    for p in _dataset_files():
        group = os.path.splitext(os.path.basename(p))[0].upper()
        try:
            recs = _records_of(_read(p))
        except (ValueError, OSError):
            continue
        for r in recs:
            r = dict(r)
            r["group_id"] = group
            convs[r["conversation_id"]] = r
    return convs


def get_conversation(cid):
    return all_conversations().get(cid)


def save_dataset(group_id, api_obj):
    group = re.sub(r"[^A-Za-z0-9_-]", "", group_id).lower() or "uploaded"
    _write(os.path.join(config.DATA_DIR, group + ".json"), api_obj)
    return group.upper()


def add_uploaded(records):
    """Append records uploaded through the UI to data/uploaded.json."""
    with _lock:
        path = os.path.join(config.DATA_DIR, "uploaded.json")
        cur = _records_of(_read(path)) if os.path.exists(path) else []
        ids = {r["conversation_id"] for r in records}
        cur = [r for r in cur if r["conversation_id"] not in ids] + records
        _write(path, {"error": "", "isSuccess": True,
                      "data": {"dataset_group_id": "UPLOADED", "total_records": len(cur), "records": cur}})


# ---------------------------------------------------------------- checklists

def _checklist_files():
    return glob.glob(os.path.join(config.CHECKLIST_DIR, "*_v*.json"))


def list_checklists(all_versions=False):
    latest = {}
    versions = []
    for p in _checklist_files():
        try:
            c = _read(p)
        except (ValueError, OSError):
            continue
        versions.append(c)
        if c["id"] not in latest or c["version"] > latest[c["id"]]["version"]:
            latest[c["id"]] = c
    if all_versions:
        return sorted(versions, key=lambda c: (c["id"], c["version"]))
    return sorted(latest.values(), key=lambda c: c["id"])


def get_checklist(cid, version=None):
    best = None
    for c in list_checklists(all_versions=True):
        if c["id"] != cid:
            continue
        if version is not None and int(version) == c["version"]:
            return c
        if version is None and (best is None or c["version"] > best["version"]):
            best = c
    return best


def validate_checklist(c):
    errors = []
    if not re.match(r"^[a-z0-9_-]+$", str(c.get("id", ""))):
        errors.append("id чек-листа: только латиница в нижнем регистре, цифры, _ и -")
    if not c.get("name"):
        errors.append("нет названия чек-листа")
    crits = c.get("criteria") or []
    if not crits:
        errors.append("нет ни одного критерия")
    seen = set()
    sections = {s["id"] for s in c.get("sections") or []}
    for i, cr in enumerate(crits, 1):
        label = "критерий %d" % i
        if not cr.get("id") or cr["id"] in seen:
            errors.append(label + ": пустой или повторяющийся id")
        seen.add(cr.get("id"))
        if not cr.get("title"):
            errors.append(label + ": нет названия")
        if cr.get("type") not in ("binary", "scale"):
            errors.append(label + ": тип должен быть binary или scale")
        try:
            w = float(cr.get("weight") or 0)
            if w < 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(label + ": вес должен быть числом ≥ 0")
        if cr.get("type") == "scale":
            vals = [s.get("value") for s in cr.get("scale") or []]
            if len(vals) < 2:
                errors.append(label + ": у шкалы должно быть минимум 2 ступени")
            try:
                vals = [int(v) for v in vals]
                if max(vals) <= 0:
                    errors.append(label + ": верхняя ступень шкалы должна быть > 0")
                if len(set(vals)) != len(vals):
                    errors.append(label + ": ступени шкалы повторяются")
            except (TypeError, ValueError):
                errors.append(label + ": ступени шкалы должны быть целыми числами")
        if cr.get("section") and sections and cr["section"] not in sections:
            errors.append(label + ": неизвестная секция " + str(cr["section"]))
    return errors


def save_checklist(c):
    """Save as a NEW version (old versions are kept so old evaluations stay reproducible)."""
    errors = validate_checklist(c)
    if errors:
        raise ValueError("; ".join(errors))
    with _lock:
        prev = get_checklist(c["id"])
        c = dict(c)
        c["version"] = (prev["version"] + 1) if prev else 1
        c["saved_at"] = now_iso()
        c.setdefault("rounding", 2)
        _write(os.path.join(config.CHECKLIST_DIR, "%s_v%d.json" % (c["id"], c["version"])), c)
        return c


# ---------------------------------------------------------------- evaluations

def _eval_dir():
    return os.path.join(config.STORAGE_DIR, "evaluations")


def save_evaluation(ev):
    with _lock:
        _write(os.path.join(_eval_dir(), ev["id"] + ".json"), ev)
    return ev


def get_evaluation(eid):
    p = os.path.join(_eval_dir(), os.path.basename(eid) + ".json")
    return _read(p) if os.path.exists(p) else None


def list_evaluations(conversation_id=None):
    out = []
    for p in glob.glob(os.path.join(_eval_dir(), "*.json")):
        try:
            ev = _read(p)
        except (ValueError, OSError):
            continue
        if conversation_id and ev["conversation_id"] != conversation_id:
            continue
        out.append(ev)
    return sorted(out, key=lambda e: e["created_at"], reverse=True)


def latest_evaluations(checklist_id=None):
    """Most recent evaluation per conversation (optionally for one checklist id)."""
    best = {}
    for ev in list_evaluations():
        if checklist_id and ev["checklist"]["id"] != checklist_id:
            continue
        cur = best.get(ev["conversation_id"])
        if cur is None or ev["created_at"] > cur["created_at"]:
            best[ev["conversation_id"]] = ev
    return best


def save_error(conversation_id, message):
    """Remember that an evaluation failed, so results.json can report status=error honestly."""
    with _lock:
        _write(os.path.join(config.STORAGE_DIR, "errors", conversation_id + ".json"),
               {"conversation_id": conversation_id, "error": message, "at": now_iso()})


def get_errors():
    out = {}
    for p in glob.glob(os.path.join(config.STORAGE_DIR, "errors", "*.json")):
        try:
            e = _read(p)
            out[e["conversation_id"]] = e
        except (ValueError, OSError, KeyError):
            continue
    return out


def new_id(prefix):
    return prefix + "_" + datetime.datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6]


# ---------------------------------------------------------------- appeals

def _final_answers(ev):
    answers = {r["criterion_id"]: r["answer"] for r in ev["results"]}
    for a in ev.get("appeals", []):
        d = a.get("decision")
        if d and d["action"] == "change":
            answers[a["criterion_id"]] = d["new_answer"]
    return answers


def recompute_final(ev):
    checklist = ev["checklist"]
    final_answers = _final_answers(ev)
    ev["final_score"] = compute_score(checklist, {k: v for k, v in final_answers.items() if v is not None})
    ev["final_answers"] = final_answers
    ev["has_changes"] = any(a.get("decision", {}) and a["decision"]["action"] == "change"
                            for a in ev.get("appeals", []))
    return ev


def create_appeal(eid, criterion_id, comment, author="Оператор"):
    with _lock:
        ev = get_evaluation(eid)
        if not ev:
            raise KeyError("оценка не найдена")
        if criterion_id not in {c["id"] for c in ev["checklist"]["criteria"]}:
            raise ValueError("нет такого критерия в чек-листе этой оценки")
        if not (comment or "").strip():
            raise ValueError("добавьте комментарий к апелляции")
        if any(a["criterion_id"] == criterion_id and a["status"] == "open" for a in ev.get("appeals", [])):
            raise ValueError("по этому критерию уже есть открытая апелляция")
        appeal = {"id": new_id("ap"), "criterion_id": criterion_id, "comment": comment.strip(),
                  "author": author, "created_at": now_iso(), "status": "open",
                  "disputed_answer": _final_answers(ev).get(criterion_id), "decision": None}
        ev.setdefault("appeals", []).append(appeal)
        save_evaluation(ev)
        return ev


def decide_appeal(eid, appeal_id, action, reason, new_answer=None, reviewer="Проверяющий"):
    with _lock:
        ev = get_evaluation(eid)
        if not ev:
            raise KeyError("оценка не найдена")
        appeal = next((a for a in ev.get("appeals", []) if a["id"] == appeal_id), None)
        if not appeal:
            raise KeyError("апелляция не найдена")
        if appeal["status"] != "open":
            raise ValueError("апелляция уже рассмотрена")
        if action not in ("confirm", "change"):
            raise ValueError("решение: confirm или change")
        if not (reason or "").strip():
            raise ValueError("укажите причину решения")
        crit = next(c for c in ev["checklist"]["criteria"] if c["id"] == appeal["criterion_id"])
        decision = {"action": action, "reason": reason.strip(), "reviewer": reviewer, "decided_at": now_iso()}
        if action == "change":
            decision["new_answer"] = normalize_answer(crit, new_answer)
            if decision["new_answer"] == appeal["disputed_answer"]:
                raise ValueError("новая оценка совпадает с оспоренной — выберите «Подтвердить»")
        appeal["decision"] = decision
        appeal["status"] = "resolved"
        recompute_final(ev)
        save_evaluation(ev)
        return ev


def open_appeals():
    out = []
    for ev in list_evaluations():
        for a in ev.get("appeals", []):
            out.append({"evaluation_id": ev["id"], "conversation_id": ev["conversation_id"],
                        "checklist": ev["checklist"]["name"], **a})
    return sorted(out, key=lambda a: (a["status"] != "open", a["created_at"]))


def delete_evaluations(ids):
    removed = []
    with _lock:
        for eid in ids:
            p = os.path.join(_eval_dir(), os.path.basename(eid) + ".json")
            if os.path.exists(p):
                removed.append({"id": eid, "conversation_id": _read(p)["conversation_id"]})
                os.remove(p)
    return removed


# ---------------------------------------------------------------- audit log (append-only)

def _audit_path():
    return os.path.join(config.STORAGE_DIR, "audit.jsonl")


def audit(action, details, actor):
    entry = {"at": now_iso(), "actor": actor, "action": action, "details": details}
    with _lock:
        os.makedirs(config.STORAGE_DIR, exist_ok=True)
        with open(_audit_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def list_audit(limit=500):
    if not os.path.exists(_audit_path()):
        return []
    with open(_audit_path(), encoding="utf-8") as f:
        lines = [l for l in f if l.strip()]
    return [json.loads(l) for l in reversed(lines[-limit:])]
