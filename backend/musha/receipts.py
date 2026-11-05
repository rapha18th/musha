"""Thermal-printer receipts, rendered as phone photos, for the sandbox shop and the proof engine's test set.

`render_receipt(spec, seed=...)` is deterministic. The paper (the till's font, print density,
fading, the torn edge) follows the spec, so it stays the same slip of paper; the seed is the camera:
the table, the angle, the light, the noise and the JPEG. Two seeds of one spec read as two photos of
one receipt. `tilt` and `light` set the camera by hand: a phone held at an angle, a dim shop.
"""
import functools
import hashlib
import io
import math
import random
import re
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from .money import to_value

PHOTO = (960, 1280)  # a phone photo, portrait
PAPER_WIDTH = 540
MARGIN = 26

_FONT_DIRS = [Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/dejavu"),
              Path("C:/Windows/Fonts"), Path("/Library/Fonts"), Path("/System/Library/Fonts")]
_MONO = ["DejaVuSansMono.ttf", "consola.ttf", "cour.ttf", "Menlo.ttc", "Courier New.ttf"]
_MONO_BOLD = ["DejaVuSansMono-Bold.ttf", "consolab.ttf", "courbd.ttf", "Menlo.ttc", "Courier New Bold.ttf"]
# Till fonts: regular and bold files, tried before the defaults above
FONTS = {
    "dejavu": (["DejaVuSansMono.ttf"], ["DejaVuSansMono-Bold.ttf"]),
    "liberation": (["LiberationMono-Regular.ttf"], ["LiberationMono-Bold.ttf"]),
    "courier": (["cour.ttf", "Courier New.ttf"], ["courbd.ttf", "Courier New Bold.ttf"]),
    "consolas": (["consola.ttf"], ["consolab.ttf"]),
    "lucida": (["lucon.ttf"], ["lucon.ttf"]),
    "narrow": (["LiberationSansNarrow-Regular.ttf"], ["LiberationSansNarrow-Bold.ttf"]),
}
LIGHTS = ("", "dim", "warm", "bright", "shade")


@dataclass(frozen=True)
class Line:
    name: str
    quantity: int
    unit_cents: int
    total_cents: int | None = None  # set only to print a line that does not add up

    @property
    def line_cents(self) -> int:
        return self.quantity * self.unit_cents if self.total_cents is None else self.total_cents


@dataclass(frozen=True)
class ReceiptSpec:
    payee: str
    address: str
    receipt_no: str
    at: datetime  # shop time, as printed
    lines: tuple[Line, ...]
    currency: str = "USD"
    cash_cents: int | None = None  # None: the next round ten dollars
    footer: str = "Thank you. Come again."
    total_cents: int | None = None  # set only to print a total that does not add up
    till: str = ""
    font: str = field(default="", repr=False)  # a key of FONTS; outside repr, so older paper keeps its look

    @property
    def total(self) -> int:
        return sum(x.line_cents for x in self.lines) if self.total_cents is None else self.total_cents

    @property
    def cash(self) -> int:
        if self.cash_cents is not None:
            return self.cash_cents
        return -(-self.total // 1000) * 1000

    def fingerprint(self) -> str:
        return hashlib.sha256(repr(self).encode()).hexdigest()


def render_receipt(spec: ReceiptSpec, *, seed: int | str = 0, tilt: float | None = None, light: str = "") -> bytes:
    """A JPEG photo of the printed receipt. `tilt` turns the slip by that many degrees in place of the
    seed's small angle; `light` is one of LIGHTS."""
    if light not in LIGHTS:
        raise ValueError(f"Light is one of {', '.join(repr(x) for x in LIGHTS)}.")
    return _render(spec, seed, tilt, light)


@functools.lru_cache(maxsize=16)  # the seed renders the same history receipt for every seeded shop
def _render(spec: ReceiptSpec, seed: int | str, tilt: float | None = None, light: str = "") -> bytes:
    return _photograph(_paper(spec), random.Random(f"camera:{seed}"), tilt=tilt, light=light)


def scan_receipt(spec: ReceiptSpec) -> bytes:
    """A JPEG of the slip laid flat, the way a phone's document scanner keeps it: the same paper as
    `render_receipt`, on a plain page. A twentieth of a photo's work, for receipts kept on file."""
    paper = _paper(spec)
    page = Image.new("RGB", (paper.width + 2 * MARGIN, paper.height + 2 * MARGIN), (238, 235, 228))
    page.paste(paper, (MARGIN, MARGIN), paper if paper.mode == "RGBA" else None)
    buf = io.BytesIO()
    page.save(buf, "JPEG", quality=84, optimize=True)
    return buf.getvalue()


def reading_of(spec: ReceiptSpec) -> dict:
    """What a careful reader takes from this receipt, in the proof engine's reading shape."""
    return {
        "payee": spec.payee,
        "receipt_no": spec.receipt_no,
        "date": spec.at.date().isoformat(),
        "time": spec.at.strftime("%H:%M"),
        "items": [{"name": x.name, "quantity": x.quantity, "unit_price": float(to_value(x.unit_cents)),
                   "line_total": float(to_value(x.line_cents))} for x in spec.lines],
        "total": float(to_value(spec.total)),
        "currency": spec.currency,
        "legible": True,
    }


def receipt_number(payee: str, n: int) -> str:
    """A payee's own numbering: its initials and a running number."""
    initials = "".join(w[0] for w in payee.split() if w[:1].isalnum()).upper()[:3] or "R"
    return f"{initials}-{n:06d}"


# The paper

def _font(names: list[str], size: int):
    for d in _FONT_DIRS:
        for n in names:
            p = d / n
            if p.exists():
                try:
                    return ImageFont.truetype(str(p), size)
                except OSError:
                    continue
    return ImageFont.load_default(size=size)


def _paper(spec: ReceiptSpec) -> Image.Image:
    rng = random.Random(f"paper:{spec.fingerprint()}")
    regular, heavy = FONTS.get(spec.font, ([], []))
    body = _font(regular + _MONO, 21)
    bold = _font(heavy + _MONO_BOLD, 21)
    big = _font(heavy + _MONO_BOLD, 27)
    width = PAPER_WIDTH
    inner = width - 2 * MARGIN
    rows: list[tuple] = []  # (kind, payload, font, height)

    def text(s, f=body, align="left", gap=6):
        rows.append(("text", (s, align), f, f.size + gap))

    def pair(left, right, f=body, gap=6):
        rows.append(("pair", (left, right), f, f.size + gap))

    def rule(ch="-"):
        rows.append(("rule", ch, body, body.size + 4))

    def space(h=10):
        rows.append(("space", None, body, h))

    space(22)
    for chunk in _wrap(spec.payee.upper(), big, inner):
        text(chunk, big, "center", 8)
    for chunk in _wrap(spec.address, body, inner):
        text(chunk, body, "center")
    space(8)
    rule("=")
    pair("RECEIPT NO", spec.receipt_no)
    pair("DATE", spec.at.strftime("%d/%m/%Y"))
    pair("TIME", spec.at.strftime("%H:%M"))
    if spec.till:
        pair("TILL", spec.till)
    rule()
    for line in spec.lines:
        for chunk in _wrap(line.name.upper(), body, inner):
            text(chunk)
        pair(f"  {line.quantity} x {to_value(line.unit_cents)}", to_value(line.line_cents))
    rule()
    pair(f"TOTAL {spec.currency}", to_value(spec.total), big, 10)
    pair("CASH", to_value(spec.cash))
    pair("CHANGE", to_value(max(0, spec.cash - spec.total)))
    rule("=")
    count = sum(x.quantity for x in spec.lines)
    text(f"ITEMS {count}", body, "center")
    space(6)
    for chunk in _wrap(spec.footer, body, inner):
        text(chunk, body, "center")
    space(46)

    height = sum(r[3] for r in rows)
    ink = Image.new("L", (width, height), 0)
    d = ImageDraw.Draw(ink)
    y = 0
    for kind, payload, f, h in rows:
        if kind == "text":
            s, align = payload
            w = d.textlength(s, font=f)
            x = MARGIN if align == "left" else (width - w) / 2
            d.text((x, y), s, font=f, fill=255)
        elif kind == "pair":
            left, right = payload
            d.text((MARGIN, y), left, font=f, fill=255)
            d.text((width - MARGIN - d.textlength(right, font=f), y), right, font=f, fill=255)
        elif kind == "rule":
            step = d.textlength(payload, font=f)
            n = int(inner // step)
            d.text((MARGIN, y), payload * n, font=f, fill=255)
        y += h

    # Thermal print: density drifts down the roll and fades in streaks
    profile = Image.new("L", (1, 24))
    profile.putdata([int(rng.uniform(175, 248)) for _ in range(24)])
    density = profile.resize((width, height), Image.BICUBIC)
    streaks = Image.new("L", (12, 1))
    streaks.putdata([int(rng.uniform(205, 255)) for _ in range(12)])
    density = ImageChops.multiply(density, streaks.resize((width, height), Image.BICUBIC))
    ink = ImageChops.multiply(ink.filter(ImageFilter.GaussianBlur(0.35)), density)

    tone = (int(rng.uniform(240, 250)), int(rng.uniform(238, 247)), int(rng.uniform(230, 241)))
    paper = Image.new("RGB", (width, height), tone)
    grain = Image.frombytes("L", (width // 2, height // 2), rng.randbytes((width // 2) * (height // 2)))
    grain = grain.resize((width, height), Image.BILINEAR).point(lambda v: v * 10 // 255)
    paper = ImageChops.subtract(paper, Image.merge("RGB", (grain, grain, grain)))
    ink_rgb = Image.new("RGB", (width, height), (34, 33, 40))
    paper = Image.composite(ink_rgb, paper, ink)

    # The cutter leaves a ragged top and bottom edge
    alpha = Image.new("L", (width, height), 255)
    a = ImageDraw.Draw(alpha)
    for edge in (0, height - 1):
        x = 0
        while x < width:
            step = rng.randint(6, 14)
            depth = rng.randint(1, 5)
            if edge == 0:
                a.polygon([(x, 0), (x + step, 0), (x + step / 2, depth)], fill=0)
            else:
                a.polygon([(x, edge), (x + step, edge), (x + step / 2, edge - depth)], fill=0)
            x += step
    paper.putalpha(alpha)
    return paper


def _wrap(s: str, f, width: float) -> list[str]:
    words, out, cur = s.split(), [], ""
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    for w in words:
        trial = f"{cur} {w}".strip()
        if probe.textlength(trial, font=f) <= width or not cur:
            cur = trial
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out or [""]


# The camera

_TABLES = [
    ((122, 84, 52), (94, 62, 38)),    # varnished wood
    ((150, 112, 74), (121, 86, 54)),  # pale wood
    ((168, 172, 166), (140, 146, 140)),  # grey counter top
    ((96, 120, 112), (74, 96, 90)),   # green formica
    ((190, 186, 176), (162, 158, 148)),  # cream laminate
]


def _table(size: tuple[int, int], cam: random.Random) -> Image.Image:
    """A counter or table top: a base colour with grain that runs one way."""
    w, h = size
    light, dark = cam.choice(_TABLES)
    vertical = cam.random() < 0.5
    across, along = (w, h) if vertical else (h, w)
    # Grain: noise across the board, stretched far along it, with a slow wave
    fine = Image.frombytes("L", (across // 3, 6), cam.randbytes((across // 3) * 6))
    fine = fine.resize((across, along), Image.BICUBIC)
    broad = Image.frombytes("L", (max(2, across // 60), 3), cam.randbytes(max(2, across // 60) * 3))
    broad = broad.resize((across, along), Image.BICUBIC)
    mix = Image.blend(broad, fine, 0.45)
    if not vertical:
        mix = mix.transpose(Image.Transpose.ROTATE_90)
    mix = mix.resize(size)
    table = Image.composite(Image.new("RGB", size, light), Image.new("RGB", size, dark), mix)
    noise = Image.frombytes("L", (w // 2, h // 2), cam.randbytes((w // 2) * (h // 2)))
    noise = noise.resize(size, Image.BILINEAR).point(lambda v: v * 14 // 255)
    return ImageChops.add(table, Image.merge("RGB", (noise, noise, noise)), 1.0, -7)


def _photograph(paper: Image.Image, cam: random.Random, *, tilt: float | None = None, light: str = "") -> bytes:
    fw, fh = PHOTO
    pad = int(fw * 0.12)
    canvas_size = (fw + 2 * pad, fh + 2 * pad)
    canvas = _table(canvas_size, cam)

    # Fit the slip to the frame, then tilt it on the table
    scale = min(fw * cam.uniform(0.68, 0.8) / paper.width, fh * cam.uniform(0.84, 0.92) / paper.height)
    angle = cam.uniform(-3.0, 3.0)
    if tilt is not None:
        angle = tilt
        t = math.radians(abs(tilt))  # a steeper slip shrinks so its corners stay in the frame
        scale = min(scale, fw * 0.9 / (paper.width * math.cos(t) + paper.height * math.sin(t)),
                    fh * 0.93 / (paper.height * math.cos(t) + paper.width * math.sin(t)))
    slip = paper.resize((int(paper.width * scale), int(paper.height * scale)), Image.LANCZOS)
    slip = slip.rotate(angle, Image.BICUBIC, expand=True)
    x = (canvas_size[0] - slip.width) // 2 + int(cam.uniform(-0.05, 0.05) * fw)
    y = (canvas_size[1] - slip.height) // 2 + int(cam.uniform(-0.03, 0.03) * fh)
    shadow = Image.new("L", canvas_size, 0)
    shadow.paste(slip.getchannel("A"), (x + 6, y + 9))
    shadow = shadow.filter(ImageFilter.GaussianBlur(9)).point(lambda v: v * 0.45)
    canvas = Image.composite(Image.new("RGB", canvas_size, (20, 16, 12)), canvas, shadow)
    canvas.paste(slip, (x, y), slip)

    # Perspective: the phone leans a little (a keystone) and is never quite square
    k = cam.uniform(-0.03, 0.06) * fw
    j = lambda: cam.uniform(-0.012, 0.012) * fw
    quad = [(pad + k + j(), pad + j()), (pad + fw - k + j(), pad + j()),
            (pad + fw + j(), pad + fh + j()), (pad + j(), pad + fh + j())]
    photo = canvas.transform(PHOTO, Image.PERSPECTIVE, _coeffs(quad, PHOTO), Image.BICUBIC)

    # Uneven light: a window on one side, a vignette, and sometimes the phone's own shadow
    glow = Image.new("L", (6, 8))
    gx, gy = cam.uniform(-1, 1), cam.uniform(-1, 1)
    vals = []
    for yy in range(8):
        for xx in range(6):
            u, v = xx / 5 - 0.5, yy / 7 - 0.5
            lin = 0.5 + 0.5 * (gx * u + gy * v)
            vignette = 1 - 0.9 * (u * u + v * v)
            vals.append(int(255 * min(1.0, (0.70 + 0.30 * lin) * vignette + 0.08)))
    glow.putdata(vals)
    glow = glow.resize(PHOTO, Image.BICUBIC)
    if cam.random() < 0.4:
        blot = Image.new("L", PHOTO, 255)
        bx, by = cam.uniform(0.1, 0.9) * fw, cam.uniform(0.75, 1.1) * fh
        ImageDraw.Draw(blot).ellipse([bx - fw * 0.45, by - fh * 0.2, bx + fw * 0.45, by + fh * 0.2], fill=200)
        glow = ImageChops.multiply(glow, blot.filter(ImageFilter.GaussianBlur(60)))
    tint = (cam.uniform(0.94, 1.0), cam.uniform(0.94, 1.0), cam.uniform(0.88, 1.0))
    if light == "dim":  # an evening shop: less light, so the phone lifts the gain and the noise
        tint = tuple(t * 0.62 for t in tint)
    elif light == "warm":  # a bulb in place of daylight
        tint = (tint[0], tint[1] * 0.86, tint[2] * 0.66)
    elif light == "shade":  # a hand or the phone casts a hard shadow across part of the slip
        hand = Image.new("L", PHOTO, 255)
        x0, y1 = cam.uniform(0.2, 0.6) * fw, cam.uniform(0.35, 0.7) * fh
        ImageDraw.Draw(hand).polygon([(x0, -20), (fw + 20, -20), (fw + 20, y1), (x0 + fw * 0.3, y1 + fh * 0.15)],
                                     fill=125)
        glow = ImageChops.multiply(glow, hand.filter(ImageFilter.GaussianBlur(16)))
    lit = Image.merge("RGB", tuple(glow.point(lambda v, t=t: int(v * t)) for t in tint))
    photo = ImageChops.multiply(photo, lit)
    if light == "bright":  # sun through the door washes the paper out
        photo = Image.blend(photo, Image.new("RGB", PHOTO, (255, 255, 250)), 0.24)

    # Sensor noise, a little softness, and the phone's JPEG
    amp = cam.randint(8, 16)
    if light == "dim":
        amp = amp * 2 + 6
    noise = Image.frombytes("L", PHOTO, cam.randbytes(fw * fh)).point(lambda v: v * amp // 255)
    photo = ImageChops.add(photo, Image.merge("RGB", (noise, noise, noise)), 1.0, -(amp // 2))
    photo = photo.filter(ImageFilter.GaussianBlur(cam.uniform(0.45, 1.0)))
    buf = io.BytesIO()
    photo.save(buf, "JPEG", quality=cam.randint(72, 86), optimize=True)
    return buf.getvalue()


def _coeffs(quad, size):
    """Coefficients that map the output frame onto a quad of the source (Image.PERSPECTIVE)."""
    w, h = size
    dst = [(0, 0), (w, 0), (w, h), (0, h)]
    rows, rhs = [], []
    for (x, y), (u, v) in zip(dst, quad):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        rhs.append(u)
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        rhs.append(v)
    return _solve(rows, rhs)


def _solve(a, b):
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


# Receipts from the shop's own data: last week's history and the sandbox samples

GOODS = Path(__file__).resolve().parent / "assets" / "goods"
PACKS = 4  # a regular order: four reorder packs
VARIANTS = ("genuine", "reused", "inflated", "wrong_payee", "stale")


def slug(item: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", item.lower()).strip("-")


def goods_photo(item: str) -> bytes | None:
    path = GOODS / f"{slug(item)}.jpg"
    return path.read_bytes() if path.is_file() else None


@functools.lru_cache(maxsize=16)
def earlier_goods_photo(item: str) -> bytes | None:
    """Last week's photo of the same goods, for the shop's history: the stock photo seen from the other
    side, framed tighter and in dimmer light. It reads as another photo, so the sandbox's own goods photo
    passes the reuse check and a reused one is still caught."""
    data = goods_photo(item)
    if data is None:
        return None
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.mirror(im.convert("RGB"))
        w, h = im.size
        im = im.crop((round(w * .06), round(h * .04), round(w * .97), round(h * .98)))
        im = ImageEnhance.Brightness(im.rotate(-2.5, resample=Image.BICUBIC)).enhance(0.92)
        im = im.resize((768, round(768 * im.height / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=84, optimize=True)
        return buf.getvalue()


def address_for(shop, payee: str) -> str:
    stand = zlib.crc32(payee.lower().encode()) % 60 + 1
    return f"Stand {stand}, {shop.place}"


def label_for(product, item: str, unit: str) -> str:
    return f"{item} ({unit})" if product is not None or unit not in ("", "units") else item


def regular_buy(session, shop):
    """The shop's biggest regular buy (a reorder pack at its latest price) and that latest purchase."""
    from sqlalchemy import select

    from .models import PricePoint, Product

    best = None
    for product in session.scalars(select(Product).where(Product.shop_id == shop.id).order_by(Product.id)):
        point = session.scalar(select(PricePoint).where(
            PricePoint.shop_id == shop.id, PricePoint.item == product.name,
            PricePoint.supplier == product.usual_payee).order_by(PricePoint.bought_at.desc()))
        if point is None:
            continue
        value = product.pack * point.unit_cents
        if best is None or value > best[0]:
            best = (value, product, point)
    return best and best[1:]


def _till(shop, payee: str) -> int:
    """Where this payee's receipt numbers stood last week."""
    return 4000 + zlib.crc32(f"{shop.id}:{payee}".encode()) % 5000


def history_spec(shop, product, point, quantity: int, unit_cents: int, back: int = 0) -> ReceiptSpec:
    """A receipt from the shop's history. `back` counts receipts back from where the payee's till
    stood last week, for its earlier slips."""
    from .flows import utc
    from .proof import SHOP_TZ

    day = utc(point.bought_at).astimezone(SHOP_TZ)
    n = _till(shop, point.supplier) - back
    at = datetime(day.year, day.month, day.day, 9, n % 50)
    return ReceiptSpec(payee=point.supplier, address=address_for(shop, point.supplier),
                       receipt_no=receipt_number(point.supplier, n), at=at,
                       lines=(Line(label_for(product, product.name, product.unit), quantity, unit_cents),))


def sample_spec(session, req, variant: str) -> ReceiptSpec:
    """A receipt for this request as the manager might send it: genuine, or wrong in one way."""
    from datetime import timedelta

    from sqlalchemy import select

    from .flows import now, utc
    from .models import PricePoint, Product
    from .proof import SHOP_TZ, same_payee, usual_price

    shop = req.shop
    product = session.scalar(select(Product).where(Product.shop_id == shop.id, Product.name == req.item))
    payee = req.payee or (product.usual_payee if product else "")
    if not payee:
        supplier = session.scalar(select(PricePoint.supplier).where(
            PricePoint.shop_id == shop.id, PricePoint.item == req.item).order_by(PricePoint.bought_at.desc()))
        payee = supplier or f"{shop.place.split(',')[0]} traders"
    held = req.hold.amount_cents if req.hold else req.amount_cents
    qty = max(1, req.quantity)
    usual = usual_price(session, shop.id, req.item, payee, now())
    base = min(usual[0], held // qty) if usual else held // qty
    unit = base * 96 // 100 // 5 * 5  # a little under the usual, in five-cent steps
    at = now().astimezone(SHOP_TZ).replace(tzinfo=None, second=0, microsecond=0)
    if variant not in VARIANTS or variant == "reused":
        raise ValueError(f"A sample is one of {', '.join(VARIANTS)}.")
    n = 100 + req.id * len(VARIANTS) + VARIANTS.index(variant)  # numbers move on after last week's
    if variant == "inflated":
        unit = -(-base * 128 // 100 // 5) * 5
    elif variant == "wrong_payee":
        others = [p.usual_payee for p in session.scalars(select(Product).where(Product.shop_id == shop.id))]
        others += list(session.scalars(select(PricePoint.supplier).where(PricePoint.shop_id == shop.id)))
        other = next((o for o in others if o and not same_payee(payee, o) and not same_payee(o, payee)), None)
        payee = other or f"{shop.place.split(',')[0]} traders"
    elif variant == "stale":
        approved = utc(req.approved_at or now()).astimezone(SHOP_TZ)
        at = (approved - timedelta(days=9)).replace(tzinfo=None, hour=11, minute=n % 50, second=0, microsecond=0)
    return ReceiptSpec(payee=payee, address=address_for(shop, payee),
                       receipt_no=receipt_number(payee, _till(shop, payee) + n),
                       at=at, lines=(Line(label_for(product, req.item, req.unit), qty, max(5, unit)),))
