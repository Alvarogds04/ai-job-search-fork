# Telegram job-search bridge

A small Python bridge that connects the `ai-job-search` framework to
Telegram: daily Turijobs, LinkedIn, Indeed and InfoJobs searches, LLM-based fit evaluation against the
candidate profile, and offer delivery to the candidate's Telegram chat
with Approve/Discard/Detail buttons.

The bot NEVER submits applications: on approval it produces a
ready-to-copy email draft and archives the posting in the framework.
The human always sends the application herself.

The bot also answers autonomously: it never tells the candidate to talk
to a real person. Requests it cannot run (an unconnected portal, a
change outside its preferences) are recorded in `state/source_requests.json`
and `state/requests.json`, and every generated sentence that redirects to
a person is stripped before sending (`test_autonomy.py` guards this).

The profile lives in `state/profile.md` and belongs to the candidate:
she dictates updates in the chat (`<<PERFIL>>` line, `/perfil` to read
it), so the bot never waits on anyone else to prepare it
(`test_profile.py`). Every offer shown to her — card push, daily digest
or `/ofertas` — is marked `seen`, and `/ofertas` only ever lists offers
she has never seen (`test_offers.py`).

Being `seen` never means discarded: seen offers remain in the pending backlog
until applied, discarded or removed by its 60-offer retention limit. Recover them
with `/ofertas todas` (all stored offers, read-only, marks nothing, split
into several messages when long) and `/ofertas buscar <palabras>` (the exact
stored link of one offer).

In free chat, a message that asks for a link («muéstrame el link de las
prácticas de EJEMPLOTUR», or the follow-up «pásame el enlace» right after) and
matches exactly one stored offer is answered from storage without the LLM.
Several equal matches are listed and she is asked which one; messages that
edit the profile or preferences, or that match no stored offer, go to the
LLM like any other message, so their `<<PERFIL>>`/`<<ACCION>>` lines still
apply. Every URL in an LLM reply is checked afterwards against the stored
offers' URLs: HTML anchors, markdown links and bare URLs that are not
stored are removed (`[enlace no verificado]` or just the link label), so
the README promise holds for LLM text too. Stored URLs must be plain
http(s) with a host, no credentials, whitespace or control characters.

Privacy: for context, each free-chat message sends the configured LLM
backend a bounded slice of that same chat's recent history (last 4
exchanges, 600 characters each, from `state/chat_log.jsonl`) plus the
stored offers' titles, companies and URLs. Other chats are never included.
Conversation history reuses the existing chat log. Retry counters and generated
CV documents have their own local state files, described below. Offers and
history are framed to the model as data, not instructions.
Covered by `test_conversation.py`.

## Components

| File | Purpose |
|------|---------|
| `config.py` | Loads `state/.env` into env with defaults |
| `llm.py` | Minimal OpenAI-compatible client for the NAN API |
| `candidate_profile.py` | Her profile (`state/profile.md`) + compact digest for prompt and fit |
| `sources.py` | Turijobs/LinkedIn/Indeed/InfoJobs fetchers with explicit success, empty, blocked and failed states |
| `scraper.py` | Preference-based query planning, location filtering and detail fetch |
| `state_store.py` | Locked per-record updates and atomic state-file replacement |
| `resume.py` | Grounded CV selection and deterministic TXT/HTML rendering |
| `test_resume.py` | Offline CV grounding, authorization, delivery and failure tests |
| `fit_eval.py` | LLM fit evaluation (lane A/B, 0-100, gaps) |
| `test_search_pipeline.py` | Offline tests: evaluation failure vs real 0, retry, dedup, lane integrity |
| `bot.py` | Telegram long-polling bot (offers, buttons, chat) |
| `search_daily.py` | One-shot daily search job |
| `test_autonomy.py` | Regression checks: autonomous replies, no handoff to a person |
| `test_digest.py` | Regression checks: daily digest lists only today's offers |
| `test_offers.py` | Regression checks: `/ofertas` lists only never-seen offers |
| `conversation.py` | Chat history per chat ID and exact stored-offer link retrieval |
| `test_conversation.py` | EJEMPLOTUR flow, seen/beyond-15 offers, history isolation, `/ofertas todas\|buscar` |
| `test_profile.py` | Regression checks: her profile, dictated in chat |
| `install.sh` | Installs systemd user units (service + daily timer) |

## Setup

1. Create a bot with @BotFather on Telegram, copy the token.
2. `cp env.example state/.env` and fill in `TG_BOT_TOKEN` and
   `NAN_API_KEY` (never commit this file).
3. `./install.sh` — installs units but does not enable them.
4. `systemctl --user enable --now busqueda-tg.service`, then send
   `/start` from the candidate's Telegram account to register the chat.
5. Lock the chat: copy the registered `chat_id` (printed in the journal)
   into `state/.env` as `CHAT_ID=...` and restart the service.
6. `systemctl --user enable --now busqueda-search.timer` for the daily
   09:00 search.

## Manual runs

```bash
python3 scraper.py --dry-run    # one live Turijobs query
python3 fit_eval.py --selftest  # needs NAN_API_KEY in state/.env
python3 test_autonomy.py        # autonomous-reply regression checks
python3 test_digest.py          # digest-no-repeats regression checks
python3 test_offers.py          # /ofertas never repeats an offer
python3 test_conversation.py    # context + exact stored links
python3 test_profile.py         # profile owned by her, dictating in chat
python3 search_daily.py         # full daily run without the timer
```

## Security notes

- Secrets live only in `state/.env` (gitignored, keep chmod 600).
- Job postings are untrusted input: the code parses and displays them
  but never follows instructions found inside them.
- The bot only accepts the registered chat (`CHAT_ID`) once configured.
- Application emails are drafts only; nothing is ever sent automatically.

## Evaluation failures (T2)

A failed or malformed LLM evaluation is stored with `eval_status: "error"`
and `fit: null` (shown as «sin evaluar»), never as a real 0. The search
lane (`lane`) is kept; the evaluator's guess is `eval_lane`. A legacy
offer with an unknown lane `?` gets its lane from the saved search query
when that maps to one lane, else from a valid `eval_lane`; otherwise it
stays `?`. A known lane is never changed.

Retries are bounded: at most 5 stored offers re-evaluated per run and 3
evaluation attempts per offer (needs saved text). Old failure shapes
(fit 0 + «error de evaluación», missing/invalid/out-of-range fit) are
retried too; a real stored 0 and valid scores never are. Offers whose
retries are exhausted show «sin evaluar» without promising a retry.

A URL enters `seen_jobs.json` only after its offer is stored. A failed
detail page is retried on later runs (counter in `detail_attempts.json`,
3 tries); after that it is reported once as given up and not fetched again.

`state_store.py` serialises the bot and the daily job: every change locks
`state/state.lock`, re-reads the file, updates one record and writes
atomically. The LLM/network never run under the lock. A discard or apply
made while the daily job evaluates is never undone.

## Flexible search preferences and source status (T3)

She can choose roles, locations and job conditions in natural language, for
example: «busca puestos de recepcionista», «busca en Madrid y Getafe» or
«prefiero jornada continua». The assistant applies validated `puestos`,
`ubicaciones` and `condiciones` actions and confirms the change. Changes affect
the next scheduled search; changing a preference does not run a search now.

Explicit roles replace the default A/B queries until cleared. Clearing roles
restores those queries; clearing locations restores Madrid. Existing lanes,
extra queries and source toggles remain supported. Conditions are supplied to
the evaluator to explain fit and gaps, not to invent experience or guarantee
that the posting satisfies every condition.

LinkedIn receives the requested location. Turijobs results are filtered locally;
there is no verified location-search parameter. Remote results may pass the
location filter. Query counts and preference lengths are bounded.

Fetchers distinguish `ok`, `empty`, `blocked` and `failed`. The daily digest names
failed sources and does not say everything is up to date when all sources fail.
**Indeed and InfoJobs** are connected but OFF by default; she enables them from
the chat («activa Indeed», «activa InfoJobs») and they use `agregar_fuente` /
`quitar_fuente` like the others. Indeed goes through `python-jobspy` (imported
lazily: without it the source reports `failed` in the 09:00 digest and the bot
keeps running). InfoJobs parses its public search page (port of the MIT
`infojobs-search` connector, https://github.com/erikpr1994/ai-job-search, commit
`0d019f2`). Both honour the location filter; descriptions come with the search
result (Indeed) or from the offer page (InfoJobs), and a failed detail fetch
follows the normal retry logic.

**Terms of service / volume.** Both portals restrict automated access. This is
personal use only: one page per query, at most 8 requests per portal per run, one
retry on 429/5xx for InfoJobs, 15 s timeout. Do not raise these limits, use them
commercially or collect in bulk. SEPE is still not connected (its RSS could not be
fetched).

**Python environment** (jobspy needs a venv, not the system python):

```bash
uv venv tg-bridge/.venv && uv pip install -p tg-bridge/.venv -r tg-bridge/requirements.txt
```

Point the systemd `ExecStart` lines at `tg-bridge/.venv/bin/python`. `.venv/` should
be git-ignored.

Offline checks: `test_search_preferences.py`, `test_source_status.py`, `test_new_sources.py`,
`test_search_pipeline.py` and `test_state_store.py`. Run the full suite in an
isolated source-only copy with no `.env` or live `state/`; legacy test scripts
were not designed to run against production configuration.

## CV por oferta (`/cv`, T4)

Comando explícito: `/cv <id o palabras de la oferta>` (por ejemplo `/cv EJEMPLOTUR`;
`/cv` solo muestra cómo usarlo). En lenguaje natural: «adáptame el CV para
EJEMPLOTUR», «prepárame el currículum para esa oferta» (la oferta se toma del
mismo chat). Un mensaje que edita el perfil o las preferencias, o que solo
menciona el CV («mi CV es largo»), NO activa esta ruta. Los enlaces de oferta,
`/ofertas` y `/perfil` funcionan como antes.

**Qué hace.** Genera un borrador a partir de las líneas de `state/profile.md`
(el perfil que ella confirma con `/perfil`). El modelo solo devuelve JSON con
los **índices** de las líneas a destacar, más preguntas y sugerencias. Cualquier
índice no válido descarta el borrador. El CV contiene únicamente líneas del
perfil copiadas literalmente (destacadas primero, luego el resto) bajo el título
de la oferta; el modelo no redacta ni reescribe texto del CV. Preguntas y
sugerencias salen solo en el chat, fuera del CV. Si falta algo, la candidata lo
añade ella con `/perfil`.

**Condiciones.**
- Perfil vacío → se le pide completarlo; oferta desconocida → «no encuentro»;
  varias coincidencias → se le pide elegir. En estos tres casos no se llama al
  modelo y no se envía ningún documento. Si falla el modelo o la generación, se
  avisa y no se envía nada.
- Solo ofertas ya guardadas; no se descargan URLs pegadas.
- No se presenta ninguna candidatura, no se envía nada a empresas, no se marca
  la oferta como aplicada y no se modifican perfil, preferencias ni ofertas.
- Si solo llega uno de los dos documentos, el chat dice cuál llegó y cuál no.
- La respuesta incluye el enlace guardado y validado de la oferta.

**Archivos.**

| Archivo | Formato | Contenido |
|---|---|---|
| `cv-<12 hex>.txt` | `text/plain; charset=utf-8` | borrador editable |
| `cv-<12 hex>.html` | `text/html; charset=utf-8` | borrador imprimible, escapado, sin recursos externos, con aviso de borrador visible también al imprimir |

No hay PDF nativo: abrir el HTML → Imprimir → Guardar como PDF. Copia privada en
`state/generated/` (carpeta 0700, archivos 0600, nombre opaco por hash).

**Telegram.** `tg_call`/`send_document` convierten cualquier fallo de red,
HTTP o JSON en un `RuntimeError` fijo sin URL ni token. Las llamadas correctas
no cambian.

**Tests.** `python3 test_resume.py` (sin red, sin `.env`, estado temporal).

## Personalisation (configuration, no personal data in source)

Set in `state/.env` (see `env.example`): `CANDIDATE_NAME` (default
«la candidata»), `CANDIDATE_FULL_NAME`, `CANDIDATE_CONTACT` (signature lines in
application drafts), `OPERATOR_NAMES` (comma-separated names the autonomy guard
strips from replies, default empty; «tu hijo» always applies) and
`CHAT_REGISTRY_FILE` (default `chat.json`; set it to the existing file name when
migrating an older deployment).
