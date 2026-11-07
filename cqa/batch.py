"""Dataset fetching, batch evaluation and results.json export."""
import json
import os
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import config, storage
from .evaluator import ProviderError, evaluate

FORMAT_VERSION = "cqa-results/1"


def fetch_dataset(group_id):
    base = os.environ.get("CQA_API_URL", "").strip()
    if not base:
        raise ProviderError("нет CQA_API_URL в файле .env (ссылка GET из гайда организаторов)")
    parts = urllib.parse.urlsplit(base)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != "group_id"]
    q.append(("group_id", group_id))
    url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            obj = json.loads(r.read().decode("utf-8"))
    except OSError as e:
        raise ProviderError("API организаторов недоступно: %s" % e)
    if not obj.get("isSuccess"):
        raise ProviderError("API организаторов вернуло ошибку: %s" % (obj.get("error") or "isSuccess=false"))
    group = storage.save_dataset(group_id, obj)
    return {"group_id": group, "count": len(obj["data"]["records"])}


# ---------------------------------------------------------------- jobs

_jobs = {}
_jobs_lock = threading.Lock()


def _job_view(job):
    return {k: v for k, v in job.items() if not k.startswith("_")}


def get_job(job_id):
    with _jobs_lock:
        j = _jobs.get(job_id)
        return _job_view(j) if j else None


def list_jobs():
    with _jobs_lock:
        return [_job_view(j) for j in sorted(_jobs.values(), key=lambda j: j["started_at"], reverse=True)]


def run_batch(group_id, checklist_id=None, force=False, workers=4, provider=None, on_progress=None):
    """Evaluate every conversation of a group. Returns the job dict (blocking)."""
    convs = [c for c in storage.all_conversations().values() if c["group_id"] == group_id.upper()]
    done = storage.latest_evaluations()
    todo = []
    for c in convs:
        ev = done.get(c["conversation_id"])
        if force or ev is None or (checklist_id and ev["checklist"]["id"] != checklist_id):
            todo.append(c)
    job = {"id": storage.new_id("job"), "group_id": group_id.upper(), "total": len(todo), "done": 0,
           "failed": 0, "errors": [], "status": "running", "started_at": storage.now_iso(), "cost_usd": 0.0,
           "skipped": len(convs) - len(todo)}
    with _jobs_lock:
        _jobs[job["id"]] = job

    def one(conv):
        try:
            ev = evaluate(conv["conversation_id"], checklist_id=checklist_id, provider=provider)
            with _jobs_lock:
                job["done"] += 1
                job["cost_usd"] = round(job["cost_usd"] + ev.get("cost_usd", 0), 5)
        except Exception as e:  # keep the batch going; report per conversation
            storage.save_error(conv["conversation_id"], str(e).strip("'"))
            with _jobs_lock:
                job["failed"] += 1
                job["errors"].append({"conversation_id": conv["conversation_id"], "error": str(e)})
        if on_progress:
            on_progress(_job_view(job))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, todo))
    job["status"] = "finished"
    job["finished_at"] = storage.now_iso()
    return _job_view(job)


def start_batch(group_id, checklist_id=None, force=False, provider=None):
    t = threading.Thread(target=run_batch, args=(group_id, checklist_id, force), kwargs={"provider": provider},
                         daemon=True)
    t.start()
    # the job is registered synchronously at the start of run_batch; wait for it briefly
    for _ in range(50):
        with _jobs_lock:
            js = [j for j in _jobs.values() if j["group_id"] == group_id.upper() and j["status"] == "running"]
        if js:
            return _job_view(js[-1])
        threading.Event().wait(0.05)
    return {"status": "starting", "group_id": group_id.upper()}


# ---------------------------------------------------------------- export
#
# Official format (CQA_Student_Guide_Appendix_Results.pdf, JSON 1.0):
#   {"schema_version": "1.0", "team_name": ..., "model": {"mode": local|external_api|hybrid, "name": ...},
#    "results": [{"conversation_id", "status": "ok", "criteria": [{"criterion_id", "value", "rationale",
#                 "evidence_turns": [1-based]}], "score": {"final_score", "critical_flag"}}
#                {"conversation_id", "status": "error", "criteria": [], "score": null, "error": "..."}]}
# value: true/false/null for yes/no/N/A, integer for scale criteria (before weighting).

def _official_value(crit, answer):
    if answer is None or answer == "na":
        return None
    if crit["type"] == "scale":
        return int(answer)
    return answer == "yes"


def _mode_for(models):
    kinds = set()
    for m in models:
        if m.startswith("ollama:") or m == "mock":
            kinds.add("local")
        else:
            kinds.add("external_api")
    if len(kinds) > 1:
        return "hybrid"
    return kinds.pop() if kinds else "external_api"


def _model_label(m):
    if m == "mock":
        return "heuristic-demo (no AI)"
    return m.replace("ollama:", "") if m else "unknown"


def export_record(ev):
    """One official result record built from the FINAL (post-appeal) answers."""
    final = ev.get("final_score") or ev["score"]
    final_answers = ev.get("final_answers") or {r["criterion_id"]: r["answer"] for r in ev["results"]}
    crit_by_id = {c["id"]: c for c in ev["checklist"]["criteria"]}
    appeals = {a["criterion_id"]: a for a in ev.get("appeals", []) if a.get("decision")}
    criteria = []
    for r in ev["results"]:
        c = crit_by_id[r["criterion_id"]]
        ans = final_answers.get(r["criterion_id"])
        if ans is None:
            # the model never produced a valid answer for this criterion
            return {"conversation_id": ev["conversation_id"], "status": "error", "criteria": [], "score": None,
                    "error": "нет корректной оценки по критерию %s" % c.get("code", c["id"])}
        rationale = r.get("explanation") or ""
        ap = appeals.get(r["criterion_id"])
        if ap and ap["decision"]["action"] == "change":
            rationale = "Пересмотрено проверяющим: %s (исходно «%s»). %s" % (
                ap["decision"]["reason"], ap["disputed_answer"], rationale)
        criteria.append({
            "criterion_id": c.get("code") or c["id"],
            "value": _official_value(c, ans),
            "rationale": rationale.strip(),
            "evidence_turns": [i + 1 for i in (r.get("evidence_ids") or [])],  # official numbering starts at 1
        })
    return {
        "conversation_id": ev["conversation_id"],
        "status": "ok",
        "criteria": criteria,
        "score": {"final_score": final.get("total"), "critical_flag": bool(final.get("critical_error"))},
    }


def validate_official(out, expected_ids=None):
    """Checks from the appendix: one record per dialog, no duplicates, 8 criteria once each for ok."""
    problems = []
    ids = [r["conversation_id"] for r in out["results"]]
    if len(ids) != len(set(ids)):
        problems.append("повторяющиеся conversation_id")
    if expected_ids is not None:
        missing = sorted(set(expected_ids) - set(ids))
        extra = sorted(set(ids) - set(expected_ids))
        if missing:
            problems.append("нет записей для: " + ", ".join(missing))
        if extra:
            problems.append("лишние записи: " + ", ".join(extra))
    official = ["contact", "discovery", "identification", "consultation", "empathy", "dialogue", "closure", "critical"]
    for r in out["results"]:
        if r["status"] == "ok":
            codes = [c["criterion_id"] for c in r["criteria"]]
            if sorted(codes) == sorted(official) and codes != official:
                pass
            if len(codes) != 8 or len(set(codes)) != 8:
                problems.append("%s: должно быть ровно 8 критериев по одному разу (сейчас %d)" % (r["conversation_id"], len(codes)))
            unknown = [c for c in codes if c not in official]
            if unknown:
                problems.append("%s: нестандартные коды критериев %s (жюри ждёт официальные)" % (r["conversation_id"], unknown))
            if r["score"]["final_score"] is None:
                problems.append("%s: final_score пустой" % r["conversation_id"])
        elif r["status"] != "error":
            problems.append("%s: неизвестный status" % r["conversation_id"])
    return problems


def export_results(group_id, write=True, folder=None):
    """folder: 'dev' or 'prod' (results/<folder>/results.json). Default: DEV -> dev, others -> prod."""
    group = group_id.upper()
    settings = config.get_settings()
    convs = {k: v for k, v in storage.all_conversations().items() if v["group_id"] == group}
    latest = storage.latest_evaluations()
    errors = storage.get_errors()
    records, models, costs = [], set(), []
    for cid in sorted(convs):
        ev = latest.get(cid)
        if ev is None:
            err = errors.get(cid, {}).get("error") or "Диалог не был оценён."
            records.append({"conversation_id": cid, "status": "error", "criteria": [], "score": None, "error": err})
            continue
        records.append(export_record(ev))
        if ev.get("model"):
            models.add(ev["model"])
        if ev.get("cost_usd"):
            costs.append(ev["cost_usd"])
    model_names = sorted(_model_label(m) for m in models)
    out = {
        "schema_version": "1.0",
        "team_name": settings.get("team_name") or "Название команды",
        "model": {"mode": _mode_for(models), "name": ", ".join(model_names) or "unknown"},
        "results": records,
    }
    problems = validate_official(out, expected_ids=list(convs))
    ok = sum(1 for r in records if r["status"] == "ok")
    meta = {"dataset_group_id": group, "generated_at": storage.now_iso(), "total_records": len(convs),
            "ok_records": ok, "error_records": len(records) - ok, "problems": problems,
            "avg_cost_usd_per_evaluation": round(sum(costs) / len(costs), 5) if costs else 0}
    if write:
        folder = folder or ("dev" if group == "DEV" else "prod")
        path = os.path.join(config.RESULTS_DIR, folder, "results.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        # richer internal dump next to it (not required by the organizers)
        with open(os.path.join(config.RESULTS_DIR, folder, "results_detailed.json"), "w", encoding="utf-8") as f:
            json.dump({"meta": meta, "evaluations": [latest[c] for c in sorted(convs) if c in latest]},
                      f, ensure_ascii=False, indent=1)
        meta["path"] = os.path.relpath(path, config.ROOT)
    return out, meta
