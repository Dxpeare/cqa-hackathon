#!/usr/bin/env python3
"""CQA prototype server: static web UI + JSON API. Standard library only.

    python3 server.py            # http://localhost:8080
    python3 server.py --port 9000
"""
import argparse
import json
import mimetypes
import os
import sys
import traceback
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cqa import batch, config, storage  # noqa: E402
from cqa.evaluator import ProviderError, call_metrics, evaluate, gemini_models, relative_times, fmt_offset  # noqa: E402

config.load_env()


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def conversation_view(conv):
    times = relative_times(conv)
    out = dict(conv)
    out["transcript"] = [dict(m, index=i, offset=fmt_offset(times[i]), offset_sec=times[i])
                         for i, m in enumerate(conv["transcript"])]
    if conv.get("channel") == "voice call":
        out["metrics"] = call_metrics(conv)
    return out


def analytics(group):
    """Aggregates over the latest evaluation of every conversation (final score after appeals)."""
    convs = storage.all_conversations()
    evs = [e for e in storage.latest_evaluations().values()
           if e["conversation_id"] in convs and (not group or convs[e["conversation_id"]]["group_id"] == group)]

    def avg(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 2) if xs else None

    def by(key):
        out = {}
        for e in evs:
            out.setdefault(key(e) or "—", []).append((e.get("final_score") or e["score"])["total"])
        return [{"key": k, "count": len(v), "avg": avg(v)} for k, v in sorted(out.items())]

    crit = {}
    for e in evs:
        for r in (e.get("final_score") or e["score"])["criteria"]:
            c = crit.setdefault(r["title"], {"title": r["title"], "critical": r["critical"], "scored": 0, "na": 0,
                                            "fails": 0, "fractions": []})
            if r["status"] == "na":
                c["na"] += 1
            elif r["status"] == "scored":
                c["scored"] += 1
                if r["critical"]:
                    c["fails"] += r["answer"] == "yes"
                elif r.get("max_points"):
                    frac = r["points"] / r["max_points"]
                    c["fractions"].append(frac * 100)
                    c["fails"] += frac == 0
    for c in crit.values():
        c["avg_percent"] = avg(c.pop("fractions"))
    totals = [(e.get("final_score") or e["score"])["total"] for e in evs]
    bins = [("0–49", 0, 50), ("50–69", 50, 70), ("70–84", 70, 85), ("85–100", 85, 101)]
    appeals = [a for e in evs for a in e.get("appeals", [])]
    return {
        "count": len(evs), "avg": avg(totals),
        "critical": sum(1 for e in evs if (e.get("final_score") or e["score"])["critical_error"]),
        "appeals_open": sum(a["status"] == "open" for a in appeals),
        "appeals_changed": sum(bool(a.get("decision")) and a["decision"]["action"] == "change" for a in appeals),
        "distribution": [{"key": k, "count": sum(1 for t in totals if t is not None and lo <= t < hi)} for k, lo, hi in bins],
        "criteria": list(crit.values()),
        "by_language": by(lambda e: convs[e["conversation_id"]].get("language")),
        "by_channel": by(lambda e: convs[e["conversation_id"]].get("channel")),
        "by_day": by(lambda e: e["created_at"][:10]),
    }


# ------------------------------------------------------------------ routes

def route(method, path, query, body, actor="Пользователь"):
    s = config.get_settings()

    if path == "/api/health":
        return {"ok": True, "provider": s["provider"], "model": s["anthropic_model"],
                "has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "has_gemini_key": bool(os.environ.get("GEMINI_API_KEY")),
                "has_api_url": bool(os.environ.get("CQA_API_URL"))}

    if path == "/api/settings":
        if method == "PUT":
            new = config.update_settings(body or {})
            storage.audit("Изменены настройки", "провайдер: %s" % new["provider"], actor)
            return new
        return s

    if path == "/api/models":
        local = []
        try:
            with urllib.request.urlopen(s["ollama_url"].rstrip("/") + "/api/tags", timeout=5) as r:
                local = sorted(m["name"] for m in json.loads(r.read().decode("utf-8")).get("models", []))
        except OSError:
            pass
        return {"ollama": local, "gemini": gemini_models()}

    if path == "/api/datasets":
        return storage.list_datasets()

    if path == "/api/datasets/fetch" and method == "POST":
        r = batch.fetch_dataset((body or {}).get("group_id") or "DEV")
        storage.audit("Скачан набор из API", "%s: %s диалогов" % (r.get("group_id"), r.get("count")), actor)
        return r

    if path == "/api/datasets/upload" and method == "POST":
        recs = body if isinstance(body, list) else storage._records_of(body or {})
        recs = [r for r in recs if isinstance(r, dict) and r.get("conversation_id") and isinstance(r.get("transcript"), list)]
        if not recs:
            raise ApiError("в файле нет диалогов: нужен массив записей с conversation_id и transcript")
        for r in recs:
            r.setdefault("channel", "agent chat")
            r.setdefault("language", "RU")
            r.setdefault("audio_url", None)
        storage.add_uploaded(recs)
        storage.audit("Загружены диалоги из файла", "%d диалогов" % len(recs), actor)
        return {"count": len(recs)}

    if path == "/api/conversations":
        latest = storage.latest_evaluations()
        out = []
        for c in sorted(storage.all_conversations().values(), key=lambda c: (c["group_id"], c["conversation_id"])):
            ev = latest.get(c["conversation_id"])
            item = {"conversation_id": c["conversation_id"], "group_id": c["group_id"], "channel": c.get("channel"),
                    "language": c.get("language"), "messages": len(c["transcript"]),
                    "has_audio": bool(c.get("audio_url")), "evaluation": None}
            if ev:
                fs = ev.get("final_score") or ev["score"]
                item["evaluation"] = {"id": ev["id"], "total": fs["total"], "initial_total": ev["score"]["total"],
                                      "critical_error": fs["critical_error"], "checklist": ev["checklist"]["name"],
                                      "model": ev.get("model"), "created_at": ev["created_at"],
                                      "open_appeals": sum(1 for a in ev.get("appeals", []) if a["status"] == "open"),
                                      "has_changes": ev.get("has_changes", False),
                                      "min_confidence": min((r["confidence"] for r in ev["results"]
                                                             if r.get("confidence") is not None), default=None)}
            out.append(item)
        return out

    if path.startswith("/api/conversations/"):
        cid = urllib.parse.unquote(path.split("/", 3)[3])
        conv = storage.get_conversation(cid)
        if not conv:
            raise ApiError("диалог не найден", 404)
        view = conversation_view(conv)
        view["evaluations"] = [{"id": e["id"], "created_at": e["created_at"], "checklist": e["checklist"]["name"],
                                "checklist_version": e["checklist"]["version"], "total": (e.get("final_score") or e["score"])["total"],
                                "model": e.get("model")} for e in storage.list_evaluations(cid)]
        return view

    if path == "/api/checklists":
        if method == "POST":
            try:
                c = storage.save_checklist(body or {})
            except ValueError as e:
                raise ApiError(str(e))
            storage.audit("Сохранён чек-лист", "%s v%d" % (c["name"], c["version"]), actor)
            return c
        return storage.list_checklists(all_versions=query.get("all") == "1")

    if path.startswith("/api/checklists/"):
        cid = path.split("/")[3]
        c = storage.get_checklist(cid, query.get("version"))
        if not c:
            raise ApiError("чек-лист не найден", 404)
        return c

    if path == "/api/evaluate" and method == "POST":
        b = body or {}
        try:
            ev = evaluate(b.get("conversation_id"), checklist_id=b.get("checklist_id"),
                          checklist_version=b.get("checklist_version"), provider=b.get("provider"))
        except (ProviderError, KeyError, ValueError) as e:
            raise ApiError(str(e).strip("'"))
        storage.audit("Оценён диалог", "%s: %s баллов, %s, модель %s" % (
            ev["conversation_id"], ev["score"]["total"], ev["checklist"]["name"], ev.get("model")), actor)
        return ev

    if path == "/api/evaluate/batch" and method == "POST":
        b = body or {}
        job = batch.start_batch(b.get("group_id") or "DEV", checklist_id=b.get("checklist_id"),
                                force=bool(b.get("force")), provider=b.get("provider"))
        storage.audit("Переоценка всего набора" if b.get("force") else "Оценка неоценённых диалогов",
                      "%s: %s диалогов в очереди" % (job.get("group_id"), job.get("total", "?")), actor)
        return job

    if path == "/api/jobs":
        return batch.list_jobs()

    if path == "/api/export" and method == "POST":
        b = body or {}
        _, meta = batch.export_results(b.get("group_id") or "DEV", folder=b.get("folder"))
        storage.audit("Выгружен results.json", "%s: ok %s, error %s" % (meta.get("path"), meta.get("ok_records"), meta.get("error_records")), actor)
        return meta

    if path == "/api/evaluations/delete" and method == "POST":
        group = ((body or {}).get("group_id") or "").upper()
        convs = storage.all_conversations()
        ids = [e["id"] for e in storage.list_evaluations()
               if e["conversation_id"] in convs and convs[e["conversation_id"]]["group_id"] == group]
        removed = storage.delete_evaluations(ids)
        storage.audit("Удалены оценки набора", "%s: %d оценок" % (group, len(removed)), actor)
        return {"removed": len(removed)}

    if path.startswith("/api/evaluations/"):
        parts = path.split("/")
        ev_id = parts[3]
        if len(parts) == 4 and method == "GET":
            ev = storage.get_evaluation(ev_id)
            if not ev:
                raise ApiError("оценка не найдена", 404)
            return ev
        if len(parts) == 4 and method == "DELETE":
            removed = storage.delete_evaluations([ev_id])
            if not removed:
                raise ApiError("оценка не найдена", 404)
            storage.audit("Удалена оценка", "%s (%s)" % (removed[0]["conversation_id"], ev_id), actor)
            return {"removed": 1}
        try:
            if len(parts) == 5 and parts[4] == "appeals" and method == "POST":
                b = body or {}
                ev = storage.create_appeal(ev_id, b.get("criterion_id"), b.get("comment"), actor)
                storage.audit("Подана апелляция", "%s, критерий %s: %s" % (ev["conversation_id"], b.get("criterion_id"), b.get("comment")), actor)
                return ev
            if len(parts) == 7 and parts[4] == "appeals" and parts[6] == "decision" and method == "POST":
                b = body or {}
                ev = storage.decide_appeal(ev_id, parts[5], b.get("action"), b.get("reason"),
                                           new_answer=b.get("new_answer"), reviewer=actor)
                a = next(x for x in ev["appeals"] if x["id"] == parts[5])
                storage.audit("Решение по апелляции", "%s, критерий %s: %s%s, причина: %s, итог %s" % (
                    ev["conversation_id"], a["criterion_id"], "подтверждено" if b.get("action") == "confirm" else "изменено на ",
                    "" if b.get("action") == "confirm" else a["decision"]["new_answer"], b.get("reason"),
                    ev["final_score"]["total"]), actor)
                return ev
        except KeyError as e:
            raise ApiError(str(e).strip("'"), 404)
        except ValueError as e:
            raise ApiError(str(e))

    if path == "/api/appeals":
        return storage.open_appeals()

    if path == "/api/audit":
        return storage.list_audit()

    if path == "/api/analytics":
        return analytics((query.get("group") or "").upper())

    raise ApiError("нет такого маршрута: %s %s" % (method, path), 404)


# ------------------------------------------------------------------ server

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=config.WEB_DIR, **kw)

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/jobs"):
            sys.stderr.write("%s %s\n" % (self.command, self.path))

    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle_api(self):
        parsed = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        body = None
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except ValueError:
                return self._send_json({"error": "тело запроса не JSON"}, 400)
        try:
            actor = {"operator": "Оператор", "reviewer": "Проверяющий"}.get(self.headers.get("X-CQA-Role"), "Пользователь")
            self._send_json(route(self.command, parsed.path, query, body, actor))
        except ApiError as e:
            self._send_json({"error": str(e)}, e.status)
        except Exception as e:  # report instead of silently dropping the connection
            traceback.print_exc()
            self._send_json({"error": "внутренняя ошибка: %s" % e}, 500)

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self._handle_api()
        if self.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        return self._handle_api()

    def do_PUT(self):
        return self._handle_api()

    def do_DELETE(self):
        return self._handle_api()

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    mimetypes.add_type("application/javascript", ".js")
    os.makedirs(config.STORAGE_DIR, exist_ok=True)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    s = config.get_settings()
    print("CQA server: http://%s:%d  (provider: %s, key: %s)" % (
        a.host, a.port, s["provider"], "есть" if os.environ.get("ANTHROPIC_API_KEY") else "НЕТ — см. .env"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
