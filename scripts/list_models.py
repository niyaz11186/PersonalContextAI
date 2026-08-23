"""List the Gemini models this API key can actually use.

ADR-002 requires model identifiers to be verified against a live call rather than
copied from documentation, because model names change frequently and a stale
identifier fails at runtime with an unhelpful error.

Run:  .\\venv\\Scripts\\python.exe scripts\\list_models.py
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, "src")


def main() -> int:
    load_dotenv()
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        print("GOOGLE_API_KEY missing from environment or .env")
        return 1

    from google import genai

    client = genai.Client(api_key=api_key)

    generate: list[tuple[str, str]] = []
    embed: list[tuple[str, str]] = []

    for model in client.models.list():
        name = (model.name or "").removeprefix("models/")
        actions = set(model.supported_actions or [])
        display = model.display_name or ""
        if "embedContent" in actions:
            embed.append((name, display))
        if "generateContent" in actions:
            generate.append((name, display))

    print(f"\n=== generateContent models ({len(generate)}) ===")
    for name, display in sorted(generate):
        print(f"  {name:<50} {display}")

    print(f"\n=== embedContent models ({len(embed)}) ===")
    for name, display in sorted(embed):
        print(f"  {name:<50} {display}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
