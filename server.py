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


# ------------------------------------------------------------------ routes

def route(method, path, query, body):
    s = config.get_settings()

    if path == "/api/health":
        return {"ok": True, "provider": s["provider"], "model": s["anthropic_model"],
                "has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "has_gemini_key": bool(os.environ.get("GEMINI_API_KEY")),
                "has_api_url": bool(os.environ.get("CQA_API_URL"))}

    if path == "/api/settings":
        if method == "PUT":
            return config.update_settings(body or {})
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
        return batch.fetch_dataset((body or {}).get("group_id") or "DEV")

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
                                      "has_changes": ev.get("has_changes", False)}
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
                return storage.save_checklist(body or {})
            except ValueError as e:
                raise ApiError(str(e))
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
            return evaluate(b.get("conversation_id"), checklist_id=b.get("checklist_id"),
                            checklist_version=b.get("checklist_version"), provider=b.get("provider"))
        except (ProviderError, KeyError, ValueError) as e:
            raise ApiError(str(e).strip("'"))

    if path == "/api/evaluate/batch" and method == "POST":
        b = body or {}
        return batch.start_batch(b.get("group_id") or "DEV", checklist_id=b.get("checklist_id"),
                                 force=bool(b.get("force")), provider=b.get("provider"))

    if path == "/api/jobs":
        return batch.list_jobs()

    if path == "/api/export" and method == "POST":
        b = body or {}
        _, meta = batch.export_results(b.get("group_id") or "DEV", folder=b.get("folder"))
        return meta

    if path.startswith("/api/evaluations/"):
        parts = path.split("/")
        ev_id = parts[3]
        if len(parts) == 4 and method == "GET":
            ev = storage.get_evaluation(ev_id)
            if not ev:
                raise ApiError("оценка не найдена", 404)
            return ev
        try:
            if len(parts) == 5 and parts[4] == "appeals" and method == "POST":
                b = body or {}
                return storage.create_appeal(ev_id, b.get("criterion_id"), b.get("comment"), b.get("author") or "Оператор")
            if len(parts) == 7 and parts[4] == "appeals" and parts[6] == "decision" and method == "POST":
                b = body or {}
                return storage.decide_appeal(ev_id, parts[5], b.get("action"), b.get("reason"),
                                             new_answer=b.get("new_answer"), reviewer=b.get("reviewer") or "Проверяющий")
        except KeyError as e:
            raise ApiError(str(e).strip("'"), 404)
        except ValueError as e:
            raise ApiError(str(e))

    if path == "/api/appeals":
        return storage.open_appeals()

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
            self._send_json(route(self.command, parsed.path, query, body))
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
