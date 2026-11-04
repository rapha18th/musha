"""Draw the delivery check's measured set: 30 deliveries and 30 counter scenes without one.

Every still is drawn from the quiet counter, so the room is the same shop seen by the same
phone. Goods, amounts and suppliers come from the seeded shop's products. People, light,
the phone's angle and partial occlusion vary from still to still. Each still is a 512 px
JPEG under 60 KB in stills/, and labels.json says what each one shows.

Four sets, in the order they were drawn. The reading prompt changed twice.
- main: 60 stills. The first prompt was tuned on them.
- probe: 12 stills aimed at the first prompt's failures, read before the second prompt was written.
- fresh: 20 stills held out from the second prompt. Two customers carrying goods out read as
  deliveries, and the third prompt was written to fix that.
- test: 20 stills drawn after the third prompt was fixed. They were checked by eye before any
  reading and read once. This is the held-out measure.
Every still in a set is checked by eye before the set is read; a still is redrawn only when
its picture does not show its label.

    backend/.venv/Scripts/python backend/eval/camera/make_eval_set.py          # draws what is missing
    backend/.venv/Scripts/python backend/eval/camera/make_eval_set.py d07 n12  # redraws these

Look at every still after a run: a label holds only when the picture shows it.
"""
import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

from make_stills import OUT, RAW, draw, vertex  # noqa: E402

from musha.seed import PRODUCTS  # noqa: E402

STILLS = HERE / "stills"
LABELS = HERE / "labels.json"
WIDTH = 512
MAX_BYTES = 60_000

ROOM = ("This is the same tuck shop counter as in the reference picture, photographed by the same phone "
        "propped at one end of the counter. Keep the room, the shelves and their layout, and the colours. ")
FINISH = (" Photorealistic phone camera picture with slight noise. Text and brand logos stay blurred and "
          "unreadable.")
MANAGER = "the manager"
WHO_MANAGES = "The manager is a young Zimbabwean man behind the counter."

LIGHT = [
    "Bright midday light.",
    "Soft early morning light.",
    "An overcast day, with dim, flat light.",
    "Late afternoon, with warm low sun from the front.",
    "Evening: the street outside is dark and one bulb lights the counter.",
]
ANGLE = [
    "The camera angle stays as in the reference.",
    "The phone has slipped and now tilts a little down at the counter.",
    "The phone sits a little further left and looks more towards the shelves.",
    "The phone sits a little higher and looks down along the counter.",
]
SHOULDER = "A customer's shoulder and back fill the near left of the picture and hide part of the counter."
HAND = "A hand reaching past the phone blurs one corner of the picture."
OCCLUSION = [SHOULDER, "The goods sit at the edge of the frame, partly cut off.", HAND,
             "A customer standing at the counter hides part of the goods."]
WHO = [
    "A supplier in a blue work jacket",
    "A delivery woman in a headscarf",
    "Two delivery men",
    "An older delivery man in grey overalls",
    "A young delivery man in a cap",
    "A driver from {payee}",
    "A delivery woman with a baby tied on her back",
    "A tall supplier in a reflective vest",
]
ARRIVE = [
    "sets {goods} down on the counter in front of {manager}.",
    "hands {goods} across the counter to {manager}.",
    "carries {goods} in through the open front towards the counter, where {manager} waits.",
    "wheels {goods} up to the counter on a hand trolley while {manager} looks on.",
    "unloads {goods} onto the end of the counter while {manager} counts them.",
    "passes {goods} through the open grille to {manager}.",
    "holds out a delivery note across the counter. {Goods} stands on the counter beside the note, just "
    "set down, and {manager} counts it.",
]

NUMBERS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
           "sixteen seventeen eighteen nineteen").split()
TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}


def spell(n: int) -> str:
    if n < 20:
        return NUMBERS[n]
    if n // 10 in TENS:
        return TENS[n // 10] + (f"-{NUMBERS[n % 10]}" if n % 10 else "")
    return str(n)


def one_unit(unit: str) -> str:
    """'trays' -> 'tray', 'loaves' -> 'loaf', '2 L bottles' -> '2 L bottle'."""
    head, _, last = unit.rpartition(" ")
    last = last[:-3] + "f" if last.endswith("ves") else last.rstrip("s")
    return f"{head} {last}".strip()


def bulk(name: str, unit: str, n: int) -> str:
    return f"a wholesale lot of {spell(n)} {unit} of {name}"


def deliveries() -> list[dict]:
    out = []
    for i in range(30):
        name, unit, _, payee, pack, _ = PRODUCTS[i % len(PRODUCTS)]
        n = (pack, pack * 2, pack + pack // 2)[i % 3]
        goods = bulk(name, unit, n)
        who = WHO[i % len(WHO)].format(payee=payee)
        arrive = ARRIVE[i % len(ARRIVE)].format(goods=goods, Goods=goods[0].upper() + goods[1:], manager=MANAGER)
        if who.startswith("Two"):
            arrive = arrive.replace("sets ", "set ").replace("hands ", "hand ").replace("carries ", "carry ") \
                .replace("wheels ", "wheel ").replace("unloads ", "unload ").replace("passes ", "pass ") \
                .replace("holds ", "hold ")
        occlusion = OCCLUSION[(i // 3) % len(OCCLUSION)] if i % 3 == 2 else ""
        out.append({
            "name": f"d{i + 1:02d}-{name.replace(' ', '-')}", "delivery": True, "goods": name, "amount": n,
            "unit": unit, "scene": "delivery", "light": LIGHT[i % len(LIGHT)],
            "angle": ANGLE[(i // 2) % len(ANGLE)], "occlusion": occlusion,
            "action": f"{who} {arrive} {WHO_MANAGES}",
        })
    return out


def others() -> list[dict]:
    names = [p[0] for p in PRODUCTS]
    units = {p[0]: p[1] for p in PRODUCTS}
    packs = {p[0]: p[4] for p in PRODUCTS}
    rows = []

    def add(scene: str, action: str, goods: str = "", light: bool = True):
        rows.append({"scene": scene, "action": action, "in_view": goods, "lit": light})

    # customers buying, some of them several units at once
    for name in names:
        add("sale", f"A customer pays {MANAGER} in cash for one {one_unit(units[name])} of {name} and holds it "
                    "in her hand.", name)
    add("sale", f"A customer turns away from the counter carrying two {units[names[0]]} of {names[0]} she "
                "just bought.", names[0])
    add("sale", f"A customer counts change at the counter with three {units[names[1]]} of {names[1]} in a "
                "carrier bag in front of him.", names[1])
    add("sale", f"Three customers queue at the counter while {MANAGER} serves the first.")
    add("sale", f"A child stands on tiptoe at the counter and buys one {one_unit(units[names[-1]])} of "
                f"{names[-1]}.", names[-1])
    # the manager stocks the shelves from the back room
    for name in names:
        add("restock", f"Nobody else is in view. The manager, a young Zimbabwean man, stands in the narrow "
                       "aisle between the counter and the shelves on the left. An open carton from the store "
                       f"room sits at his feet with {spell(packs[name])} {units[name]} of {name} in it, and he "
                       f"stacks them on the {name} shelf.", name)
    add("restock", f"Nobody else is in view. The manager kneels behind the counter and fills the bottom shelf "
                   f"from an open carton of {names[2]}.", names[2])
    add("restock", f"Nobody else is in view. The manager moves {units[names[1]]} of {names[1]} from a high "
                   "shelf down to a lower shelf.", names[1])
    # the empty counter
    for _ in range(5):
        add("empty", "The shop is open and nobody is in view.")
    add("empty", "The shop is open and nobody is in view. Two shelves are half empty.")
    # a customer carrying a bag of shopping
    add("shopping", "A woman walks in carrying a full plastic bag of shopping from the market and stops at "
                    "the counter.")
    add("shopping", f"A customer sets a bulging carrier bag of shopping on the counter and pays {MANAGER} for "
                    "airtime.")
    add("shopping", "A man with a large woven shopping bag over his shoulder leans on the counter, talking "
                    f"to {MANAGER}.")
    add("shopping", "A customer stands at the counter holding two full carrier bags of groceries.")
    add("shopping", "A woman with a child tied on her back carries a sack of shopping past the counter "
                    "towards the street.")
    # the closed shutter
    add("closed", "The shop is closed. The metal shutter is pulled down over the whole front. Inside is dim, "
                  "lit only by thin daylight around the shutter edges. Nobody is in view.", light=False)
    add("closed", "Night. The shop is closed with the shutter down, and a weak bulb is the only light. "
                  "Nobody is in view.", light=False)
    add("closed", "Early morning before opening. The shutter is down and the manager stands behind the "
                  "counter counting cash under a bulb.", light=False)
    add("closed", f"The shop is closed with the shutter down. A few cartons of {names[3]} stand on the floor "
                  "by the shelves, waiting to be shelved. Nobody is in view.", names[3], light=False)
    add("closed", "Lunch time. The metal shutter over the whole front is pulled half way down, and the "
                  "grille below it is locked. Nobody is in view.", light=False)

    out = []
    for i, r in enumerate(rows):
        action = r["action"] + (f" {WHO_MANAGES}" if f" {MANAGER} " in f" {r['action']} " else "")
        occlusion = ""
        if i % 4 == 3 and r["scene"] != "closed":  # nobody stands in a scene where nobody is in view
            occlusion = HAND if "nobody" in action.lower() else SHOULDER
        out.append({
            "name": f"n{i + 1:02d}-{r['scene']}", "delivery": False, "goods": "", "in_view": r["in_view"],
            "scene": r["scene"], "light": LIGHT[(i + 2) % len(LIGHT)] if r["lit"] else "",
            "angle": ANGLE[(i // 2) % len(ANGLE)], "occlusion": occlusion, "action": action,
        })
    return out


def probe() -> list[dict]:
    """Twelve more stills aimed at the first prompt's failures: women carrying babies as suppliers,
    customers carrying out bulk, the manager restocking. Both prompts read them."""
    names = [p[0] for p in PRODUCTS]
    units = {p[0]: p[1] for p in PRODUCTS}
    packs = {p[0]: p[4] for p in PRODUCTS}
    payee = {p[0]: p[3] for p in PRODUCTS}
    e, b, o, s = names  # the seeded products in order
    lot = lambda n: bulk(n, units[n], packs[n])  # noqa: E731
    pack, sale = lambda n: packs[n], ""  # deliveries say how many units arrive; a sale has no goods label
    rows = [
        (e, pack(e), f"A woman with a baby tied on her back carries {lot(e)} in from the street and hands it "
                     f"across the counter to {MANAGER}."),
        (b, pack(b), f"A woman with a baby tied on her back sets {lot(b)} down on the counter and gives "
                     f"{MANAGER} a delivery note."),
        (o, pack(o), f"A young man has leaned a bicycle against the front. Cartons are tied on its carrier, and "
                     f"he lifts a carton of {spell(packs[o])} {units[o]} of {o} onto the counter for {MANAGER}."),
        (s, pack(s), f"An elderly woman in a church uniform puts {lot(s)} on the counter and counts it out with "
                     f"{MANAGER}."),
        (e, pack(e), f"A porter from {payee[e]} stands on the customer side of the counter beside {lot(e)} on "
                     f"the floor and reads a delivery note to {MANAGER}."),
        (o, 2 * pack(o), f"A woman with a baby tied on her back hands two shrink-wrapped packs of "
                         f"{spell(packs[o])} {units[o]} of {o} across the counter to {MANAGER}, one after the "
                         "other."),
        (sale, 0, f"A customer, her back to the camera, walks out through the front onto the street carrying a "
                  f"crate of {spell(packs[b])} {units[b]} of {b} she just bought, while {MANAGER} counts her "
                  "cash behind the counter."),
        (sale, 0, f"Nobody else is in view. The manager, a young Zimbabwean man, carries an open carton of {e} "
                  f"along the aisle behind the counter and fills the {e} shelf."),
        (sale, 0, f"Nobody else is in view. The manager, a young Zimbabwean man, has opened a carton of {o} on "
                  f"the counter and puts the bottles on the {o} shelf behind him."),
        (sale, 0, f"A customer pays {MANAGER} for a shrink-wrapped pack of {spell(packs[o])} {units[o]} of {o} "
                  "and lifts it off the counter to carry it home."),
        (sale, 0, f"A woman with a baby tied on her back buys one {one_unit(units[b])} of {b} from {MANAGER} "
                  "and holds out her money."),
        (sale, 0, "Two customers talk at the counter. One has a sack of shopping from the market at her feet."),
    ]
    scenes = ("sale", "restock", "restock", "sale", "sale", "shopping")
    out = []
    for i, (goods, amount, action) in enumerate(rows):
        delivery = bool(goods)
        scene = "delivery" if delivery else scenes[i - 6]
        action += f" {WHO_MANAGES}" if MANAGER in action else ""
        out.append({
            "name": f"h{i + 1:02d}-{goods.replace(' ', '-') if delivery else scene}", "set": "probe",
            "delivery": delivery, "goods": goods, "amount": amount if delivery else None,
            "unit": units[goods] if delivery else "", "scene": scene,
            "light": LIGHT[(i + 1) % len(LIGHT)], "angle": ANGLE[(i + 1) % len(ANGLE)],
            "occlusion": "", "action": action,
        })
    return out


def fresh() -> list[dict]:
    """Twenty stills drawn after the second prompt was fixed: ten deliveries and ten scenes without one,
    in new words, with new people and new hard cases. The third prompt was written after they were read."""
    names = [p[0] for p in PRODUCTS]
    units = {p[0]: p[1] for p in PRODUCTS}
    packs = {p[0]: p[4] for p in PRODUCTS}
    payee = {p[0]: p[3] for p in PRODUCTS}
    e, b, o, s = names
    lot = lambda n, k=1: bulk(n, units[n], k * packs[n])  # noqa: E731
    M, The = MANAGER, MANAGER[0].upper() + MANAGER[1:]
    arrivals = [  # goods, units arriving, light, angle, occlusion, action
        (e, packs[e], LIGHT[0], ANGLE[0], "",
         f"A man pushes a wheelbarrow loaded with {lot(e)} up to the open front and lifts the stack onto the "
         f"counter for {M}."),
        (b, 2 * packs[b], LIGHT[4], ANGLE[1], "",
         f"A bakery driver in a white coat carries two plastic crates holding {lot(b, 2)}, one on top of the "
         f"other, in through the front and puts them on the counter. {The} signs the driver's delivery book."),
        (o, packs[o], LIGHT[2], ANGLE[2], "",
         f"A supplier has come in from the street. He stands on the customer side of the counter, by the open "
         f"front, with his back half turned to the camera, and slides {lot(o)}, shrink-wrapped together, "
         f"across the counter to {M}, who stands behind it by the shelves."),
        (s, packs[s], LIGHT[1], ANGLE[0], "",
         f"A woman comes in through the front towards the counter balancing {lot(s)}, wrapped in one bale, on "
         f"her head. {The} reaches out to take it."),
        (e, packs[e], LIGHT[3], ANGLE[3], "",
         f"A delivery man from {payee[e]} has walked in from the street. He stands on the right of the picture, "
         f"between the counter and the open front, and sets {lot(e)} on the counter in front of {M}. Further "
         f"along the counter a customer waits to buy one loaf of {b}."),
        (b, packs[b], LIGHT[0], ANGLE[1], "",
         f"A small delivery van is parked in the street in front of the shop with its back doors open. A man in "
         f"a cap carries {lot(b)} from the van and sets it on the counter in front of {M}."),
        (o, packs[o], LIGHT[2], ANGLE[0], HAND,
         f"A supplier in a green work shirt stands on the customer side of the counter, by the open front. He "
         f"has just set {lot(o)} on the counter and holds out a delivery note to {M}, who counts the bottles "
         "with one finger."),
        (s, 2 * packs[s], LIGHT[1], ANGLE[2], "",
         f"A supplier in a red overall and his helper carry {lot(s, 2)} between them, in two bales, in through "
         f"the front to the counter, where {M} waits."),
        (e, packs[e], LIGHT[4], ANGLE[3], "",
         f"The metal shutter is pulled half way down. A supplier has ducked under it and passes {lot(e)} "
         f"across the counter to {M}."),
        (b, packs[b], LIGHT[3], ANGLE[1], SHOULDER,
         f"A woman in a pink apron from {payee[b]} lifts {lot(b)} out of a large basket and stacks the loaves "
         f"on the counter, while {M} counts them."),
    ]
    others = [  # scene, light, angle, occlusion, action
        ("sale", LIGHT[0], ANGLE[2], "",
         f"A market trader has just bought a stack of {spell(packs[e])} {units[e]} of {e} for her own stall. "
         f"She has turned away from the counter and walks towards the open front and the street, carrying the "
         f"stack, her back to the camera. {The} puts her cash away behind the counter."),
        ("sale", LIGHT[2], ANGLE[0], "",
         f"A man walks out through the front onto the street, his back to the camera, with a bale of "
         f"{spell(packs[s])} {units[s]} of {s} he just bought on his head. {The} writes in a cash book behind "
         "the counter."),
        ("restock", LIGHT[1], ANGLE[1], "",
         f"Nobody else is in view. The manager, a young Zimbabwean man, carries a closed carton of {o} out of "
         "the store room door at the back of the shop and sets it down behind the counter."),
        ("restock", LIGHT[3], ANGLE[0], "",
         f"Nobody else is in view. The manager, a young Zimbabwean man, stands on a stool behind the counter "
         f"and fills the top shelf with loaves of {b} from a crate at his feet."),
        ("empty", LIGHT[0], ANGLE[3], "",
         f"The shop is open and nobody is inside. Out in the street, a woman walks past with a stack of "
         f"{units[e]} of {e} on her head, on her way somewhere else."),
        ("shopping", LIGHT[2], ANGLE[2], "",
         f"A customer has put a heavy sack of maize meal from the market on the floor by the counter and buys "
         f"airtime from {M}."),
        ("sale", LIGHT[4], ANGLE[0], "",
         f"Four customers crowd the counter. {The} hands one of them a loaf of {b} and takes cash from "
         "another."),
        ("empty", "An overcast day. Rain falls on the street outside.", ANGLE[1], "",
         "The shop is open and nobody is in view."),
        ("closed", "", ANGLE[0], "",
         "Sunday. The metal shutter is down over the whole front and padlocked. Dim light comes through a gap "
         "above the shutter. Nobody is in view."),
        ("shopping", LIGHT[3], ANGLE[2], SHOULDER,
         f"A man carrying a full woven bag of vegetables from the market comes in and asks {M} for one loaf of "
         f"{b}."),
    ]
    rows = [dict(goods=g, amount=n, unit=units[g], scene="delivery", light=li, angle=an, occlusion=oc, action=ac)
            for g, n, li, an, oc, ac in arrivals]
    rows += [dict(goods="", amount=None, unit="", scene=sc, light=li, angle=an, occlusion=oc, action=ac)
             for sc, li, an, oc, ac in others]
    out = []
    for i, r in enumerate(rows):
        delivery = bool(r["goods"])
        if "manager" in r["action"].lower() and "Zimbabwean" not in r["action"]:
            r["action"] += f" {WHO_MANAGES}"
        out.append({"name": f"f{i + 1:02d}-{r['goods'].replace(' ', '-') if delivery else r['scene']}",
                    "set": "fresh", "delivery": delivery, **r})
    return out


def held_out() -> list[dict]:
    """Twenty stills drawn after the third prompt was fixed, set "test". Every supplier and customer
    stands where the picture can show it: suppliers on the customer side, the manager behind the
    counter. Nothing in the prompt changes after they are read."""
    names = [p[0] for p in PRODUCTS]
    units = {p[0]: p[1] for p in PRODUCTS}
    packs = {p[0]: p[4] for p in PRODUCTS}
    payee = {p[0]: p[3] for p in PRODUCTS}
    e, b, o, s = names
    lot = lambda n, k=1: bulk(n, units[n], k * packs[n])  # noqa: E731
    M, The = MANAGER, MANAGER[0].upper() + MANAGER[1:]
    SIDE = "on the right of the picture, between the counter and the open front"
    arrivals = [  # goods, units arriving, light, angle, occlusion, action
        (e, packs[e], LIGHT[1], ANGLE[2], "",
         f"A supplier in a brown dust coat walks in from the street towards the counter, {SIDE}, carrying "
         f"{lot(e)} in his arms and looking at {M}, who waits behind the counter."),
        (b, packs[b], LIGHT[0], ANGLE[0], "",
         f"A bakery delivery man in a white coat stands {SIDE} with his back to the camera. He holds {lot(b)} "
         f"in a plastic crate out across the counter, and {M}, standing behind the counter by the shelves, "
         "takes it from him."),
        (o, packs[o], LIGHT[3], ANGLE[1], "",
         f"A woman in a yellow headscarf from {payee[o]} stands {SIDE}. She has set {lot(o)}, shrink-wrapped, on "
         f"the counter and waits while {M} checks the bottles against her delivery note."),
        (s, packs[s], LIGHT[2], ANGLE[3], "",
         f"A man has pulled a handcart loaded with {lot(s)} up to the open front. He stands {SIDE} and lifts "
         f"the packs onto the counter for {M}, half of them already there."),
        (e, packs[e], LIGHT[4], ANGLE[0], "",
         f"A supplier has parked a motorbike at the front. He stands {SIDE} and passes {lot(e)} across the "
         f"counter to {M}."),
        (b, 2 * packs[b], LIGHT[1], ANGLE[1], "",
         f"A bakery van driver stands {SIDE} and stacks two crates holding {lot(b, 2)} on the counter, while "
         f"{M} counts the loaves. A customer waits behind the driver."),
        (o, packs[o], LIGHT[0], ANGLE[2], "",
         f"A delivery man stands {SIDE}, his back half to the camera, and lowers a carton of {lot(o)} from his "
         f"shoulder onto the counter. {The} holds the delivery note."),
        (s, packs[s], LIGHT[3], ANGLE[0], "",
         f"An old man in a hat stands {SIDE} and sets {lot(s)}, tied in a bale, on the counter. {The} unties "
         "the bale and counts the packs."),
        (e, packs[e], LIGHT[2], ANGLE[3], SHOULDER,
         f"A supplier stands {SIDE} and hands {lot(e)} across the counter to {M}."),
        (b, packs[b], LIGHT[1], ANGLE[0], HAND,
         f"A woman from {payee[b]} stands {SIDE} and lifts {lot(b)} from a crate on her head onto the counter "
         f"in front of {M}."),
    ]
    others = [  # scene, light, angle, occlusion, action
        ("sale", LIGHT[0], ANGLE[1], "",
         f"A man has just bought a shrink-wrapped pack of {spell(packs[o])} {units[o]} of {o}. He carries it "
         f"away from the counter towards the open front and the street. {The} counts the cash behind the "
         "counter."),
        ("sale", LIGHT[3], ANGLE[0], "",
         f"A woman walks out of the shop onto the street, her back to the camera, with a crate of "
         f"{spell(packs[b])} {units[b]} of {b} she just bought on her head. {The} stands behind the counter."),
        ("sale", LIGHT[2], ANGLE[2], "",
         f"A customer stands at the counter and pays {M} for two {units[e]} of {e}. The trays sit on the "
         "counter between them, and she holds out the banknotes."),
        ("sale", LIGHT[1], ANGLE[3], "",
         f"Two schoolchildren in uniform buy one loaf of {b} each from {M}."),
        ("restock", LIGHT[0], ANGLE[0], "",
         f"Nobody else is in view. The manager, a young Zimbabwean man, stands behind the counter on the left "
         f"of the picture, in the narrow aisle between the counter and the shelves. He has carried a crate of "
         f"loaves of {b} from the store room at the back and puts the loaves on the bread shelf."),
        ("restock", LIGHT[4], ANGLE[2], "",
         f"Nobody else is in view. The manager, a young Zimbabwean man, kneels behind the counter and unpacks "
         f"a carton of {units[s]} of {s} onto the bottom shelf."),
        ("shopping", LIGHT[3], ANGLE[1], "",
         f"A woman sets two full carrier bags of shopping on the counter while she buys one bottle of {o} from "
         f"{M}."),
        ("shopping", LIGHT[0], ANGLE[3], SHOULDER,
         "A man with a sack of potatoes from the market on his shoulder walks away from the counter and out "
         "to the street, his back to the camera."),
        ("empty", LIGHT[1], ANGLE[0], "",
         "The shop is open and nobody is in view. A broom leans against the counter."),
        ("closed", "", ANGLE[1], "",
         "Night. The metal shutter is down over the whole front. One weak bulb is on. Nobody is in view."),
    ]
    rows = [dict(goods=g, amount=n, unit=units[g], scene="delivery", light=li, angle=an, occlusion=oc, action=ac)
            for g, n, li, an, oc, ac in arrivals]
    rows += [dict(goods="", amount=None, unit="", scene=sc, light=li, angle=an, occlusion=oc, action=ac)
             for sc, li, an, oc, ac in others]
    out = []
    for i, r in enumerate(rows):
        delivery = bool(r["goods"])
        if "manager" in r["action"].lower() and "Zimbabwean" not in r["action"]:
            r["action"] += f" {WHO_MANAGES}"
        out.append({"name": f"t{i + 1:02d}-{r['goods'].replace(' ', '-') if delivery else r['scene']}",
                    "set": "test", "delivery": delivery, **r})
    return out


def prompt(label: dict) -> str:
    bits = [label["angle"], label["light"], label["action"], label["occlusion"]]
    return ROOM + " ".join(b for b in bits if b) + FINISH


def labels() -> list[dict]:
    rows = deliveries() + others()
    for r in rows:
        r["set"] = "main"
    rows += probe() + fresh() + held_out()
    for r in rows:
        r["prompt"] = prompt(r)
    return rows


def small_jpeg(raw: bytes) -> bytes:
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im = im.resize((WIDTH, round(im.height * WIDTH / im.width)), Image.LANCZOS)
    for quality in (85, 80, 75, 70, 65, 60, 55, 50, 45, 40):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
        if buf.tell() <= MAX_BYTES:
            break
    return buf.getvalue()


def main(only: list[str]):
    from google.genai import types

    rows = labels()
    LABELS.write_text(json.dumps({r["name"]: {k: v for k, v in r.items() if k != "name"} for r in rows},
                                 indent=2) + "\n", encoding="utf-8")
    STILLS.mkdir(exist_ok=True)
    todo = [r for r in rows if any(r["name"].startswith(o) for o in only)] if only else \
        [r for r in rows if not (STILLS / f"{r['name']}.jpg").exists()]
    client = vertex()
    raw = RAW / "quiet.png"  # the full size original when this machine drew it, else the app's still
    ref = (types.Part.from_bytes(data=raw.read_bytes(), mime_type="image/png") if raw.exists() else
           types.Part.from_bytes(data=(OUT / "quiet.jpg").read_bytes(), mime_type="image/jpeg"))

    def one(r):
        try:
            jpg = small_jpeg(draw(client, [ref, r["prompt"]]))
        except RuntimeError as e:
            return f"{r['name']}: {e}"
        (STILLS / f"{r['name']}.jpg").write_bytes(jpg)
        return f"{r['name']}: {len(jpg) // 1000} KB"

    with ThreadPoolExecutor(3) as pool:
        for line in pool.map(one, todo):
            print(line, flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
