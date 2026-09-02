"""Traducao de footprints do KiCad para padroes de furos numa perfboard 0.1" (2.54mm).

Cada footprint vira um conjunto de offsets inteiros (dx, dy) em furos, relativo
a origem do componente. Quando o footprint real nao encaixa na grade (SMD, pitch
fracionario), geramos o padrao mais proximo e devolvemos um aviso.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

PITCH_MM = 2.54

SMD_HINTS = (
    "SMD", "SOIC", "SSOP", "TSSOP", "MSOP", "QFN", "QFP", "DFN", "BGA",
    "SOT-23", "SOT-323", "SOT-363", "0402", "0603", "0805", "1206", "1210",
    "Handsolder",
)


@dataclass
class FootprintDef:
    """Padrao de furos de um componente."""
    key: str                                  # string original do footprint
    label: str                                # nome curto legivel
    pins: dict = field(default_factory=dict)  # numero do pino -> (dx, dy)
    warnings: list = field(default_factory=list)
    inferred: bool = True                     # False quando veio de override do usuario

    # Quanto o CORPO da peca passa alem do retangulo dos pinos, em furos, na
    # orientacao original: (esquerda, cima, direita, baixo). Um borne Phoenix,
    # por exemplo, tem os terminais na frente e o plastico avancando para tras -
    # sem isso o sistema deixaria outra peca encostar onde ha corpo.
    margins: tuple = (0, 0, 0, 0)
    body_note: str = ""                       # de onde veio a medida, para a interface
    pin_note: str = ""                        # idem, para o afastamento dos terminais

    @property
    def extent(self):
        """(min_dx, min_dy, max_dx, max_dy) dos furos com pino."""
        if not self.pins:
            return (0, 0, 0, 0)
        xs = [p[0] for p in self.pins.values()]
        ys = [p[1] for p in self.pins.values()]
        return (min(xs), min(ys), max(xs), max(ys))

    @property
    def size(self):
        x0, y0, x1, y1 = self.extent
        return (x1 - x0 + 1, y1 - y0 + 1)

    @property
    def body_extent(self):
        """(min_dx, min_dy, max_dx, max_dy) do corpo, ja com as folgas."""
        x0, y0, x1, y1 = self.extent
        l, t, r, b = self.margins
        return (x0 - l, y0 - t, x1 + r, y1 + b)

    @property
    def body_size(self):
        x0, y0, x1, y1 = self.body_extent
        return (x1 - x0 + 1, y1 - y0 + 1)

    def to_json(self):
        return {
            "key": self.key,
            "label": self.label,
            "pins": {k: list(v) for k, v in self.pins.items()},
            "warnings": list(self.warnings),
            "inferred": self.inferred,
            "size": list(self.size),
            "margins": list(self.margins),
            "body_size": list(self.body_size),
            "body_mm": [round(self.body_size[0] * PITCH_MM, 1),
                        round(self.body_size[1] * PITCH_MM, 1)],
            "body_note": self.body_note,
            "pin_note": self.pin_note,
            "arranjo": arranjo_dos_pinos(self.pins),
        }


def holes_from_mm(mm: float, minimum: int = 1):
    """Converte um pitch em mm para numero inteiro de furos. Retorna (furos, erro_mm)."""
    exact = mm / PITCH_MM
    h = max(minimum, int(round(exact)))
    return h, abs(exact - h) * PITCH_MM


def _pitch_from_name(name: str):
    m = re.search(r"_P(\d+(?:\.\d+)?)mm", name)
    return float(m.group(1)) if m else None


# Folga entre a ponta do corpo e o furo, de cada lado, para a dobra de 90 graus
# feita a mao. Menos que isso e o terminal sai forcando do corpo.
FOLGA_DOBRA_MM = 1.9

# Peca axial: terminais saem das PONTAS e precisam ser dobrados para descer ate a
# placa. Peca radial (disco, eletrolitico, LED) ja tem os terminais para baixo e
# nao entra nesta conta - alargar o passo dela so afastaria os furos a toa.
AXIAIS = ("R_Axial", "C_Axial", "L_Axial", "D_DO", "D_A-405", "Varistor")


def _passo_axial(name: str, passo_nominal: int):
    """Passo minimo que deixa a peca axial assentar, em furos.

    Sai do comprimento do corpo no proprio nome do footprint (`_L6.3mm`) mais a
    folga da dobra dos dois lados. Sem essa medida no nome, usa 4 furos: e o que
    assenta o resistor de 1/4W, que e a peca axial que aparece em quase todo
    circuito.
    """
    corpo = re.search(r"_L(\d+(?:\.\d+)?)mm", name)
    if corpo:
        preciso = float(corpo.group(1)) + 2 * FOLGA_DOBRA_MM
        minimo = max(1, int(round(preciso / PITCH_MM)))
    else:
        minimo = 4
    return max(passo_nominal, minimo)


def _row(pins, pitch_holes, axis="x"):
    """Distribui os pinos em linha, com o passo dado."""
    out = {}
    for i, p in enumerate(pins):
        out[p] = (i * pitch_holes, 0) if axis == "x" else (0, i * pitch_holes)
    return out


def _mm_no_nome(name: str, letra: str):
    """Extrai medidas do tipo _D5.0mm, _L6.3mm, _W2.5mm do nome do footprint."""
    m = re.search(r"_%s(\d+(?:\.\d+)?)mm" % letra, name)
    return float(m.group(1)) if m else None


def _folgas_para(largura_furos: int, altura_furos: int, span_x: int, span_y: int,
                 extra_tras: int = 0):
    """Converte um tamanho de corpo desejado em folgas ao redor do retangulo dos pinos.

    As folgas sao SIMETRICAS: o corpo de uma peca e centrado nos terminais dela, e
    sobra impar arredonda para cima dos dois lados. A versao anterior jogava a sobra
    impar toda para um lado, com a ideia de que peca THT "avanca para tras" - o
    resultado ficava visivelmente torto e nao correspondia a peca real. Quando um
    componente de fato avanca so para um lado, isso se ajusta caso a caso na
    interface, nao como regra geral.
    """
    esq = dir_ = max(0, largura_furos - span_x + 1) // 2
    folga_y = max(0, altura_furos - span_y) + extra_tras
    cima = baixo = (folga_y + 1) // 2
    return (esq, cima, dir_, baixo)


def _corpo_pelo_nome(name: str, d: FootprintDef):
    """Aplica ao footprint o tamanho real do corpo, quando o nome informa."""
    if not d.pins:
        return
    span_x, span_y = d.size

    diam = _mm_no_nome(name, "D")
    comp = _mm_no_nome(name, "L")
    larg = _mm_no_nome(name, "W")

    if comp and diam:                       # axial deitado: L ao longo, D de espessura
        lx = max(span_x, int(math.ceil(comp / PITCH_MM)))
        ly = max(span_y, int(math.ceil(diam / PITCH_MM)))
        d.margins = _folgas_para(lx, ly, span_x, span_y)
        d.body_note = "corpo %.1f x %.1f mm, do nome do footprint" % (comp, diam)
    elif diam and larg:
        # Disco em pe (C_Disc_D5.0mm_W2.5mm): D e o diametro, no sentido dos
        # terminais, e W e a espessura, perpendicular. Usar D nos dois eixos
        # inflaria a peca em 50%.
        lx = max(span_x, int(math.ceil(diam / PITCH_MM)))
        ly = max(span_y, int(math.ceil(larg / PITCH_MM)))
        d.margins = _folgas_para(lx, ly, span_x, span_y)
        d.body_note = "disco de %.1f mm, %.1f mm de espessura, do nome do footprint" % (diam, larg)
    elif diam:                              # radial/LED: circulo de diametro D
        n = max(1, int(math.ceil(diam / PITCH_MM)))
        d.margins = _folgas_para(max(span_x, n), max(span_y, n), span_x, span_y)
        d.body_note = "corpo redondo de %.1f mm, do nome do footprint" % diam
    elif larg:
        ly = max(span_y, int(math.ceil(larg / PITCH_MM)))
        d.margins = _folgas_para(span_x, ly, span_x, span_y)
        d.body_note = "largura %.1f mm, do nome do footprint" % larg
    elif re.search(r"TerminalBlock|Screw", name, re.I):
        # Borne de 5,08 mm (MKDS-1,5 e equivalentes): ~12,5 mm de profundidade e
        # sobra de um furo de cada lado dos parafusos. Corpo centrado nos terminais.
        d.margins = _folgas_para(span_x + 2, span_y + 4, span_x, span_y)
        d.body_note = ("borne 5,08 mm: ~12,7 x 12,7 mm (medida tipica). "
                       "Se o seu for maior, ajuste aqui")
    elif re.search(r"TO-?220|TO-?126", name, re.I):
        d.margins = _folgas_para(max(span_x, 4), max(span_y, 2), span_x, span_y)
        d.body_note = "TO-220 sem dissipador; se usar dissipador, aumente a profundidade"
    elif re.search(r"Potentiometer|Trimmer|_RV|Poti", name, re.I):
        d.margins = _folgas_para(max(span_x, 3), max(span_y, 3), span_x, span_y)
        d.body_note = "trimpot/potenciometro, medida tipica"


def infer(footprint: str, pin_numbers, ref: str = "") -> FootprintDef:
    """Deduz onde ficam os pinos e qual e o tamanho real do corpo."""
    d = _infer_pins(footprint, pin_numbers, ref)
    _corpo_pelo_nome((footprint or "").split(":", 1)[-1], d)
    return d


def _infer_pins(footprint: str, pin_numbers, ref: str = "") -> FootprintDef:
    """Deduz o padrao de furos a partir da string de footprint e da lista de pinos."""
    fp = footprint or ""
    name = fp.split(":", 1)[-1]
    pins = list(pin_numbers)
    d = FootprintDef(key=fp, label=name or "generico")

    if not pins:
        d.warnings.append("componente sem pinos na netlist")
        return d

    for smd in SMD_HINTS:
        if smd.lower() in name.lower():
            d.warnings.append(
                "footprint '%s' e SMD ou de pitch fino: nao encaixa direto na perfboard. "
                "Gerei um padrao em linha - use adaptador ou troque o footprint." % name
            )
            d.pins = _row(pins, 1)
            d.label = (name or "SMD") + " (adaptado)"
            return d

    # --- DIP ---
    m = re.search(r"DIP-(\d+)", name, re.I)
    if m:
        width_mm = 7.62
        mw = re.search(r"_W(\d+(?:\.\d+)?)mm", name)
        if mw:
            width_mm = float(mw.group(1))
        cols, err = holes_from_mm(width_mm, minimum=1)
        if err > 0.35:
            d.warnings.append("largura do DIP (%.2fmm) arredondada para %d furos" % (width_mm, cols))
        n = len(pins)
        half = n // 2
        out = {}
        for i in range(half):
            out[pins[i]] = (0, i)
        for i in range(half, n):
            out[pins[i]] = (cols, n - 1 - i)
        d.pins = out
        d.label = "DIP-%d (%d furos de largura)" % (n, cols + 1)
        return d

    # --- Pin headers / conectores em grade ---
    m = re.search(r"(\d+)x(\d+)", name)
    if m and ("header" in name.lower() or "socket" in name.lower() or "conn" in fp.lower()):
        a, b = int(m.group(1)), int(m.group(2))
        pitch = _pitch_from_name(name) or 2.54
        step, err = holes_from_mm(pitch)
        if err > 0.35:
            d.warnings.append("pitch %.2fmm do conector nao e multiplo de 2.54mm" % pitch)
        out = {}
        if a == 1:
            for i, p in enumerate(pins[: a * b]):
                out[p] = (0, i * step)
        else:
            # KiCad numera 2xN em ziguezague: impares numa coluna, pares na outra
            for i, p in enumerate(pins[: a * b]):
                out[p] = ((i % a) * step, (i // a) * step)
        for p in pins[a * b:]:
            out[p] = (0, len(out))
        d.pins = out
        d.label = "Header %dx%d" % (a, b)
        return d

    # --- 2 terminais axiais/radiais (R, C, D, LED, indutor) ---
    two_pin_hint = any(k in name for k in (
        "R_Axial", "C_Disc", "C_Rect", "CP_Radial", "C_Axial", "D_DO", "D_A-405",
        "LED_D", "L_Axial", "L_Radial", "Varistor", "R_Box", "R_Bare",
    ))
    if len(pins) == 2 and (two_pin_hint or _pitch_from_name(name)):
        pitch = _pitch_from_name(name) or 2.54
        step, err = holes_from_mm(pitch)
        if err > 0.35:
            d.warnings.append(
                "pitch %.2fmm nao cai na grade; usando %d furo(s) (%.2fmm) - dobre os terminais"
                % (pitch, step, step * PITCH_MM)
            )
        if any(k in name for k in AXIAIS):
            aberto = _passo_axial(name, step)
            if aberto != step:
                d.warnings.append(
                    "passo aberto de %d para %d furos: peca axial precisa de espaco para "
                    "dobrar o terminal. Ajuste no botao da lista se a sua for diferente."
                    % (step, aberto))
                step = aberto
        d.pins = {pins[0]: (0, 0), pins[1]: (step, 0)}
        d.label = "2 terminais, passo %d furo(s)" % step
        return d

    # --- TO-92 / TO-220 / transistores de 3 pernas ---
    if re.search(r"TO-?92|TO-?220|TO-?126|TO-?251|SOT-?223", name, re.I) or (
        len(pins) == 3 and re.search(r"transistor|regulator|TO-", fp, re.I)
    ):
        pitch = _pitch_from_name(name) or 2.54
        step, _ = holes_from_mm(pitch)
        d.pins = _row(pins[:3], step)
        for p in pins[3:]:
            d.pins[p] = (0, 1)
        d.label = "3 pernas em linha, passo %d furo(s)" % step
        if "TO-220" in name.upper():
            d.warnings.append("TO-220: confira a orientacao da aba metalica e o espaco do dissipador")
        return d

    # --- Bornes / terminal blocks ---
    if re.search(r"TerminalBlock|Screw", name, re.I):
        pitch = _pitch_from_name(name) or 5.08
        step, err = holes_from_mm(pitch, minimum=2)
        if err > 0.35:
            d.warnings.append("borne com pitch %.2fmm aproximado para %d furos" % (pitch, step))
        d.pins = _row(pins, step)
        d.label = "Borne, passo %d furos" % step
        return d

    # --- Generico: usa o pitch do nome, senao 1 furo ---
    pitch = _pitch_from_name(name)
    if pitch:
        step, err = holes_from_mm(pitch)
        if err > 0.35:
            d.warnings.append("pitch %.2fmm aproximado para %d furo(s)" % (pitch, step))
    else:
        step = 1
        if fp:
            d.warnings.append("footprint '%s' desconhecido: assumindo pinos em linha com passo 2.54mm" % name)
        else:
            d.warnings.append("componente sem footprint na netlist: assumindo pinos em linha com passo 2.54mm")

    if len(pins) > 8:
        # muitos pinos numa linha so fica impraticavel; distribui em duas fileiras
        half = math.ceil(len(pins) / 2)
        out = {}
        for i, p in enumerate(pins):
            out[p] = ((i % half) * step, (i // half) * step)
        d.pins = out
        d.label = "%d pinos em 2 fileiras" % len(pins)
    else:
        d.pins = _row(pins, step)
        d.label = "%d pinos em linha" % len(pins)
    return d


def arranjo_dos_pinos(pins: dict) -> dict:
    """Le o padrao como uma pessoa leria: fileiras, passo e largura.

    `tipo`:
      "linha"    - todos os terminais numa fileira so (resistor, capacitor, TO-92)
      "fileiras" - duas fileiras paralelas (DIP, barra 2xN)
      "irregular"- qualquer outra coisa; a interface nao oferece ajuste, porque
                   mexer no passo geraria um desenho que nao corresponde a peca
                   nenhuma

    `eixo` diz para que lado a fileira corre ("x" ou "y"). Precisa existir porque
    um DIP deduzido tem as duas fileiras separadas em x, correndo em y - e um
    header 2xN pode vir do outro jeito.
    """
    def regular(vals):
        vals = sorted(vals)
        vaos = {b - a for a, b in zip(vals, vals[1:])}
        return len(vaos) <= 1

    def passo_de(vals):
        vals = sorted(vals)
        if len(vals) < 2:
            return 1
        return max(1, (vals[-1] - vals[0]) // (len(vals) - 1))

    vazio = {"tipo": "irregular", "eixo": "x", "passo": 1, "largura": 0,
             "fileiras": 1, "vao_total": 0}
    if len(pins) < 2:
        return vazio

    xs = sorted({dx for dx, _dy in pins.values()})
    ys = sorted({dy for _dx, dy in pins.values()})

    # uma fileira so
    if len(ys) == 1:
        return {"tipo": "linha" if regular(xs) else "irregular", "eixo": "x",
                "passo": passo_de(xs), "largura": 0, "fileiras": 1,
                "vao_total": xs[-1] - xs[0]}
    if len(xs) == 1:
        return {"tipo": "linha" if regular(ys) else "irregular", "eixo": "y",
                "passo": passo_de(ys), "largura": 0, "fileiras": 1,
                "vao_total": ys[-1] - ys[0]}

    # duas fileiras: o eixo com DOIS valores distintos e o que as separa
    for eixo, sep, corre in (("y", xs, ys), ("x", ys, xs)):
        if len(sep) != 2:
            continue
        grupos = {}
        for dx, dy in pins.values():
            chave = dx if eixo == "y" else dy
            grupos.setdefault(chave, []).append(dy if eixo == "y" else dx)
        iguais = len({len(v) for v in grupos.values()}) == 1
        todas_regulares = all(regular(v) for v in grupos.values())
        return {"tipo": "fileiras" if (iguais and todas_regulares) else "irregular",
                "eixo": eixo, "passo": passo_de(corre),
                "largura": sep[1] - sep[0], "fileiras": 2,
                "vao_total": corre[-1] - corre[0]}

    return dict(vazio, fileiras=len(ys))


def redistribui_pinos(d: FootprintDef, passo=None, largura=None) -> bool:
    """Reposiciona os terminais com outro passo, mantendo a numeracao.

    Cada pino guarda seu lugar (que fileira, que posicao dentro dela) e so o
    espacamento muda - assim o pino 1 continua sendo o pino 1. Devolve False para
    padrao irregular, onde nao ha passo unico que faca sentido.
    """
    arranjo = arranjo_dos_pinos(d.pins)
    if arranjo["tipo"] == "irregular":
        return False

    passo = arranjo["passo"] if passo is None else max(1, min(40, int(passo)))
    largura = arranjo["largura"] if largura is None else max(0, min(40, int(largura)))
    ao_longo_de_x = arranjo["eixo"] == "x"

    # (valor que separa as fileiras) -> pinos daquela fileira, em ordem
    grupos = {}
    for pino, (dx, dy) in d.pins.items():
        chave = dy if ao_longo_de_x else dx
        grupos.setdefault(chave, []).append((dx if ao_longo_de_x else dy, pino))

    novos = {}
    for i_fileira, chave in enumerate(sorted(grupos)):
        for i_pos, (_v, pino) in enumerate(sorted(grupos[chave])):
            desloca = i_fileira * largura
            novos[pino] = ((i_pos * passo, desloca) if ao_longo_de_x
                           else (desloca, i_pos * passo))
    d.pins = novos
    return True


def aplica_override(d: FootprintDef, spec: dict) -> FootprintDef:
    """Aplica ajustes do usuario por cima do que foi deduzido.

    Aceita mexer so no corpo (`margins`) mantendo os pinos deduzidos, que e o caso
    comum: o footprint acerta os terminais e erra o tamanho da peca.
    """
    if not spec:
        return d

    pins = spec.get("pins")
    if pins:
        d.pins = {str(pin): (int(off[0]), int(off[1])) for pin, off in pins.items()}
        d.inferred = False

    # Passo dos terminais. Vem antes das margens de proposito: o corpo e medido a
    # partir do retangulo dos pinos, entao mexer no passo depois moveria o corpo junto.
    if spec.get("passo") is not None or spec.get("largura") is not None:
        try:
            mexeu = redistribui_pinos(d, spec.get("passo"), spec.get("largura"))
        except (TypeError, ValueError):
            mexeu = False
        if mexeu:
            d.inferred = False
            d.pin_note = "afastamento ajustado por voce"

    margens = spec.get("margins")
    if margens is not None:
        try:
            l, t, r, b = (max(0, min(30, int(v))) for v in margens)
        except (TypeError, ValueError):
            return d
        d.margins = (l, t, r, b)
        d.body_note = "tamanho ajustado por voce"
        d.inferred = False

    if spec.get("label"):
        d.label = str(spec["label"])
    return d


def from_override(spec: dict, footprint: str = "") -> FootprintDef:
    """Cria um padrao inteiramente a partir de um override."""
    d = FootprintDef(key=footprint, label=spec.get("label", "personalizado"), inferred=False)
    return aplica_override(d, spec)


def build_library(netlist, overrides=None) -> dict:
    """ref -> FootprintDef para todos os componentes da netlist."""
    overrides = overrides or {}
    lib = {}
    for ref, comp in netlist.components.items():
        d = infer(comp.footprint, comp.pins, ref)
        lib[ref] = aplica_override(d, overrides.get(ref))
    return lib
