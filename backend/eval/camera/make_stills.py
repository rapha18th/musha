"""Draw the counter camera's staged stills with Gemini's image model.

One small tuck shop counter in Mbare, Harare, seen from a phone propped on the counter.
The first still sets the scene and every later still is drawn from it, so the room,
the shelves and the angle stay the same. Each still is a 640 px JPEG under 120 KB in
backend/musha/assets/counter/<scene>[-<goods>].jpg.

    python -I backend/eval/camera/make_stills.py              # every still that is missing
    python -I backend/eval/camera/make_stills.py busy closed  # redraw these

A still that misses its scene, such as the manager drawn on the customer side or a delivery far
short of its count, is drawn again. Then read_stills.py keeps each still's reading beside it.
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

# The manager is Farai, a woman. "Behind the counter" is the shelf side, on the left of the picture;
# a supplier stands on the customer side, on the right by the open front.
MANAGER = "a young Zimbabwean woman in a plain apron, her hair in short braids"
BEHIND = "stands on the shelf side of the counter, between the shelves and the counter on the left of the picture,"
SCENES = {
    "quiet": "The shop is open. Nobody is in view. The shelves are well stocked.",
    "opening": f"Early morning. The shop has just opened. The manager, {MANAGER}, stands behind the counter "
               "setting loaves of bread on the shelf. Nobody else is in view.",
    "busy": "The shop is open and busy. Three customers stand on the customer side of the counter, one paying "
            f"with cash, while the manager, {MANAGER}, serves them from behind the counter.",
    "delivery-eggs": "A supplier in a blue work jacket has carried a wholesale lot of forty cardboard trays of "
                     "brown eggs in through the front. Four stacks of egg trays, ten trays in each, stand side by "
                     "side in one row across the counter, a hand's width apart, so every tray edge can be counted. "
                     f"He hands a delivery note across to the manager, {MANAGER}, who receives the eggs from "
                     "behind the counter.",
    "delivery-bread": "A bakery delivery man in a cap stands on the customer side of the counter, on the right "
                      "by the open front, and sets a large plastic bread crate full of wrapped loaves on the "
                      f"counter, with a second full crate on the floor beside him. The manager, {MANAGER}, "
                      f"{BEHIND} and counts the loaves.",
    "delivery-cooking-oil": "A wholesale delivery of twenty-four 2 litre cooking oil bottles has just arrived on "
                            "the counter: four shrink-wrapped packs of six bottles, two packs side by side with "
                            "two more packs stacked on top of them. A supplier in plain grey overalls stands on "
                            f"the customer side, on the right by the open front. The manager, {MANAGER}, "
                            f"{BEHIND} and receives them.",
    "delivery-sugar": "A supplier in a brown dust coat sets a large shrink-wrapped bale of ten 2 kg white sugar "
                      f"packs on the counter from the customer side. The manager, {MANAGER}, receives it from "
                      "behind the counter.",
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
