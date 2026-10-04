"""Gnani (Vachana) MCP server — thin pass-through to the REAL Gnani STT/TTS API.

Not a mock. Every call goes to https://api.vachana.ai using the official
`gnani-vachana` SDK, so request formats are Gnani's own, not guessed.

Env vars:
  GNANI_API_KEY    (required)  your Gnani key — set on the host, never in code
  PUBLIC_BASE_URL  (required for speak) e.g. https://your-app.onrender.com
  PORT             (optional)  default 8000

Tools return {"ok": False, "error": ..., "kind": ...} instead of raising, so the
agent can branch on failures (auth, rate limit, timeout, bad audio).
"""
import base64, os, uuid, pathlib, asyncio
import httpx
from mcp.server.fastmcp import FastMCP
from starlette.responses import FileResponse, JSONResponse
from gnani.stt import GnaniSTTClient
from gnani.tts import GnaniTTSClient

API_KEY = os.environ.get("GNANI_API_KEY", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
AUDIO_DIR = pathlib.Path("/tmp/gnani_audio")
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

mcp = FastMCP(
    "gnani",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
    stateless_http=True,
)


def _err(e: Exception) -> dict:
    msg = str(e)
    low = msg.lower()
    kind = "unknown"
    if "401" in low or "missing_api_key" in low or "auth" in low:
        kind = "auth"
    elif "429" in low or "rate" in low:
        kind = "rate_limited"
    elif "timeout" in low or "timed out" in low:
        kind = "timeout"
    elif "403" in low or "1010" in low:
        kind = "blocked_by_cloudflare"
    elif "400" in low:
        kind = "bad_request"
    return {"ok": False, "kind": kind, "error": msg[:500]}


@mcp.tool()
async def gnani_transcribe(
    audio_url: str = "",
    audio_base64: str = "",
    language_code: str = "hi-IN",
    filename: str = "audio.wav",
) -> dict:
    """Speech-to-text via Gnani. Give EITHER audio_url (publicly fetchable) OR
    audio_base64. Clips up to ~60s. language_code: en-IN, hi-IN, gu-IN, ta-IN,
    kn-IN, te-IN, mr-IN, bn-IN, ml-IN, pa-IN."""
    if not API_KEY:
        return {"ok": False, "kind": "auth", "error": "GNANI_API_KEY not set on server"}
    try:
        if audio_base64:
            data = base64.b64decode(audio_base64)
        elif audio_url:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as h:
                r = await h.get(audio_url, headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                data = r.content
        else:
            return {"ok": False, "kind": "bad_request", "error": "provide audio_url or audio_base64"}
        client = GnaniSTTClient(api_key=API_KEY)
        result = await asyncio.to_thread(
            client.transcribe_bytes, data, filename, language_code
        )
        return {"ok": True, "result": result}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
async def gnani_speak(
    text: str,
    voice: str = "Nalini",
    language: str = "",
    model: str = "timbre-v2.5",
    speed: float = 1.0,
) -> dict:
    """Text-to-speech via Gnani. Returns audio_url (a WAV hosted on this server)
    you can send on WhatsApp etc. Keep IDs/dates/numbers short and explicit —
    they must not be mispronounced."""
    if not API_KEY:
        return {"ok": False, "kind": "auth", "error": "GNANI_API_KEY not set on server"}
    if not PUBLIC_BASE_URL:
        return {"ok": False, "kind": "config", "error": "PUBLIC_BASE_URL not set on server"}
    try:
        client = GnaniTTSClient(api_key=API_KEY)
        kwargs = {"voice": voice, "model": model}
        if language:
            kwargs["language"] = language
        if speed and speed != 1.0:
            kwargs["speed"] = speed
        audio = await asyncio.to_thread(client.synthesize, text, **kwargs)
        name = f"{uuid.uuid4().hex}.wav"
        (AUDIO_DIR / name).write_bytes(audio)
        return {"ok": True, "audio_url": f"{PUBLIC_BASE_URL}/audio/{name}", "bytes": len(audio)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.custom_route("/audio/{name}", methods=["GET"])
async def serve_audio(request):
    name = pathlib.Path(request.path_params["name"]).name  # block path traversal
    p = AUDIO_DIR / name
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p, media_type="audio/wav")


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    return JSONResponse({"status": "ok", "gnani_key_set": bool(API_KEY)})


# ---- Kencase tools (DigiLocker, Account Aggregator, case state, KB, OCR,
# ---- courier, slots). Lives in tools_kencase.py so the Gnani code above stays untouched.
from tools_kencase import register_kencase_tools  # noqa: E402
register_kencase_tools(mcp)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")  # MCP endpoint: /mcp
