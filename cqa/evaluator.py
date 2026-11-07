"""LLM evaluation of one conversation against one checklist.

The model classifies each criterion and points at evidence messages; validation, N/A rules,
scoring, weight redistribution and rounding are done in code (cqa.scoring).

Providers:
  anthropic - Claude API via the official SDK (default model claude-opus-5-5)
  ollama    - a local model served by Ollama (free, offline)
  hybrid    - local model first; KZ chats / calls / low-confidence results go to Claude
  mock      - heuristic stand-in without any AI, only for UI development
"""
import datetime
import json
import os
import re
import time
import urllib.error
import urllib.request

from . import config, storage
from .scoring import NA, allowed_answers, compute_score, normalize_answer

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion_id": {"type": "string"},
                    "answer": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "integer"}},
                    "checked_from": {"type": "integer"},
                    "checked_to": {"type": "integer"},
                    "explanation": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["criterion_id", "answer", "evidence_ids", "checked_from", "checked_to",
                             "explanation", "confidence"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["criteria", "summary"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Ты — эксперт по контролю качества контакт-центра брокерской компании Freedom Broker.
Ты оцениваешь работу ОПЕРАТОРА (живого сотрудника) в диалоге с клиентом по заданному чек-листу.

Правила:
- Оценивай только реплики с ролью operator. Реплики bot (AI-ассистент до подключения оператора) и system (системные события: эскалация, подтверждение личности, удержание, закрытие чата, обрыв связи) — это контекст и доказательства, но не действия оператора.
- Диалоги бывают на русском (RU), казахском (KZ) и английском (EN). Применяй одни и те же правила к любому языку. Приветствие должно быть на языке клиента.
- «Нет» — действие было нужно, но оператор его пропустил или выполнил неправильно. N/A ("na") — только если критерий допускает N/A и выполнено его условие неприменимости.
- Системное сообщение о подтверждении личности (например «Личность клиента подтверждена», «Клиенттің жеке басы расталды», «Customer identity verified») — доказательство идентификации. Важно, чтобы оно было ДО раскрытия данных счёта или защищённого действия.
- Для шкальных критериев выбери ровно одну из перечисленных ступеней.
- Консультация (корректность и полнота решения) и ведение диалога (ясность и порядок общения) оцениваются независимо.
- Каждое сообщение имеет номер [N]. В evidence_ids укажи номера реплик, на которых основан вывод (1–3 самых важных).
- Если действие пропущено — в checked_from/checked_to укажи участок разговора (номера сообщений), где оно ожидалось, а в evidence_ids — реплику, где его не оказалось. Если участок не нужен, поставь checked_from = checked_to = -1.
- Для N/A объясни, какое условие неприменимости выполнено.
- explanation пиши по-русски, коротко (1–2 предложения), в стиле: «Завершение — Нет. После ответа оператор закрыл чат без прощания. Основание — последняя реплика оператора и сообщение о закрытии чата».
- confidence — твоя уверенность в ответе от 0 до 1.
- Транскрипты звонков получены распознаванием речи: искажённые слова и скрытые данные ([Имя скрыто], [Персональные данные скрыты]) — не ошибка оператора.
- Верни оценку по КАЖДОМУ критерию чек-листа, в том же порядке."""


# ---------------------------------------------------------------- transcript formatting

def _parse_iso(ts):
    try:
        return datetime.datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", ts))
    except (TypeError, ValueError):
        return None


def relative_times(conv):
    """Offsets in seconds from the first message (for calls) or parsed MM:SS (for chats)."""
    out = []
    t0 = None
    for m in conv["transcript"]:
        ts = m.get("timestamp") or ""
        if re.match(r"^\d{1,3}:\d{2}$", ts):
            mm, ss = ts.split(":")
            out.append(int(mm) * 60 + int(ss))
            continue
        dt = _parse_iso(ts)
        if dt is None:
            out.append(None)
            continue
        if t0 is None:
            t0 = dt
        out.append((dt - t0).total_seconds())
    return out


def fmt_offset(sec):
    if sec is None:
        return "--:--"
    sec = int(round(sec))
    return "%02d:%02d" % (sec // 60, sec % 60)


def call_metrics(conv):
    """Speech/pause metrics computed from timestamps (no audio needed)."""
    times = relative_times(conv)
    msgs = conv["transcript"]
    gaps = []
    for i in range(1, len(msgs)):
        if times[i] is None or times[i - 1] is None:
            continue
        gap = times[i] - times[i - 1]
        if gap >= 15 and msgs[i]["speaker"] in ("operator", "client"):
            gaps.append({"after": i - 1, "before": i, "seconds": round(gap, 1),
                         "next_speaker": msgs[i]["speaker"]})
    op_idx = [i for i, m in enumerate(msgs) if m["speaker"] == "operator"]
    words = sum(len(msgs[i]["text"].split()) for i in op_idx)
    span = None
    if op_idx and times[op_idx[0]] is not None and times[op_idx[-1]] is not None:
        span = times[op_idx[-1]] - times[op_idx[0]]
    holds = [i for i, m in enumerate(msgs) if m["speaker"] == "system"
             and re.search(r"удержани|hold|приостановлен|күту", m["text"], re.I)]
    return {
        "operator_messages": len(op_idx),
        "operator_words": words,
        "operator_active_minutes": round(span / 60, 1) if span else None,
        "operator_words_per_minute_rough": round(words / (span / 60), 0) if span and span > 30 else None,
        "long_pauses": gaps[:15],
        "hold_or_pause_events": holds,
    }


def format_transcript(conv):
    times = relative_times(conv)
    lines = []
    for i, m in enumerate(conv["transcript"]):
        lines.append("[%d] %s %s: %s" % (i, fmt_offset(times[i]), m["speaker"], m["text"]))
    return "\n".join(lines)


def format_checklist(checklist):
    parts = []
    sections = {s["id"]: s["title"] for s in checklist.get("sections") or []}
    for c in checklist["criteria"]:
        lines = ["### %s — %s%s" % (c["id"], c["title"],
                                     " (секция: %s)" % sections[c["section"]] if c.get("section") in sections else "")]
        lines.append("Тип: %s. Разрешённые ответы: %s." % (
            "шкала" if c["type"] == "scale" else ("флаг грубой ошибки" if c.get("critical") else "да/нет"),
            ", ".join(allowed_answers(c))))
        lines.append("Правило: " + c.get("description", ""))
        if c.get("allow_na") and c.get("na_condition"):
            lines.append("N/A, если: " + c["na_condition"])
        if c["type"] == "scale":
            for s in sorted(c["scale"], key=lambda s: -int(s["value"])):
                lines.append("  %s — %s" % (s["value"], s["label"]))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def build_user_prompt(conv, checklist):
    head = "Канал: %s. Язык: %s. ID: %s." % (conv.get("channel"), conv.get("language"), conv["conversation_id"])
    extra = ""
    if conv.get("channel") == "voice call":
        extra = ("\n\nМетрики речи и пауз (рассчитаны по меткам времени, для критерия ведения разговора):\n"
                 + json.dumps(call_metrics(conv), ensure_ascii=False))
    return ("%s\n\n## Чек-лист «%s»\n\n%s%s\n\n## Транскрипт\n\n%s\n\n"
            "Оцени работу оператора по каждому критерию. Ответ — JSON по схеме."
            % (head, checklist["name"], format_checklist(checklist), extra, format_transcript(conv)))


# ---------------------------------------------------------------- providers

class ProviderError(Exception):
    pass


def _extract_json(text):
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ProviderError("модель не вернула JSON")
    return json.loads(m.group(0))


def call_anthropic(system, user, settings):
    try:
        import anthropic
    except ImportError:
        raise ProviderError("не установлен пакет anthropic: выполните  python3 -m pip install --user -r requirements.txt")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ProviderError("нет ANTHROPIC_API_KEY в файле .env")
    client = anthropic.Anthropic()
    model = settings["anthropic_model"]
    output_config = {"effort": settings.get("anthropic_effort", "medium"),
                     "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
    extra_body = {"output_config": output_config}
    extra_headers = {}
    if settings.get("anthropic_fallbacks"):
        extra_body["fallbacks"] = "default"
        extra_headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
    kwargs = dict(model=model, max_tokens=16000, system=system,
                  messages=[{"role": "user", "content": user}])
    try:
        resp = client.messages.create(extra_body=extra_body, extra_headers=extra_headers, **kwargs)
    except anthropic.BadRequestError as e:
        if "fallback" in str(e).lower():
            # fallbacks not available for this account/model - retry without them
            resp = client.messages.create(extra_body={"output_config": output_config}, **kwargs)
        else:
            raise ProviderError("Claude API: %s" % e)
    except anthropic.AuthenticationError:
        raise ProviderError("Claude API: неверный ANTHROPIC_API_KEY")
    except anthropic.RateLimitError:
        raise ProviderError("Claude API: превышен лимит запросов, повторите позже")
    except anthropic.APIStatusError as e:
        raise ProviderError("Claude API: HTTP %s %s" % (e.status_code, e.message))
    except anthropic.APIConnectionError:
        raise ProviderError("Claude API: нет соединения с интернетом")

    if resp.stop_reason == "refusal":
        raise ProviderError("модель отказалась оценивать этот диалог")
    if resp.stop_reason == "max_tokens":
        raise ProviderError("ответ модели обрезан (max_tokens)")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    return _extract_json(text), usage, model


def call_ollama(system, user, settings):
    url = settings["ollama_url"].rstrip("/") + "/api/chat"
    body = {"model": settings["ollama_model"], "stream": False, "format": OUTPUT_SCHEMA,
            "options": {"temperature": 0, "num_ctx": 32768},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            data = json.loads(r.read().decode("utf-8"))
    except OSError as e:
        raise ProviderError("Ollama недоступна (%s). Запущена ли программа Ollama?" % e)
    usage = {"input_tokens": data.get("prompt_eval_count", 0), "output_tokens": data.get("eval_count", 0)}
    return _extract_json(data["message"]["content"]), usage, "ollama:" + settings["ollama_model"]


def _gemini_schema(schema):
    """Gemini's responseSchema is a JSON-schema subset: drop additionalProperties."""
    if isinstance(schema, dict):
        return {k: _gemini_schema(v) for k, v in schema.items() if k != "additionalProperties"}
    if isinstance(schema, list):
        return [_gemini_schema(x) for x in schema]
    return schema


def call_gemini(system, user, settings):
    """Google Gemini via REST (free tier works without a card). Retries on rate limits."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ProviderError("нет GEMINI_API_KEY в файле .env")
    model = settings.get("gemini_model", "gemini-2.5-pro")
    url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                             "responseSchema": _gemini_schema(OUTPUT_SCHEMA)},
    }
    last_err = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 503, 500) and attempt < 3:
                last_err = "Gemini HTTP %d, повтор через %d с" % (e.code, 15 * (attempt + 1))
                time.sleep(15 * (attempt + 1))
                continue
            if e.code == 400 and "API key" in msg:
                raise ProviderError("Gemini: неверный GEMINI_API_KEY")
            if e.code == 404:
                raise ProviderError("Gemini: модель %s не найдена, выберите другую в настройках" % model)
            raise ProviderError("Gemini HTTP %d: %s" % (e.code, msg))
        except OSError as e:
            raise ProviderError("Gemini: нет соединения (%s)" % e)
    else:
        raise ProviderError(last_err or "Gemini: не удалось получить ответ")
    cands = data.get("candidates") or []
    if not cands or not cands[0].get("content"):
        reason = (cands[0].get("finishReason") if cands else data.get("promptFeedback", {}).get("blockReason")) or "пустой ответ"
        raise ProviderError("Gemini не вернул ответ: %s" % reason)
    text = "".join(p.get("text", "") for p in cands[0]["content"].get("parts", []))
    um = data.get("usageMetadata", {})
    usage = {"input_tokens": um.get("promptTokenCount", 0), "output_tokens": um.get("candidatesTokenCount", 0)}
    return _extract_json(text), usage, "gemini:" + model


def gemini_models():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return []
    req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                                 headers={"x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except OSError:
        return []
    out = []
    for m in data.get("models", []):
        if "generateContent" in m.get("supportedGenerationMethods", []):
            out.append(m["name"].replace("models/", ""))
    return sorted(out)


def call_mock(conv, checklist):
    """Heuristic placeholder (NOT AI). Lets the UI be developed and demoed without a key."""
    msgs = conv["transcript"]
    op = [i for i, m in enumerate(msgs) if m["speaker"] == "operator"]
    first_op = op[0] if op else 0
    last_op = op[-1] if op else len(msgs) - 1
    verified = [i for i, m in enumerate(msgs) if m["speaker"] == "system"
                and re.search(r"подтвержден|расталды|verified", m["text"], re.I)]
    out = []
    for c in checklist["criteria"]:
        allowed = allowed_answers(c)
        ans, ev, expl = allowed[0], [first_op], "Демо-режим без ИИ: эвристика."
        if c["id"] == "c03":
            ans = "yes" if verified else (NA if NA in allowed else "no")
            ev = verified[:1] or [first_op]
        elif c["id"] == "c07":
            bye = re.search(r"до свидания|хорошего|всего доброго|сау болыңыз|goodbye|have a", msgs[last_op]["text"], re.I)
            ans = "yes" if bye else "no"
            ev = [last_op]
        elif c.get("critical"):
            ans = "no"
        out.append({"criterion_id": c["id"], "answer": ans, "evidence_ids": ev, "checked_from": -1,
                    "checked_to": -1, "explanation": expl, "confidence": 0.3})
    return {"criteria": out, "summary": "Демо-оценка без ИИ."}, {"input_tokens": 0, "output_tokens": 0}, "mock"


# ---------------------------------------------------------------- validation

def validate_output(raw, conv, checklist):
    """Returns (rows, errors). rows keep only well-formed criteria."""
    n = len(conv["transcript"])
    by_id = {}
    for item in raw.get("criteria") or []:
        if isinstance(item, dict) and item.get("criterion_id"):
            by_id[item["criterion_id"]] = item
    rows, errors = [], []
    for c in checklist["criteria"]:
        item = by_id.get(c["id"])
        if not item:
            errors.append("нет оценки по критерию %s" % c["id"])
            continue
        try:
            ans = normalize_answer(c, item.get("answer"))
        except ValueError as e:
            errors.append(str(e))
            continue
        ev_ids = sorted({int(i) for i in item.get("evidence_ids") or [] if isinstance(i, int) and 0 <= i < n})
        cf, ct = item.get("checked_from", -1), item.get("checked_to", -1)
        checked = None
        if isinstance(cf, int) and isinstance(ct, int) and 0 <= cf < n and 0 <= ct < n:
            checked = [min(cf, ct), max(cf, ct)]
        conf = item.get("confidence")
        conf = max(0.0, min(1.0, float(conf))) if isinstance(conf, (int, float)) else None
        rows.append({"criterion_id": c["id"], "answer": ans, "evidence_ids": ev_ids, "checked_range": checked,
                     "explanation": (item.get("explanation") or "").strip(), "confidence": conf})
    return rows, errors


def _run_provider(name, conv, checklist, settings):
    if name == "mock":
        return call_mock(conv, checklist)
    system, user = SYSTEM_PROMPT, build_user_prompt(conv, checklist)
    fn = {"anthropic": call_anthropic, "gemini": call_gemini, "ollama": call_ollama}.get(name)
    if fn is None:
        raise ProviderError("неизвестный провайдер: %s" % name)
    raw, usage, model = fn(system, user, settings)
    rows, errors = validate_output(raw, conv, checklist)
    if errors:
        # one retry with the validation errors fed back
        user2 = user + "\n\nПредыдущий ответ содержал ошибки: " + "; ".join(errors) + \
            ". Исправь их и верни полный ответ по всем критериям."
        raw2, usage2, model = fn(system, user2, settings)
        usage = {k: usage[k] + usage2[k] for k in usage}
        rows2, errors2 = validate_output(raw2, conv, checklist)
        if len(errors2) <= len(errors):
            raw = raw2
    return raw, usage, model


EXTERNAL_LABEL = {"anthropic": "Claude", "gemini": "Gemini"}


def external_provider(settings):
    return settings.get("hybrid_external") or "anthropic"


def choose_provider(conv, settings, override=None):
    p = override or settings["provider"]
    if p != "hybrid":
        return p, "выбрано в настройках"
    ext = external_provider(settings)
    label = EXTERNAL_LABEL.get(ext, ext)
    if conv.get("language") in settings.get("hybrid_claude_languages", []):
        return ext, "гибрид: язык %s → %s" % (conv.get("language"), label)
    if conv.get("channel") in settings.get("hybrid_claude_channels", []):
        return ext, "гибрид: канал «%s» → %s" % (conv.get("channel"), label)
    return "ollama", "гибрид: сначала локальная модель"


def evaluate(conversation_id, checklist_id=None, checklist_version=None, provider=None):
    settings = config.get_settings()
    conv = storage.get_conversation(conversation_id)
    if not conv:
        raise KeyError("диалог %s не найден" % conversation_id)
    if not checklist_id:
        checklist_id = settings["default_checklists"].get(conv.get("channel"), "chat")
    checklist = storage.get_checklist(checklist_id, checklist_version)
    if not checklist:
        raise KeyError("чек-лист %s не найден" % checklist_id)

    started = time.time()
    chosen, route_reason = choose_provider(conv, settings, provider)
    attempts = []
    raw = usage = model = None
    try:
        raw, usage, model = _run_provider(chosen, conv, checklist, settings)
        attempts.append({"provider": chosen, "model": model, "usage": usage})
    except ProviderError as e:
        if (provider or settings["provider"]) == "hybrid" and chosen == "ollama":
            attempts.append({"provider": chosen, "error": str(e)})
            chosen = external_provider(settings)
            route_reason = "гибрид: локальная модель недоступна → " + EXTERNAL_LABEL.get(chosen, chosen)
            raw, usage, model = _run_provider(chosen, conv, checklist, settings)
            attempts.append({"provider": chosen, "model": model, "usage": usage})
        else:
            raise

    rows, errors = validate_output(raw, conv, checklist)
    if (provider or settings["provider"]) == "hybrid" and chosen == "ollama":
        low = [r for r in rows if r["confidence"] is not None and r["confidence"] < settings["hybrid_min_confidence"]]
        if errors or low:
            ext = external_provider(settings)
            route_reason = "гибрид: локальная модель не уверена (%s) → %s" % (
                ", ".join(r["criterion_id"] for r in low) or "ошибки формата", EXTERNAL_LABEL.get(ext, ext))
            raw, usage2, model = _run_provider(ext, conv, checklist, settings)
            attempts.append({"provider": ext, "model": model, "usage": usage2})
            rows, errors = validate_output(raw, conv, checklist)

    answers = {r["criterion_id"]: r["answer"] for r in rows}
    score = compute_score(checklist, answers)
    by_row = {r["criterion_id"]: r for r in rows}
    results = []
    for c, s in zip(checklist["criteria"], score["criteria"]):
        r = by_row.get(c["id"], {"criterion_id": c["id"], "answer": None, "evidence_ids": [],
                                 "checked_range": None, "explanation": "Модель не вернула корректную оценку.",
                                 "confidence": None})
        results.append(dict(r, points=s.get("points"), max_points=s.get("max_points"),
                            weight=s["weight"], status=s["status"]))

    total_cost = 0.0
    for a in attempts:
        if a.get("usage") and a.get("model"):
            total_cost += config.cost_usd(a["model"], a["usage"]["input_tokens"], a["usage"]["output_tokens"])

    ev = {
        "id": storage.new_id("ev"),
        "conversation_id": conv["conversation_id"],
        "group_id": conv.get("group_id"),
        "channel": conv.get("channel"),
        "language": conv.get("language"),
        "checklist": checklist,
        "created_at": storage.now_iso(),
        "provider": chosen,
        "model": model,
        "routing": route_reason,
        "attempts": attempts,
        "cost_usd": round(total_cost, 5),
        "duration_sec": round(time.time() - started, 1),
        "validation_errors": errors,
        "summary": (raw or {}).get("summary", ""),
        "results": results,
        "score": score,
        "appeals": [],
    }
    storage.recompute_final(ev)
    storage.save_evaluation(ev)
    return ev
