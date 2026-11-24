"""Generates the narration of the demo video with ElevenLabs: one clip for each entry of `narration.json`.

    python demo/voice.py [<clip id> ...] [--force]

One voice and one setting for the whole video (`voice` of `narration.json`). The text for the voice engine
is the narration text with each term of `spoken` replaced by its spoken form. The engine gives the time of
each character of a clip: the subtitles take their times from it.

Output, in demo/footage/voice/: <id>.mp3 and <id>.json (the text, the text as sent, the settings and the
character times). A clip is generated again only if its text or the voice changed, or with `--force`.
The key comes from ELEVENLABS_API_KEY (or ELEVENLABS_API_KEY__PROD). It is never printed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "footage" / "voice"
API = "https://api.elevenlabs.io/v1/text-to-speech"


def spoken_text(text: str, spoken: dict) -> str:
    """The text as sent to the voice engine: each term in its spoken form, the longest term first."""
    for term in sorted(spoken, key=len, reverse=True):
        text = text.replace(term, spoken[term])
    return text


def generate(key: str, voice: dict, text: str, before: str, after: str) -> dict:
    body = {"text": text, "model_id": voice["model"], "seed": voice["seed"], "voice_settings": voice["settings"],
            "previous_text": before, "next_text": after}
    request = urllib.request.Request(
        f"{API}/{voice['voiceId']}/with-timestamps?output_format={voice['outputFormat']}",
        data=json.dumps(body).encode(), headers={"xi-api-key": key, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise SystemExit(f"ElevenLabs refused the request: HTTP {error.code} {error.read()[:300]!r}") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ids", nargs="*", help="clips to generate (default: each clip that changed)")
    parser.add_argument("--force", action="store_true", help="generate the named clips again")
    args = parser.parse_args()
    key = os.environ.get("ELEVENLABS_API_KEY") or os.environ.get("ELEVENLABS_API_KEY__PROD")
    if not key:
        raise SystemExit("the environment variable ELEVENLABS_API_KEY is not set: the narration is blocked")
    script = json.loads((HERE / "narration.json").read_text())
    voice, clips = script["voice"], script["clips"]
    sent = [spoken_text(clip["text"], script["spoken"]) for clip in clips]
    OUT.mkdir(parents=True, exist_ok=True)
    for index, clip in enumerate(clips):
        if args.ids and clip["id"] not in args.ids:
            continue
        signature = hashlib.sha256(json.dumps([sent[index], voice], sort_keys=True).encode()).hexdigest()
        record_path, audio_path = OUT / f"{clip['id']}.json", OUT / f"{clip['id']}.mp3"
        if not args.force and record_path.exists() and audio_path.exists() \
                and json.loads(record_path.read_text())["signature"] == signature:
            continue
        answer = generate(key, voice, sent[index], sent[index - 1] if index else "",
                          sent[index + 1] if index + 1 < len(sent) else "")
        audio_path.write_bytes(base64.b64decode(answer["audio_base64"]))
        alignment = answer["alignment"]
        record = {"id": clip["id"], "text": clip["text"], "sent": sent[index], "signature": signature,
                  "voice": voice, "seconds": alignment["character_end_times_seconds"][-1],
                  "characters": alignment["characters"],
                  "starts": alignment["character_start_times_seconds"],
                  "ends": alignment["character_end_times_seconds"]}
        record_path.write_text(json.dumps(record) + "\n")
        print(f"{clip['id']}: {record['seconds']:.2f} s  {sent[index]}", flush=True)
    total = sum(json.loads((OUT / f"{clip['id']}.json").read_text())["seconds"] for clip in clips
                if (OUT / f"{clip['id']}.json").exists())
    print(f"narration: {total:.1f} s of speech in {len(clips)} clips")
    return 0


if __name__ == "__main__":
    sys.exit(main())
