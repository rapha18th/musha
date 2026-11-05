"""Read every staged still once with the live model and keep the readings beside the stills.

A staged still keeps this reading when the model is off, and the seed writes it for the
opening and quiet stills without a model call. Check the file by eye after a run.

    backend/.venv/Scripts/python backend/eval/camera/read_stills.py [names]
"""
import asyncio
import json
import sys

from live import gemini, patiently, sandbox_shop

from musha import camera


async def main(names: list[str]):
    model = gemini()
    session, shop = sandbox_shop()
    target = camera.ASSETS / "readings.json"
    out = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    for path in sorted(camera.ASSETS.glob("*.jpg")):
        if names and path.stem not in names:
            continue
        reading = await patiently(lambda: camera.read(session, model, shop, path.read_bytes(), "image/jpeg"))
        print(f"{path.stem:22} {reading and reading['line']}", flush=True)
        if reading:
            out[path.stem] = reading
    target.write_text(json.dumps(dict(sorted(out.items())), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {target}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
