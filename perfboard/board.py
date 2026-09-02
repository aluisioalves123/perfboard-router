"""Modelo geometrico da perfboard: grade de furos, posicionamento e ocupacao."""
from __future__ import annotations

from dataclasses import dataclass, field

PITCH_MM = 2.54
ROTATIONS = (0, 90, 180, 270)


def rotate(dx: int, dy: int, rot: int):
    """Rotaciona um offset em furos. Y cresce para baixo (como na tela)."""
    rot = rot % 360
    if rot == 0:
        return (dx, dy)
    if rot == 90:
        return (-dy, dx)
    if rot == 180:
        return (-dx, -dy)
    if rot == 270:
        return (dy, -dx)
    raise ValueError("rotacao invalida: %r" % rot)


@dataclass
class BoardSpec:
    """Dimensoes da placa, em furos."""
    cols: int
    rows: int
    pitch_mm: float = PITCH_MM
    margin_holes: int = 0   # furos de borda reservados (parafusos, corte)

    @property
    def width_mm(self):
        return (self.cols - 1) * self.pitch_mm

    @property
    def height_mm(self):
        return (self.rows - 1) * self.pitch_mm

    def __post_init__(self):
        self._recalcula_limites()

    def _recalcula_limites(self):
        """Limites uteis em atributos simples.

        `contains` e chamado milhoes de vezes por posicionamento; recalcular a tupla
        a cada chamada era ~15% do tempo total.
        """
        m = self.margin_holes
        self._x0, self._y0 = m, m
        self._x1, self._y1 = self.cols - 1 - m, self.rows - 1 - m

    def usable_bounds(self):
        return (self._x0, self._y0, self._x1, self._y1)

    def contains(self, c: int, r: int) -> bool:
        return self._x0 <= c <= self._x1 and self._y0 <= r <= self._y1

    def holes(self):
        x0, y0, x1, y1 = self.usable_bounds()
        for r in range(y0, y1 + 1):
            for c in range(x0, x1 + 1):
                yield (c, r)

    def to_json(self):
        return {
            "cols": self.cols,
            "rows": self.rows,
            "pitch_mm": self.pitch_mm,
            "margin_holes": self.margin_holes,
            "width_mm": round(self.width_mm, 2),
            "height_mm": round(self.height_mm, 2),
        }

    @staticmethod
    def from_json(d: dict) -> "BoardSpec":
        return BoardSpec(
            cols=int(d.get("cols", 24)),
            rows=int(d.get("rows", 18)),
            pitch_mm=float(d.get("pitch_mm", PITCH_MM)),
            margin_holes=int(d.get("margin_holes", 0)),
        )


@dataclass
class Placement:
    ref: str
    col: int = 0
    row: int = 0
    rot: int = 0
    locked: bool = False

    def to_json(self):
        return {"ref": self.ref, "col": self.col, "row": self.row,
                "rot": self.rot, "locked": self.locked}

    @staticmethod
    def from_json(d: dict) -> "Placement":
        return Placement(
            ref=d["ref"], col=int(d.get("col", 0)), row=int(d.get("row", 0)),
            rot=int(d.get("rot", 0)) % 360, locked=bool(d.get("locked", False)),
        )


class Layout:
    """Perfboard + componentes posicionados. Nao conhece roteamento."""

    def __init__(self, spec: BoardSpec, footprints: dict, placements: dict | None = None):
        self.spec = spec
        self.footprints = footprints              # ref -> FootprintDef
        self.placements = placements or {}        # ref -> Placement

    # ---------- geometria ----------

    def pin_holes(self, ref: str) -> dict:
        """pino -> (col, row) na grade da placa."""
        pl = self.placements.get(ref)
        fp = self.footprints.get(ref)
        if pl is None or fp is None:
            return {}
        out = {}
        for pin, (dx, dy) in fp.pins.items():
            rx, ry = rotate(dx, dy, pl.rot)
            out[pin] = (pl.col + rx, pl.row + ry)
        return out

    def cells(self, ref: str) -> set:
        return set(self.pin_holes(ref).values())

    def bbox(self, ref: str):
        cs = self.cells(ref)
        if not cs:
            return None
        xs = [c for c, _ in cs]
        ys = [r for _, r in cs]
        return (min(xs), min(ys), max(xs), max(ys))

    def body_bbox(self, ref: str):
        """Retangulo do CORPO da peca na placa, ja rotacionado.

        Difere do retangulo dos pinos: um borne tem os terminais na frente e o
        plastico avancando para tras, e e o corpo que impede outra peca de ocupar
        aquele espaco.
        """
        pl = self.placements.get(ref)
        fp = self.footprints.get(ref)
        if pl is None or fp is None or not fp.pins:
            return None
        x0, y0, x1, y1 = fp.body_extent
        cantos = [rotate(x0, y0, pl.rot), rotate(x1, y0, pl.rot),
                  rotate(x0, y1, pl.rot), rotate(x1, y1, pl.rot)]
        xs = [c[0] for c in cantos]
        ys = [c[1] for c in cantos]
        return (pl.col + min(xs), pl.row + min(ys), pl.col + max(xs), pl.row + max(ys))

    def body_cells(self, ref: str) -> set:
        """Furos cobertos pelo corpo, usados para colisao entre componentes."""
        bb = self.body_bbox(ref)
        if bb is None:
            return set()
        x0, y0, x1, y1 = bb
        return {(c, r) for r in range(y0, y1 + 1) for c in range(x0, x1 + 1)}

    def pin_map(self) -> dict:
        """(col,row) -> (ref, pino). Se houver colisao, a ultima vence."""
        out = {}
        for ref in self.placements:
            for pin, cell in self.pin_holes(ref).items():
                out[cell] = (ref, pin)
        return out

    # ---------- validacao ----------

    def out_of_board(self, ref: str) -> int:
        return sum(0 if self.spec.contains(c, r) else 1 for c, r in self.cells(ref))

    def collisions(self):
        """Lista de (refA, refB, n_furos) com corpos sobrepostos."""
        refs = list(self.placements)
        bodies = {r: self.body_cells(r) for r in refs}
        out = []
        for i in range(len(refs)):
            for j in range(i + 1, len(refs)):
                inter = bodies[refs[i]] & bodies[refs[j]]
                if inter:
                    out.append((refs[i], refs[j], len(inter)))
        return out

    def body_off_board(self, ref: str) -> int:
        """Furos do corpo que passam da borda (o corpo pode 'sobrar' para fora).

        Isto NAO e defeito: borne, conector e potenciometro de borda sao montados
        justamente assim, com o corpo sobrando para fora da placa - e do lado de fora
        nao ha nada com que colidir. O posicionador usa este numero como custo leve
        (W_CORPO_FORA) para nao jogar peca para fora a toa, mas `problems()` nao
        reporta e o laco de busca nao trava por causa disso. Ja custou caro: com o
        corpo fora contando como problema, uma solucao 100% ligada era recusada para
        sempre quando o usuario travava os bornes na borda, e a busca nunca parava.
        """
        bb = self.body_bbox(ref)
        if bb is None:
            return 0
        return sum(0 if self.spec.contains(c, r) else 1 for c, r in self.body_cells(ref))

    def problems(self):
        """So o que impede a montagem: pino fora da placa e corpos sobrepostos.

        Corpo sobrando para a borda nao entra aqui - veja `body_off_board`.
        """
        msgs = []
        for ref in sorted(self.placements):
            n = self.out_of_board(ref)
            if n:
                msgs.append("%s: %d furo(s) fora da placa" % (ref, n))
        for a, b, n in self.collisions():
            msgs.append("%s e %s se sobrepoem em %d furo(s)" % (a, b, n))
        return msgs

    # ---------- serializacao ----------

    def to_json(self):
        return {
            "board": self.spec.to_json(),
            "placements": [p.to_json() for p in self.placements.values()],
            "footprints": {r: f.to_json() for r, f in self.footprints.items()},
            "pins": [
                {"ref": ref, "pin": pin, "col": cell[0], "row": cell[1]}
                for ref in sorted(self.placements)
                for pin, cell in sorted(self.pin_holes(ref).items())
            ],
        }


def row_letter(row: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA (como as marcacoes impressas na perfboard)."""
    if row < 0:
        return str(row)
    s = ""
    n = row
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def hole_label(col: int, row: int, style: str = "letra") -> str:
    """Nome do furo. 'letra' -> A1/R24 (linha em letra, coluna em numero); 'numerica' -> (col,row)."""
    if style == "letra":
        return "%s%d" % (row_letter(row), col + 1)
    return "(%d,%d)" % (col, row)
