"""Conversation context and exact stored-offer retrieval.

Two jobs, both deterministic and free of network/LLM calls:

* `recent_history(chat_id)`: the last few exchanges of ONE chat, read from
  `chat_log.jsonl`, bounded in count and size, so the model sees real
  context instead of only the latest message.
* `find_offers` / `link_reply`: match what she wrote against the stored
  offers (seen or not, any position) and answer with the URL exactly as
  stored. The URL never passes through the model, so it cannot be invented.

Stored offers and logged text come from scraped postings and earlier
model output: they are data. They are sanitized here (control lines,
machine prefixes, HTML) and never treated as instructions.
"""

import html
import json
import re
import unicodedata
from urllib.parse import urlsplit

import config  # type: ignore[reportMissingImports]  # flat sibling module
import fit_eval  # type: ignore[reportMissingImports]  # flat sibling module

CHAT_LOG = "chat_log.jsonl"
MAX_HISTORY_TURNS = 4  # exchanges (user + reply) replayed to the model
MAX_HISTORY_CHARS = 600  # per replayed message
LOG_TAIL_BYTES = 64 * 1024  # never read the whole log
MAX_CONTEXT_OFFERS = 5  # relevant offers added to the prompt
MAX_CANDIDATES = 5  # offers named when the match is ambiguous

# Words that carry no offer identity. Link-request words are included so
# "muéstrame el link para postularme" does not count as offer content.
_STOPWORDS = frozenset(
    [
    "a", "al", "algo", "ante", "aplicar", "aplico", "apuntar", "apunto",
    "candidatura", "como", "con", "contra", "cual", "cuales", "cuando",
    "dame", "de", "del", "desde", "dime", "donde", "el", "ella", "en",
    "enlace", "entre", "era", "es", "esa", "ese", "eso", "esta", "este",
    "esto", "estoy", "hay", "interesa", "interesada", "interesado", "la",
    "las", "le", "link", "lo", "los", "mas", "me", "mi", "mis", "mostrar",
    "mucho", "muestra", "muestrame", "muy", "necesito", "no", "nos", "o",
    "oferta", "ofertas", "para", "pasame", "pero", "podrias", "por",
    "postular", "postularme", "postulo", "puedes", "puesto", "que", "quiero",
    "quisiera", "se", "si", "sin", "sobre", "su", "sus", "te", "tengo",
    "trabajo", "tu", "tus", "un", "una", "unas", "uno", "unos", "url", "ver",
    "y", "ya",
    ]
)
_LINK_NOUN = re.compile(r"\b(link|links|enlace|enlaces|url|urls)\b")
# Profile / preference / account edits: never a link request, even if the
# message also mentions a link ("mi enlace de LinkedIn cambió").
_NOT_A_REQUEST = re.compile(
    r"\b(perfil|umbral|carril\w*|preferenc\w*|actualiz\w*|cambi\w*|"
    r"desactiv\w*|activ[ao]\w*|sub[eo]|subir|baj[ae]|bajar|anota\w*|"
    r"borra\w*|quita\w*|agrega\w*|anade\w*)\b"
)
PLACEHOLDER = "[enlace no verificado]"
MAX_URL_LEN = 2048
_HOST = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


def _deaccent(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _tokens(text: str) -> set[str]:
    """Distinctive lowercase words (no accents, no plural 's', no stopwords)."""
    words = re.findall(r"[a-z0-9]+", _deaccent(text))
    out = set()
    for word in words:
        stem = word[:-1] if len(word) > 4 and word.endswith("s") else word
        if word in _STOPWORDS or stem in _STOPWORDS or len(word) < 3:
            continue
        out.add(stem)
    return out


def sanitize(value, limit: int = 200) -> str:
    """One-line, bounded, machine-prefix-free text for use inside a prompt."""
    text = " ".join(str(value or "").split())
    text = text.replace("<<", "‹‹").replace(">>", "››")
    return text[:limit]


def wants_link(text: str) -> bool:
    """True for a request for a link; false for profile/preference edits."""
    flat = _deaccent(text)
    return bool(_LINK_NOUN.search(flat)) and not _NOT_A_REQUEST.search(flat)


def valid_url(url) -> bool:
    """Strict http(s) check: host required, no credentials, no whitespace,
    control or markup-breaking characters, sane port and length."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LEN:
        return False
    if any(
        ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or ch in "<>\"'`\\"
        for ch in url
    ):
        return False
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError when out of range / malformed
    except ValueError:
        return False
    host = parts.hostname or ""
    return (
        parts.scheme in ("http", "https")
        and "@" not in parts.netloc
        and bool(_HOST.match(host))
    )


def safe_url(offer: dict) -> str | None:
    """The stored URL exactly as saved, only when it passes `valid_url`."""
    url = offer.get("url")
    return url if valid_url(url) else None


def allowed_urls(offers: list) -> set:
    """Every valid stored URL: the only links a reply may carry."""
    return {
        url
        for o in offers
        if isinstance(o, dict) and (url := safe_url(o)) is not None
    }


def _plain_urls(text: str, allowed: set) -> str:
    def swap(m: re.Match) -> str:
        raw = m.group(0)
        core = raw.rstrip(".,;:!?)")
        if html.unescape(core) in allowed:
            return raw
        return PLACEHOLDER + raw[len(core):]

    return _BARE.sub(swap, text)


_ANCHOR = (
    r"<a\b[^>]*?\bhref\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))[^>]*>"
    r"(.*?)</a\s*>"
)
_LONE_ANCHOR = r"<a\b[^>]*>"
_MARKDOWN = r"\[([^\]\n]*)\]\(\s*([^)\s]*)[^)]*\)"
_BARE = re.compile(r"(?i)(?:https?://|www\.)[^\s<>\"'\[\]]+")
_LINKS = re.compile(
    f"(?P<a>{_ANCHOR})|(?P<lone>{_LONE_ANCHOR})|(?P<md>{_MARKDOWN})",
    re.I | re.S,
)


def neutralize_urls(text: str, allowed: set) -> str:
    """Drop every link in `text` whose target is not in `allowed`.

    Covers HTML anchors, markdown links and bare URLs (http, https, www).
    A rejected anchor/markdown link keeps its visible label; a rejected
    bare URL becomes PLACEHOLDER. Allowed links and the rest of the
    markup are returned untouched.
    """
    def swap(m: re.Match) -> str:
        if m.group("a") is not None:
            href = next(g for g in m.group(2, 3, 4) if g is not None)
            label = _plain_urls(m.group(5), allowed)
            if html.unescape(href) in allowed:
                whole = m.group(0)
                start = m.start(5) - m.start(0)
                return whole[:start] + label + whole[start + len(m.group(5)):]
            return label or PLACEHOLDER
        if m.group("lone") is not None:
            tag = m.group(0)
            href = re.search(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+))""", tag, re.I)
            value = next((g for g in href.groups() if g), "") if href else ""
            return tag if html.unescape(value) in allowed else ""
        label, target = m.group(8), m.group(9)
        if html.unescape(target) in allowed:
            return m.group(0)
        return _plain_urls(label, allowed) or PLACEHOLDER

    return _plain_urls_outside(_LINKS, text, swap, allowed)


def _plain_urls_outside(pattern, text: str, swap, allowed: set) -> str:
    """Apply `swap` to links, and the bare-URL rule only between them."""
    out, pos = [], 0
    for m in pattern.finditer(text):
        out.append(_plain_urls(text[pos:m.start()], allowed))
        out.append(swap(m))
        pos = m.end()
    out.append(_plain_urls(text[pos:], allowed))
    return "".join(out)


def find_offers(offers: list, query: str) -> list:
    """Stored offers that best match `query`, seen or not, at any position.

    A match needs at least two shared distinctive words, or all of them
    when the query has fewer than two. Returns every offer tied for the
    best score (distinct URLs only), so the caller can detect ambiguity.
    """
    wanted = _tokens(query)
    if not wanted:
        return []
    need = min(2, len(wanted))
    best, scored = 0, []
    for offer in offers:
        if not isinstance(offer, dict) or not offer.get("title"):
            continue
        have = _tokens(
            f"{offer.get('title', '')} {offer.get('company', '')} "
            f"{offer.get('location', '')}"
        )
        score = len(wanted & have)
        if score >= need:
            scored.append((score, offer))
            best = max(best, score)
    seen_urls: set = set()
    result = []
    for index, (score, offer) in enumerate(scored):
        # Same link -> same offer; offers with no link or id stay distinct.
        key = safe_url(offer) or offer.get("id") or ("row", index)
        if score == best and key not in seen_urls:
            seen_urls.add(key)
            result.append(offer)
    return result


def describe(offer: dict) -> str:
    """HTML-safe one-line description: title and company."""
    title = html.escape(sanitize(offer.get("title"), 120))
    company = sanitize(offer.get("company"), 60)
    return f"{title} — {html.escape(company)}" if company else title


def resolve_target(offers: list, text: str, history: list) -> list:
    """Offers she means: from her message, else from the latest matching
    turn of THIS chat's history (follow-ups like «pásame el enlace»)."""
    if _tokens(text):
        return find_offers(offers, text)
    for message in reversed(history):
        found = find_offers(offers, message.get("content", ""))
        if found:
            return found
    return []


def link_reply(offers: list, query: str) -> str:
    """Answer to «/ofertas buscar X»: exact stored link, or an honest miss."""
    if not _tokens(query):
        return (
            "¿De qué oferta quieres el enlace? Dime el puesto o la empresa, "
            "o escribe /ofertas todas para ver todas las guardadas."
        )
    return reply_for_matches(find_offers(offers, query))


def reply_for_matches(found: list) -> str:
    """Grounded answer for matched offers. HTML for Telegram.

    The URL comes straight from storage; with no match or several equal
    matches nothing is guessed and she is asked or told so.
    """
    if not found:
        return (
            "No encuentro esa oferta entre las guardadas, así que no te "
            "invento ningún enlace. Escribe /ofertas todas para ver todo "
            "lo que tengo."
        )
    if len(found) > 1:
        names = "\n".join(f"• {describe(o)}" for o in found[:MAX_CANDIDATES])
        return (
            "Hay varias ofertas que encajan; dime cuál quieres:\n" + names
        )
    offer = found[0]
    url = safe_url(offer)
    if url is None:
        return (
            f"Tengo la oferta {describe(offer)}, pero no guardé un enlace "
            "válido para ella."
        )
    return (
        f"Aquí tienes el enlace de {describe(offer)}:\n{html.escape(url)}\n\n"
        "Yo no envío candidaturas: postúlate tú desde ese enlace, o toca "
        "«Aplicar» en la tarjeta y te preparo el email."
    )


def offers_context(offers: list, user_text: str, history: list | None = None) -> str:
    """Prompt block: top offers by fit plus those relevant to what she wrote.

    Each line carries the stored URL so the model can only repeat a real
    one. Titles are scraped text: sanitized and framed as data.
    """
    top = sorted(
        (o for o in offers if isinstance(o, dict) and o.get("title")),
        key=fit_eval.sort_key,
    )[:15]
    relevant = resolve_target(offers, user_text, history or [])[:MAX_CONTEXT_OFFERS]
    chosen = list(top)
    chosen += [o for o in relevant if o not in top]
    if not chosen:
        return "(no hay ofertas guardadas)"
    lines = []
    for o in chosen:
        status = "vista" if o.get("seen") else "nueva"
        lines.append(
            f"- [{fit_eval.fit_label(o)}] {sanitize(o.get('title'), 120)} · "
            f"{sanitize(o.get('company'), 60) or 'empresa sin especificar'} · "
            f"{sanitize(o.get('location'), 60)} · {status} · "
            f"URL: {safe_url(o) or 'sin enlace guardado'}"
        )
    return "\n".join(lines)


def recent_history(chat_id) -> list:
    """Last exchanges of this chat only, as chat messages, bounded.

    Reads just the tail of the log, keeps entries whose `chat_id` equals
    this chat, replays the newest MAX_HISTORY_TURNS, caps each message and
    neutralises machine-line prefixes. Any read error yields no history.
    """
    try:
        path = config.state_path(CHAT_LOG)
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - LOG_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return []
    entries = []
    for line in tail.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # includes the partial first line of the tail
        if isinstance(entry, dict) and str(entry.get("chat_id")) == str(chat_id):
            entries.append(entry)
    messages = []
    for entry in entries[-MAX_HISTORY_TURNS:]:
        user, reply = entry.get("user"), entry.get("reply")
        if isinstance(user, str) and isinstance(reply, str) and user and reply:
            messages.append(
                {"role": "user", "content": sanitize(user, MAX_HISTORY_CHARS)}
            )
            messages.append(
                {"role": "assistant", "content": sanitize(reply, MAX_HISTORY_CHARS)}
            )
    return messages
