"""Draw the counter camera's staged stills with Gemini's image model.

One small tuck shop counter in Mbare, Harare, seen from a phone propped on the counter.
The first still sets the scene and every later still is drawn from it, so the room,
the shelves and the angle stay the same. Each still is a 640 px JPEG under 120 KB in
backend/musha/assets/counter/<scene>[-<goods>].jpg.

    python -I backend/eval/camera/make_stills.py              # every still that is missing
    python -I backend/eval/camera/make_stills.py busy closed  # redraw these

Then read_stills.py keeps each still's reading beside it.
"""
import io
import os
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "musha" / "assets" / "counter"
RAW = HERE / "raw"  # full size originals, kept out of the repo
MODELS = ("gemini-3.1-flash-image", "gemini-3-pro-image")
WIDTH = 640
MAX_BYTES = 120_000

ROOM = (
    "A photograph taken by a phone propped at one end of the serving counter of a small tuck shop "
    "in Mbare, Harare, Zimbabwe, in daylight. The phone looks along the counter at a slightly wide "
    "angle. The worn wooden counter runs through the foreground. Behind it, on the left, wooden "
    "shelves hold stacked cardboard trays of brown eggs, loaves of white bread in plastic bags, "
    "2 litre bottles of cooking oil and 2 kg packs of white sugar. On the right is the open front "
    "of the shop with a green metal security grille, and the busy street outside. Painted concrete "
    "walls, a corrugated iron roof edge, natural light from the front. Photorealistic, phone camera "
    "quality, slight noise. Text and brand logos stay blurred and unreadable."
)
SAME = (
    "This is the same tuck shop counter, photographed from the same phone in the same position. "
    "Keep the room, the shelves and their layout, the colours, the light and the camera angle "
    "exactly as in the reference picture. Change only this: "
)

SCENES = {
    "quiet": "The shop is open. Nobody is in view. The shelves are well stocked.",
    "opening": "Early morning. The shop has just opened. A young Zimbabwean man, the manager, stands "
               "behind the counter setting loaves of bread on the shelf. Nobody else is in view.",
    "busy": "The shop is open and busy. Three customers stand at the counter, one paying with cash, "
            "while the manager, a young Zimbabwean man, serves them from behind the counter.",
    "delivery-eggs": "A supplier in a work jacket carries a tall stack of about ten cardboard trays of "
                     "brown eggs through the front and sets them on the counter. The manager, a young "
                     "Zimbabwean man, receives them from behind the counter.",
    "delivery-bread": "A bakery delivery man sets a large plastic bread crate full of wrapped loaves on "
                      "the counter, with a second crate on the floor beside him. The manager, a young "
                      "Zimbabwean man, counts the loaves from behind the counter.",
    "delivery-cooking-oil": "A supplier sets two shrink-wrapped packs of six 2 litre cooking oil bottles "
                            "on the counter. The manager, a young Zimbabwean man, receives them from "
                            "behind the counter.",
    "delivery-sugar": "A supplier sets a large bale of 2 kg white sugar packs on the counter. The "
                      "manager, a young Zimbabwean man, receives it from behind the counter.",
    "shelf_low": "The shop is open and nobody is in view. The egg shelf is nearly empty: a single "
                 "tray of eggs is left where the stacks of egg trays stood. Every other shelf stays "
                 "stocked.",
    "closed": "The shop is closed. The metal shutter is pulled down over the whole front opening, the "
              "inside is dim, lit only by thin daylight around the shutter edges. Nobody is in view.",
}


def draw(client, contents) -> bytes:
    config = types.GenerateContentConfig(response_modalities=["IMAGE"],
                                         image_config=types.ImageConfig(aspect_ratio="4:3"))
    last = None
    for model in MODELS:
        for attempt in range(2):
            try:
                r = client.models.generate_content(model=model, contents=contents, config=config)
                for part in r.candidates[0].content.parts:
                    if part.inline_data and part.inline_data.data:
                        print(f"  drawn by {model}")
                        return part.inline_data.data
                last = "no image in the answer"
            except Exception as e:  # try again, then the other model
                last = e
            time.sleep(2)
    raise RuntimeError(f"image model failed: {last}")


def small_jpeg(raw: bytes) -> bytes:
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im = im.resize((WIDTH, round(im.height * WIDTH / im.width)), Image.LANCZOS)
    for quality in (85, 80, 75, 70, 65, 60, 55, 50):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
        if buf.tell() <= MAX_BYTES:
            return buf.getvalue()
    return buf.getvalue()


def vertex():
    """The image model on Vertex AI, for the project in GOOGLE_CLOUD_PROJECT."""
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise SystemExit("Set GOOGLE_CLOUD_PROJECT to the Google Cloud project that draws the stills.")
    return genai.Client(vertexai=True, project=project, location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"))


def main(names: list[str]):
    client = vertex()
    RAW.mkdir(exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    wanted = names or [n for n in SCENES if not (OUT / f"{n}.jpg").exists()]
    ref_path = RAW / "quiet.png"  # raw/ stays on the machine that drew it
    for name in wanted:
        print(name)
        if name == "quiet":
            raw = draw(client, [f"{ROOM} {SCENES['quiet']}"])
        else:
            if not ref_path.exists():
                raise SystemExit("Draw the quiet still first: it is the reference for the others.")
            ref = types.Part.from_bytes(data=ref_path.read_bytes(), mime_type="image/png")
            raw = draw(client, [ref, SAME + SCENES[name]])
        png = io.BytesIO()
        Image.open(io.BytesIO(raw)).save(png, "PNG")
        (RAW / f"{name}.png").write_bytes(png.getvalue())
        jpg = small_jpeg(raw)
        (OUT / f"{name}.jpg").write_bytes(jpg)
        print(f"  {len(jpg) // 1000} KB")


if __name__ == "__main__":
    main(sys.argv[1:])
