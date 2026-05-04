"""aar-tts-render — single TTS-engine renderer used by tts-drop and tts-stream.

Reads text on stdin, writes audio to ``--out``. The engine logic lives
here so we don't have to maintain per-engine code in two places (bash
tts-drop + Python tts-stream). Both callers shell out to this binary —
or in tts-stream's case, import :func:`render_text` directly.

Engines:
  edge    edge-tts (Microsoft Azure neural voices, free, no key)
          Voice via --voice (default en-US-AriaNeural)
  openai  OpenAI Speech (gpt-4o-mini-tts), needs OPENAI_API_KEY
          Voice via --voice (default marin)
  qwen    Alibaba DashScope Qwen TTS, needs DASHSCOPE_API_KEY
          Voice via --voice (default Cherry; also Jada, Dylan)

On non-edge engine failure, falls back to edge automatically (so the
caller still gets *some* audio rather than silence). Disable with
--no-fallback.

Exit codes:
  0  audio written
  1  generation failed (logged to stderr)
  2  bad usage
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional


# ---- Engine renderers -----------------------------------------------------
# Each returns (ok: bool, err: str). On failure, err carries a short
# human-readable reason that the orchestrator surfaces in fallback logs.


def _render_edge(text: str, outfile: Path, *, voice: str, edge_bin: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [edge_bin, "--text", text, "--voice", voice, "--write-media", str(outfile)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    err = proc.stderr.decode(errors="replace").strip()
    ok = proc.returncode == 0 and outfile.exists() and outfile.stat().st_size > 0
    return ok, err


def _render_openai(
    text: str,
    outfile: Path,
    *,
    voice: str,
    model: str,
    python_bin: str,
) -> tuple[bool, str]:
    """Run the openai SDK in a subprocess so the caller doesn't have to
    have the package installed in its own venv. Most users have it via
    ``pipx install openai`` (auto-discovered in :func:`_default_openai_python`).
    """
    script = (
        "import os\n"
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "with client.audio.speech.with_streaming_response.create(\n"
        "    model=os.environ['TTS_MODEL'],\n"
        "    voice=os.environ['TTS_VOICE'],\n"
        "    input=os.environ['TTS_TEXT'],\n"
        ") as r:\n"
        "    r.stream_to_file(os.environ['TTS_OUTFILE'])\n"
    )
    env = {**os.environ, "TTS_MODEL": model, "TTS_VOICE": voice,
           "TTS_TEXT": text, "TTS_OUTFILE": str(outfile)}
    proc = subprocess.run(
        [python_bin, "-c", script],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    err = proc.stderr.decode(errors="replace").strip()
    ok = proc.returncode == 0 and outfile.exists() and outfile.stat().st_size > 0
    return ok, err


def _render_qwen(
    text: str,
    outfile: Path,
    *,
    voice: str,
    model: str,
    language: str,
    base_url: str,
    api_key: str,
) -> tuple[bool, str]:
    """DashScope Qwen TTS: one POST returns JSON with an audio URL; we
    download that URL into ``outfile``. Mirrors the JS implementation in
    extensions/pi-tts-extension.ts. Output is WAV.
    """
    if not api_key:
        return False, "DASHSCOPE_API_KEY not set"
    url = f"{base_url.rstrip('/')}/services/aigc/multimodal-generation/generation"
    payload = json.dumps({
        "model": model,
        "input": {"text": text, "voice": voice, "language_type": language},
    }).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except Exception as e:  # noqa: BLE001
        return False, f"qwen http: {e}"
    try:
        data = json.loads(body)
    except ValueError as e:
        return False, f"qwen json: {e}"
    # DashScope responses vary slightly across model families; the audio
    # URL has been seen at output.audio.url, output.url, and url.
    audio_url = (
        (data.get("output") or {}).get("audio", {}).get("url")
        or (data.get("output") or {}).get("url")
        or data.get("url")
    )
    if not audio_url:
        snippet = json.dumps(data)[:200]
        return False, f"qwen response missing audio url: {snippet}"
    try:
        with urllib.request.urlopen(audio_url, timeout=60) as resp:
            outfile.write_bytes(resp.read())
    except Exception as e:  # noqa: BLE001
        return False, f"qwen download: {e}"
    if not outfile.exists() or outfile.stat().st_size == 0:
        return False, "qwen download produced empty file"
    return True, ""


# ---- Engine selection / autodetect ----------------------------------------


def _default_openai_python(current: str) -> str:
    """If ``current`` doesn't have the ``openai`` module, look for one
    that does in the usual pipx venv locations. Returns ``current``
    unchanged if no better option is found.
    """
    candidates = [current]
    pipx_root = Path(os.environ.get("PIPX_HOME",
                                    Path.home() / ".local" / "pipx"))
    candidates.append(str(pipx_root / "venvs" / "openai" / "bin" / "python3"))
    candidates.append(str(pipx_root / "venvs" / "llm" / "bin" / "python3"))
    for c in candidates:
        if not c:
            continue
        try:
            r = subprocess.run([c, "-c", "import openai"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               timeout=2)
            if r.returncode == 0:
                return c
        except (OSError, subprocess.SubprocessError):
            continue
    return current


# ---- Public API ------------------------------------------------------------


def render_text(
    text: str,
    outfile: Path,
    *,
    engine: str,
    voice: Optional[str] = None,
    edge_voice: str = "en-US-AriaNeural",
    edge_bin: str = "edge-tts",
    openai_voice: str = "marin",
    openai_model: str = "gpt-4o-mini-tts",
    openai_python: str = "python3",
    qwen_voice: str = "Cherry",
    qwen_model: str = "qwen3-tts-flash-2025-11-27",
    qwen_lang: str = "English",
    qwen_base_url: str = "https://dashscope-intl.aliyuncs.com/api/v1",
    qwen_api_key: Optional[str] = None,
    fallback_to_edge: bool = True,
    on_fallback: Optional[callable] = None,
) -> tuple[bool, str]:
    """Render ``text`` to ``outfile`` via ``engine``. Returns (ok, err).

    On non-edge engine failure with ``fallback_to_edge=True``, falls
    back to edge automatically. ``on_fallback(engine, err)`` is called
    so callers can surface the original engine's error.
    """
    if engine == "edge":
        return _render_edge(text, outfile, voice=voice or edge_voice, edge_bin=edge_bin)
    if engine == "openai":
        ok, err = _render_openai(
            text, outfile,
            voice=voice or openai_voice,
            model=openai_model,
            python_bin=openai_python,
        )
    elif engine == "qwen":
        ok, err = _render_qwen(
            text, outfile,
            voice=voice or qwen_voice,
            model=qwen_model,
            language=qwen_lang,
            base_url=qwen_base_url,
            api_key=qwen_api_key if qwen_api_key is not None
                else os.environ.get("DASHSCOPE_API_KEY", ""),
        )
    else:
        return False, f"unknown engine: {engine}"
    if not ok and fallback_to_edge:
        if on_fallback is not None:
            on_fallback(engine, err)
        return _render_edge(text, outfile, voice=edge_voice, edge_bin=edge_bin)
    return ok, err


# ---- CLI ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aar-tts-render",
        description="Render TTS audio from stdin text to a file.",
    )
    p.add_argument("--engine", required=True, choices=["edge", "openai", "qwen"])
    p.add_argument("--out", required=True, type=Path,
                   help="Output audio file path (.mp3 for edge/openai, .wav for qwen)")
    p.add_argument("--voice", default=None,
                   help="Engine-specific voice (overrides per-engine default)")
    p.add_argument("--edge-voice",
                   default=os.environ.get("RELAY_EDGE_VOICE", "en-US-AriaNeural"))
    p.add_argument("--edge-bin",
                   default=os.environ.get("RELAY_EDGE_TTS_BIN", "edge-tts"))
    p.add_argument("--openai-voice",
                   default=os.environ.get("RELAY_OPENAI_VOICE", "marin"))
    p.add_argument("--openai-model",
                   default=os.environ.get("RELAY_OPENAI_MODEL", "gpt-4o-mini-tts"))
    p.add_argument("--openai-python",
                   default=os.environ.get("RELAY_OPENAI_PYTHON", "python3"))
    p.add_argument("--qwen-voice",
                   default=os.environ.get("RELAY_QWEN_VOICE", "Cherry"))
    p.add_argument("--qwen-model",
                   default=os.environ.get("RELAY_QWEN_MODEL",
                                          "qwen3-tts-flash-2025-11-27"))
    p.add_argument("--qwen-lang",
                   default=os.environ.get("RELAY_QWEN_LANG", "English"))
    p.add_argument("--qwen-base-url",
                   default=os.environ.get("DASHSCOPE_BASE_URL",
                                          "https://dashscope-intl.aliyuncs.com/api/v1"))
    p.add_argument("--no-fallback", dest="fallback", action="store_false",
                   help="Don't fall back to edge on engine failure")
    p.set_defaults(fallback=True)
    return p


def main() -> None:
    args = _build_parser().parse_args()
    text = sys.stdin.read()
    if not text.strip():
        print("aar-tts-render: empty stdin", file=sys.stderr)
        sys.exit(2)

    if args.engine == "openai":
        args.openai_python = _default_openai_python(args.openai_python)

    args.out.parent.mkdir(parents=True, exist_ok=True)

    def _fallback_log(engine: str, err: str) -> None:
        print(f"aar-tts-render: {engine} failed ({err or 'no stderr'}); falling back to edge",
              file=sys.stderr)

    ok, err = render_text(
        text, args.out,
        engine=args.engine,
        voice=args.voice,
        edge_voice=args.edge_voice,
        edge_bin=args.edge_bin,
        openai_voice=args.openai_voice,
        openai_model=args.openai_model,
        openai_python=args.openai_python,
        qwen_voice=args.qwen_voice,
        qwen_model=args.qwen_model,
        qwen_lang=args.qwen_lang,
        qwen_base_url=args.qwen_base_url,
        fallback_to_edge=args.fallback,
        on_fallback=_fallback_log,
    )
    if not ok:
        print(f"aar-tts-render: {args.engine} failed: {err or 'unknown error'}",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
