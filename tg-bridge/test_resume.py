"""Tests: offer-specific CV drafts grounded in her confirmed profile (T4).

Offline by construction: sockets are denied BEFORE any project import, the
state dir is a temp dir, config.ENV_FILE points nowhere, the LLM and
Telegram (requests.post) are fakes. No real .env, CV or profile is read.

Run: python3 test_resume.py
"""

import socket


def _deny(*_a, **_k):
    raise OSError("network denied in tests")


socket.socket.connect = _deny  # type: ignore[method-assign]
socket.socket.connect_ex = _deny  # type: ignore[method-assign]
socket.create_connection = _deny  # type: ignore[assignment]
socket.getaddrinfo = _deny  # type: ignore[assignment]

import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

import config  # type: ignore[reportMissingImports]  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="tg-resume-test-")
os.environ["TG_BRIDGE_STATE_DIR"] = _TMP
config.ENV_FILE = Path(_TMP) / "no-such-dir" / ".env"
config.PROFILE_PATH = Path(_TMP) / "no-such-dir" / "profile-framework.md"
if config.state_path("p").parent != Path(_TMP) or config.ENV_FILE.exists():
    sys.exit("state isolation failed: refusing to run")

import bot  # type: ignore[reportMissingImports]  # noqa: E402
import llm  # type: ignore[reportMissingImports]  # noqa: E402
import requests  # noqa: E402
import resume  # type: ignore[reportMissingImports]  # noqa: E402

TOKEN = "TESTTOKEN123:SECRET"
FAILURES: list[str] = []
CHECKS = [0]
SENT: list = []
LLM_CALLS: list = []
POSTS: list = []
LLM_ANSWER = [""]
LLM_RAISE = [None]
POST_RAISE = [None]
POST_FAIL_AT = [0]  # 1-based sendDocument call that fails (0 = never)
POST_COUNT = [0]
POST_RESP = [None]
PROFILE = (
    "# Perfil de Marta Ejemplo\n\n## Actualizaciones\n"
    "- Camarera 6 años en hostelería de Madrid\n"
    "- Inglés medio, portugués nativo\n"
    "- Atención al cliente en agencia de viajes (prácticas)\n"
    "- Disponibilidad inmediata\n"
)
EJEMPLOTUR = "https://example.test/oferta/ejemplotur"


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    if ok:
        print(f"ok   - {name}")
    else:
        print(f"FAIL - {name}" + (f" :: {detail}" if detail else ""))
        FAILURES.append(name)


class FakeResp:
    def json(self):
        return {"ok": True, "result": {}}


def fake_post(url, **kwargs):
    POST_COUNT[0] += 1
    if POST_RAISE[0] or POST_COUNT[0] == POST_FAIL_AT[0]:
        raise requests.ConnectionError(f"boom for {url}")
    if POST_RESP[0] is not None:
        return POST_RESP[0](url, kwargs)
    POSTS.append((url, kwargs))
    return FakeResp()


def fake_chat(messages, model=None):
    LLM_CALLS.append(messages)
    if LLM_RAISE[0]:
        raise LLM_RAISE[0]
    return LLM_ANSWER[0]


REAL_SEND_MESSAGE = bot.send_message
bot.send_message = lambda t, c, text, kb=None: SENT.append((c, text))
bot.llm.chat = fake_chat
bot.requests.post = fake_post
bot.profile_mod.compact_digest = lambda: "Perfil sintético."


def offer(**extra) -> dict:
    o = {"id": "k1", "title": "Prácticas Comercial (Agencia de Viajes)",
         "company": "EJEMPLOTUR", "location": "Madrid", "fit": 0, "lane": "A",
         "url": EJEMPLOTUR, "seen": True, "text": "Atención a clientes, ventas, inglés."}
    o.update(extra)
    return o


def state(name: str) -> Path:
    return Path(_TMP) / name


def reset(offers=None, profile=PROFILE) -> None:
    SENT.clear(); LLM_CALLS.clear(); POSTS.clear()
    LLM_RAISE[0] = POST_RAISE[0] = POST_RESP[0] = None
    POST_FAIL_AT[0] = POST_COUNT[0] = 0
    LLM_ANSWER[0] = json.dumps({"destacar": [2, 0]})
    for name in ("offers.json", "chat_log.jsonl", "profile.md", "preferences.json",
                 "applications.json"):
        if state(name).exists():
            state(name).unlink()
    gen = state("generated")
    if gen.is_dir():
        shutil.rmtree(gen)
    elif gen.exists():
        gen.unlink()
    bot.save_offers(offers if offers is not None else [offer()])
    if profile is not None:
        state("profile.md").write_text(profile, encoding="utf-8")


def snapshot() -> dict:
    return {n: state(n).read_bytes() if state(n).exists() else None
            for n in ("profile.md", "preferences.json", "offers.json", "applications.json")}


def say(text: str, chat: int = 1) -> None:
    bot._handle_update(TOKEN, {"message": {"chat": {"id": chat}, "text": text}}, "1")


def say_nl(text: str, chat: int = 1) -> None:
    bot.conversational_reply(TOKEN, chat, text)


def docs() -> dict:
    out = {}
    for url, kw in POSTS:
        name, data, mime = kw["files"]["document"]
        out[name] = (data, mime, url, kw)
    return out


def last() -> str:
    return SENT[-1][1] if SENT else ""


def assert_no_draft(label: str) -> None:
    check(f"{label}: no document sent", not POSTS)
    check(f"{label}: no generated files", not state("generated").exists()
          or not list(state("generated").iterdir()))


def test_natural_language_known() -> None:
    reset()
    before = snapshot()
    say_nl("adaptame el CV para EJEMPLOTUR")
    d = docs()
    names = sorted(d)
    check("two documents (txt+html) sent", len(names) == 2, str(names))
    check("opaque filenames", all(re.fullmatch(r"cv-[0-9a-f]{12}\.(txt|html)", n) for n in names), str(names))
    txt = next(v for n, v in d.items() if n.endswith(".txt"))
    htm = next(v for n, v in d.items() if n.endswith(".html"))
    check("exact mimes", txt[1] == "text/plain; charset=utf-8" and htm[1] == "text/html; charset=utf-8", f"{txt[1]} {htm[1]}")
    body = txt[0].decode("utf-8")
    check("sendDocument endpoint, token only in URL", all(v[2].endswith("/sendDocument") for v in d.values()))
    check("bounded timeout", all(0 < v[3].get("timeout", 0) <= 60 for v in d.values()))
    check("chat id sent as data", all(str(v[3]["data"]["chat_id"]) == "1" for v in d.values()))
    check("source facts preserved exactly", "Camarera 6 años en hostelería de Madrid" in body
          and "Inglés medio, portugués nativo" in body and "Disponibilidad inmediata" in body, body)
    check("highlighted facts come first in model order",
          body.index("Atención al cliente en agencia") < body.index("Camarera 6 años")
          < body.index("Disponibilidad inmediata"), body)
    check("draft is marked for review", "BORRADOR" in body)
    check("names offer", "EJEMPLOTUR" in body)
    check("html is the same facts", "Inglés medio, portugués nativo" in htm[0].decode())
    msg = last()
    check("chat warns to review, no claim of sending/PDF",
          "revís" in msg.lower() and "ninguna empresa" in msg and "ninguna candidatura" in msg
          and "No he enviado nada a nadie" not in msg and "no genero PDF nativo" in msg
          and "Guardar como PDF" in msg, msg)
    check("success reply carries the validated stored link (N2)", EJEMPLOTUR in msg, msg)
    check("printed HTML keeps the draft warning (N1)",
          "BORRADOR" in htm[0].decode() and "display:none" not in htm[0].decode()
          and "@media print" not in htm[0].decode())
    check("exactly one LLM call", len(LLM_CALLS) == 1)
    check("files private under state/generated", all(p.parent == state("generated") for p in state("generated").iterdir()))
    check("base state untouched", snapshot() == before)
    check("exchange logged for follow-ups", "EJEMPLOTUR" in state("chat_log.jsonl").read_text(encoding="utf-8"))


def test_variants_and_explicit() -> None:
    for text, label in (("preparame el curriculum para EJEMPLOTUR", "curriculum phrase"),
                        ("/cv EJEMPLOTUR", "/cv words"), ("/cv k1", "/cv id"),
                        ("/cv@bot EJEMPLOTUR", "/cv@bot")):
        reset([offer()] + [{"id": f"f{i}", "title": f"Filler {i}", "fit": 90, "url": f"https://example.test/f{i}"} for i in range(30)])
        say(text)
        check(f"{label}: draft delivered", len(POSTS) == 2, last())
    reset([{"id": f"f{i}", "title": f"Filler {i}", "fit": 90, "url": f"https://example.test/f{i}"} for i in range(30)] + [offer(fit=0)])
    say("/cv EJEMPLOTUR")
    check("offer beyond first 15 found", len(POSTS) == 2, last())
    reset()
    say("/cv")
    check("/cv without arg shows usage, no provider", not LLM_CALLS and "/cv" in last() and not POSTS, last())
    reset()
    say("/cv   ")
    check("/cv blank shows usage", not LLM_CALLS and not POSTS)
    reset()
    say("/cvfoo EJEMPLOTUR")
    check("/cvfoo is not /cv", not POSTS)


def test_resolution_failures() -> None:
    reset([offer(), offer(id="k2", company="OTRA", url="https://example.test/otra")])
    say_nl("adaptame el CV para las practicas comercial viajes")
    check("ambiguous asks", "varias" in last().lower() and "EJEMPLOTUR" in last() and "OTRA" in last(), last())
    assert_no_draft("ambiguous"); check("ambiguous: no provider", not LLM_CALLS)
    reset()
    say_nl("adaptame el CV para la oferta de panadera nocturna")
    check("unknown offer honest", "No encuentro" in last(), last())
    assert_no_draft("unknown"); check("unknown: no provider", not LLM_CALLS)
    say("/cv panadera nocturna")
    check("/cv unknown honest, no provider", "No encuentro" in last() and not LLM_CALLS and not POSTS)
    reset(profile=None)
    say_nl("adaptame el CV para EJEMPLOTUR")
    check("empty profile asks /perfil", "/perfil" in last(), last())
    assert_no_draft("empty profile"); check("empty profile: no provider", not LLM_CALLS)
    reset(profile="# Perfil de Marta\n\n## Actualizaciones\n")
    say("/cv EJEMPLOTUR")
    check("headings-only profile counts as empty", "/perfil" in last() and not LLM_CALLS)


def test_followup_pronoun() -> None:
    reset([offer(), {"id": "o", "title": "Camarera de sala", "fit": 80, "url": "https://example.test/o"}])
    LLM_ANSWER[0] = "Es una oferta de prácticas."
    say_nl("Háblame de las prácticas comercial EJEMPLOTUR")
    LLM_CALLS.clear()
    LLM_ANSWER[0] = json.dumps({"destacar": [0]})
    say_nl("preparame el curriculum para esa oferta")
    check("pronoun follow-up resolves from same chat", len(POSTS) == 2 and len(LLM_CALLS) == 1, last())
    SENT.clear(); POSTS.clear(); LLM_CALLS.clear()
    say_nl("preparame el curriculum para esa oferta", chat=2)
    check("other chat gets no foreign offer", not POSTS and not LLM_CALLS, last())


def test_grounding() -> None:
    bad = [
        ('{"destacar":[0,99]}', "out-of-range index"), ('{"destacar":["0"]}', "string index"),
        ('{"destacar":[true]}', "bool index"), ('{"destacar":[1,1]}', "duplicate"),
        ('{"destacar":[-1]}', "negative"), ('{"destacar":[]}', "empty"),
        ('{"destacar":[0.5]}', "float index"),
        ("Trabajó en Google como CEO", "free prose"), ('{"cv":"Trabajó en Google"}', "no indices"),
        ('{"destacar":[0],"preguntas":"x"}', "bad questions type"),
        ('{"destacar":[0],"sugerencias":[1]}', "bad suggestions type"),
    ]
    for answer, label in bad:
        reset(); before = snapshot()
        LLM_ANSWER[0] = answer
        say("/cv EJEMPLOTUR")
        check(f"hallucination rejected: {label}", not POSTS and "no pude" in last().lower(), last())
        check(f"{label}: nothing written", snapshot() == before and not state("generated").exists())
    reset()
    LLM_ANSWER[0] = json.dumps({
        "destacar": [1], "experiencia": "Directora en Google 2010-2020",
        "cv_texto": "Hablo alemán C2", "preguntas": ["¿Tienes carnet de conducir?"],
        "sugerencias": ["Añade un curso de ventas <b>si</b> lo has hecho"]})
    say("/cv EJEMPLOTUR")
    allbytes = b"".join(v[0] for v in docs().values()).decode()
    check("extra model keys never reach the documents",
          "Google" not in allbytes and "alemán" not in allbytes and "carnet" not in allbytes and "curso" not in allbytes)
    check("questions and suggestions only in chat, escaped, as suggestions",
          "¿Tienes carnet de conducir?" in last() and "&lt;b&gt;si" in last() and "<b>si" not in last()
          and "sugerenc" in last().lower(), last())
    check("original facts all present", all(f in allbytes for f in (
        "Camarera 6 años", "Inglés medio", "Atención al cliente", "Disponibilidad inmediata")))
    reset()
    LLM_ANSWER[0] = '```json\n{"destacar":[0]}\n```'
    say("/cv EJEMPLOTUR")
    check("fenced JSON accepted", len(POSTS) == 2, last())


def test_hostile() -> None:
    reset([offer(id="../../etc/passwd", title='<script>alert(1)</script> "x"',
                 company='"><img src=x onerror=1>', location="<i>Madrid</i>")],
          profile="# Perfil de <b>Tati</b>\n- Camarera <script>x()</script> & más\n- ../../etc/passwd lo sé\n")
    LLM_ANSWER[0] = json.dumps({"destacar": [0], "preguntas": ["<script>q</script>"]})
    say("/cv script alert")
    d = docs()
    check("hostile offer still produces draft", len(d) == 2, last())
    htm = next(v for n, v in d.items() if n.endswith(".html"))[0].decode()
    check("no raw markup in HTML", "<script" not in htm and "<img" not in htm and "<b>Tati" not in htm and "<i>Madrid" not in htm, htm)
    check("escaped content present", "&lt;script&gt;" in htm)
    check("no external resources", not re.search(r"<(img|link|script|iframe|a|object|embed|form)\b|https?://|@import|url\(", htm), htm)
    check("CSP present", "Content-Security-Policy" in htm)
    check("hostile id/title never in filename", all(re.fullmatch(r"cv-[0-9a-f]{12}\.(txt|html)", n) for n in d))
    check("chat message escaped", "<script" not in last() and "<img" not in last(), last())
    gen = state("generated")
    check("only generated/ written, with no traversal",
          sorted(p.name for p in gen.iterdir()) == sorted(d) and not Path("/tmp/etc").exists())
    check("dir private", (gen.stat().st_mode & 0o077) == 0)


def test_failures() -> None:
    reset(); before = snapshot()
    LLM_RAISE[0] = llm.LLMError("fallo con clave")
    say("/cv EJEMPLOTUR")
    check("model failure: honest, nothing sent", "no pude" in last().lower() and not POSTS and "enviado" not in last().replace("No he enviado", ""), last())
    check("model failure: state intact", snapshot() == before)
    reset(); before = snapshot()
    LLM_RAISE[0] = RuntimeError("weird")
    say("/cv EJEMPLOTUR")
    check("unexpected model error handled", "no pude" in last().lower() and not POSTS)
    reset(); before = snapshot()
    POST_RAISE[0] = True
    err = []
    old_err = sys.stderr
    sys.stderr = type("S", (), {"write": lambda s, x: err.append(x), "flush": lambda s: None})()
    try:
        say("/cv EJEMPLOTUR")
    finally:
        sys.stderr = old_err
    check("first-send failure: no delivery claim", "no he podido enviar" in last().lower()
          and "Te he enviado" not in last() and "adjunt" not in last().lower(), last())
    check("send failure never leaks token", TOKEN not in last() and TOKEN not in "".join(err) and "SECRET" not in "".join(err))
    check("send failure: state intact", snapshot() == before)
    for fail_at, sent_fmt, missing_fmt in ((2, "TXT", "HTML"),):
        reset(); before = snapshot()
        POST_FAIL_AT[0] = fail_at
        say("/cv EJEMPLOTUR")
        msg = last()
        check("second-send failure: exactly the first doc went out", len(POSTS) == 1
              and next(iter(docs())).endswith(".txt"), str(list(docs())))
        check("partial: says TXT delivered, HTML not", "TXT" in msg and "HTML" in msg
              and "no he podido enviar" in msg.lower() and "no llegó el HTML" in msg, msg)
        check("partial never says nothing arrived or both arrived",
              "no llegó ningún" not in msg and "TXT editable y HTML" not in msg
              and "ninguna empresa" in msg, msg)
        check("partial: state intact", snapshot() == before)
    reset(); before = snapshot()
    POST_RESP[0] = lambda url, kw: type("R", (), {"json": lambda s: {"ok": False, "description": "x"}})()
    say("/cv EJEMPLOTUR")
    check("telegram ok:false counts as undelivered", "no he podido enviar" in last().lower()
          and "Te he enviado" not in last(), last())
    reset(); before = snapshot()
    state("generated").write_text("im a file", encoding="utf-8")
    say("/cv EJEMPLOTUR")
    check("render/write failure: nothing sent, honest", not POSTS and "no pude" in last().lower(), last())
    check("write failure: state intact", snapshot() == before)
    reset()
    bot.save_offers([offer()])
    state("chat.json").write_text('{"chat_id": 1}')
    try:
        bot._handle_update(TOKEN, {"message": {"chat": {"id": 999}, "text": "/cv EJEMPLOTUR"}}, None)
        check("unregistered chat gets no CV", not POSTS and not LLM_CALLS)
    finally:
        state("chat.json").unlink()


def test_cv_followup_and_unauthorized() -> None:
    reset([offer(), {"id": "o", "title": "Camarera de sala", "fit": 80, "url": "https://example.test/o"}])
    say("/cv EJEMPLOTUR")
    check("first CV delivered", len(POSTS) == 2)
    POSTS.clear(); LLM_CALLS.clear()
    say_nl("vuelve a prepararme el curriculum para esa oferta")
    check("CV follow-up after a CV resolves same offer in same chat", len(POSTS) == 2
          and len(LLM_CALLS) == 1 and EJEMPLOTUR in last(), last())
    POSTS.clear(); LLM_CALLS.clear(); SENT.clear()
    say_nl("vuelve a prepararme el curriculum para esa oferta", chat=2)
    check("CV follow-up in another chat gets nothing", not POSTS and not LLM_CALLS
          and EJEMPLOTUR not in last(), last())
    # unauthorized chats, both lock modes
    reset(); before = snapshot(); n_log = state("chat_log.jsonl").exists()
    for locked, registered in (("1", None), (None, 1)):
        if registered:
            state("chat.json").write_text('{"chat_id": 1}')
        for text in ("/cv EJEMPLOTUR", "adaptame el CV para EJEMPLOTUR"):
            bot._handle_update(TOKEN, {"message": {"chat": {"id": 999}, "text": text}}, locked)
        if registered:
            state("chat.json").unlink()
    check("unauthorized CV requests: no provider, document, message, files or state",
          not LLM_CALLS and not POSTS and not SENT and snapshot() == before
          and not state("generated").exists() and state("chat_log.jsonl").exists() == n_log,
          f"{len(LLM_CALLS)} {len(POSTS)} {len(SENT)}")


def test_telegram_boundary() -> None:
    secret = "999111:LEAKY-SECRET-TOKEN"
    real_post = bot.requests.post
    out, err = [], []

    def raiser(exc):
        def post(url, **kw):
            raise exc
        return post

    class BadJson:
        def json(self):
            raise ValueError(f"bad json from {secret}")

    class Http:
        def raise_for_status(self):
            raise requests.HTTPError(f"500 for url: https://api.telegram.org/bot{secret}/x")
        json = BadJson.json

    cases = {
        "connection": raiser(requests.ConnectionError(f"https://api.telegram.org/bot{secret}/sendMessage")),
        "timeout": raiser(requests.Timeout(f"read timeout bot{secret}")),
        "bad json": lambda url, **kw: BadJson(),
        "http error": lambda url, **kw: Http(),
        "non-dict json": lambda url, **kw: type("R", (), {"json": lambda s: [secret]})(),
    }
    try:
        for label, post in cases.items():
            bot.requests.post = post
            for call in (lambda: bot.tg_call(secret, "getUpdates", {}),
                         lambda: REAL_SEND_MESSAGE(secret, 1, "hola"),
                         lambda: bot.send_document(secret, 1, "a.txt", b"x", "text/plain")):
                try:
                    call()
                    check(f"{label}: raises", False)
                except RuntimeError as exc:
                    txt = f"{exc} {exc!r} {exc.__cause__!r} {exc.__context__!r}"
                    check(f"{label}: RuntimeError without token", secret not in txt
                          and "LEAKY" not in txt, txt)
                    check(f"{label}: cause suppressed", exc.__cause__ is None and exc.__context__ is None)
                except Exception as exc:  # noqa: BLE001
                    check(f"{label}: wrong exception type {type(exc).__name__}", False)
        # main loop prints exceptions: must stay clean end to end
        bot.requests.post = raiser(requests.ConnectionError(f"https://x/bot{secret}/getUpdates"))
        old_out, old_err = sys.stdout, sys.stderr
        sink = lambda buf: type("S", (), {"write": lambda s, x: buf.append(x), "flush": lambda s: None})()  # noqa: E731
        sys.stdout, sys.stderr = sink(out), sink(err)
        calls = [0]
        real_sleep = bot.time.sleep

        def stop(_s):
            calls[0] += 1
            raise KeyboardInterrupt
        bot.time.sleep = stop
        os.environ["TG_BOT_TOKEN"] = secret
        try:
            bot.main()
        except KeyboardInterrupt:
            pass
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            bot.time.sleep = real_sleep
            os.environ.pop("TG_BOT_TOKEN", None)
        check("main poll error output has no token", calls[0] == 1 and out and err
              and secret not in "".join(out + err), "".join(out + err))
        # update failure path in the user chat
        bot.requests.post = raiser(requests.ConnectionError(secret))
        reset()
        try:
            REAL_SEND_MESSAGE(secret, 1, "x")
        except RuntimeError as exc:
            check("send_message failure message is safe", secret not in str(exc))
        # successful calls unchanged
        sent = []
        bot.requests.post = lambda url, **kw: (sent.append((url, kw)),
                                               type("R", (), {"json": lambda s: {"ok": True, "result": {"id": 7}}})())[1]
        res = bot.tg_call("TKN", "getUpdates", {"offset": 3}, timeout=12)
        check("tg_call success returns result and keeps request shape",
              res == {"id": 7} and sent[0][0] == "https://api.telegram.org/botTKN/getUpdates"
              and sent[0][1] == {"json": {"offset": 3}, "timeout": 12}, str(sent))
        sent.clear()
        REAL_SEND_MESSAGE("TKN", 5, "hola", [[("A", "a:1")]])
        check("sendMessage success payload unchanged", sent[0][1]["json"] == {
            "chat_id": 5, "text": "hola", "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [[{"text": "A", "callback_data": "a:1"}]]}}
              and sent[0][1]["timeout"] == 65, str(sent))
        bot.requests.post = lambda url, **kw: type(
            "R", (), {"json": lambda s: {"ok": False, "description": "chat not found"}})()
        try:
            bot.tg_call("TKN", "sendMessage", {})
            check("ok:false still raises", False)
        except RuntimeError as exc:
            check("ok:false keeps Telegram description", str(exc) == "Telegram sendMessage: chat not found", str(exc))
    finally:
        bot.requests.post = real_post


def test_original_intents_unchanged() -> None:
    reset()
    LLM_ANSWER[0] = 'Anotado.\n<<PERFIL>>{"texto":"mi CV antiguo dice inglés alto, corregir a medio"}'
    say_nl("Actualiza mi perfil: mi CV dice inglés alto pero es medio")
    check("profile edit mentioning CV goes to LLM, not CV route", len(LLM_CALLS) == 1 and not POSTS)
    check("profile note applied", "inglés alto" in bot.profile_mod.her_profile_text())
    for text in ("he cambiado mi CV, ahora tiene más experiencia", "sube el umbral a 80, no hagas CV hoy",
                 "mi CV es largo", "quiero ver mi cv actual"):
        reset(); LLM_ANSWER[0] = "ok"
        say_nl(text)
        check(f"not swallowed: {text[:30]}", len(LLM_CALLS) == 1 and not POSTS, last())
    reset()
    say_nl("Muéstrame el link de EJEMPLOTUR para postularme")
    check("link request unchanged", EJEMPLOTUR in last() and not LLM_CALLS and not POSTS)
    say("/ofertas buscar EJEMPLOTUR")
    check("/ofertas buscar unchanged", EJEMPLOTUR in last())
    reset(); LLM_ANSWER[0] = "tu perfil"
    say("/perfil")
    check("/perfil still goes where it did", len(LLM_CALLS) == 1 and not POSTS)
    check("welcome documents /cv", "/cv" in bot.WELCOME)
    check("prompt tells the model about /cv", "/cv" in bot._build_system_prompt("x", []))


def test_unit_render() -> None:
    facts = resume.profile_facts(PROFILE)
    check("facts exclude headings", all(not f.startswith("#") and "Actualizaciones" not in f for f in facts) and len(facts) == 4, str(facts))
    check("empty profile has no facts", resume.profile_facts("# T\n\n## x\n") == [])
    try:
        resume.parse_selection('{"destacar":[3]}', 3)
        check("parse rejects index == len", False)
    except resume.ResumeError:
        check("parse rejects index == len", True)


def main() -> int:
    try:
        for t in (test_natural_language_known, test_variants_and_explicit, test_resolution_failures,
                  test_followup_pronoun, test_grounding, test_hostile, test_failures, test_cv_followup_and_unauthorized, test_telegram_boundary,
                  test_original_intents_unchanged, test_unit_render):
            t()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{CHECKS[0]} checks run, {len(FAILURES)} failed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
