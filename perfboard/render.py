"""Desenho da perfboard em SVG: vista do lado dos componentes e do lado da solda."""
from __future__ import annotations

import html

from .board import numeracao, rotulador, row_letter

PALETTE = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#008080",
    "#f032e6", "#9a6324", "#808000", "#00a3a3", "#bc5090", "#5a8f29",
]
GND_COLOR = "#3b3b3b"
PWR_COLOR = "#d33"
FAIL_COLOR = "#ff2d2d"


def net_color(name: str) -> str:
    n = (name or "").upper()
    if "GND" in n or n.endswith("VSS"):
        return GND_COLOR
    if any(k in n for k in ("VCC", "VDD", "+5V", "+3V3", "+12V", "VBUS", "VIN")):
        return PWR_COLOR
    return PALETTE[hash(name) % len(PALETTE)]


class SvgBoard:
    def __init__(self, spec, scale: float = 26.0, pad: float = 34.0, side: str = "top",
                 label_style: str = "letra"):
        self.spec = spec
        self.s = scale
        self.pad = pad
        self.side = side          # "top" = lado dos componentes, "bottom" = lado da solda
        self.label_style = label_style
        self.parts = []

    # --- coordenadas ---

    def x(self, col: float) -> float:
        c = (self.spec.cols - 1 - col) if self.side == "bottom" else col
        return self.pad + c * self.s

    def y(self, row: float) -> float:
        return self.pad + row * self.s

    @property
    def width(self):
        return self.pad * 2 + (self.spec.cols - 1) * self.s

    @property
    def height(self):
        return self.pad * 2 + (self.spec.rows - 1) * self.s

    # --- primitivas ---

    def add(self, s):
        self.parts.append(s)

    def render(self, title: str = "") -> str:
        body = "\n".join(self.parts)
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %.1f %.1f" '
            'width="%.1f" height="%.1f" font-family="ui-sans-serif,system-ui,sans-serif" '
            'data-side="%s" data-pad="%.2f" data-scale="%.2f" data-cols="%d" data-rows="%d">\n'
            '<title>%s</title>\n%s\n</svg>'
            % (self.width, self.height, self.width, self.height,
               self.side, self.pad, self.s, self.spec.cols, self.spec.rows,
               html.escape(title), body)
        )


def _hole_grid(sv):
    spec = sv.spec
    sv.add('<rect x="0" y="0" width="%.1f" height="%.1f" rx="6" fill="#f6efdf" stroke="#c9b98f"/>'
           % (sv.width, sv.height))
    dots = []
    for c in range(spec.cols):
        for r in range(spec.rows):
            dots.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="#fffdf7" stroke="#b9a97f" stroke-width="0.8"/>'
                        % (sv.x(c), sv.y(r), sv.s * 0.16))
    sv.add("\n".join(dots))

    labels = []
    letra = sv.label_style == "letra"
    origem = getattr(spec, "label_origin", "TL")
    # A regua mostra a numeracao IMPRESSA na placa. Se ela corre ao contrario,
    # e a etiqueta que muda de lugar - o furo continua onde esta.
    step_c = 1 if (letra and sv.s >= 18) else 5
    for c in range(0, spec.cols, step_c):
        nc, _ = numeracao(c, 0, spec.cols, spec.rows, origem)
        txt = str(nc + 1) if letra else str(nc)
        labels.append('<text x="%.1f" y="%.1f" font-size="9" fill="#8a7a55" text-anchor="middle">%s</text>'
                      % (sv.x(c), sv.pad - 10, txt))
    step_r = 1 if (letra and sv.s >= 16) else 5
    for r in range(0, spec.rows, step_r):
        _, nr = numeracao(0, r, spec.cols, spec.rows, origem)
        txt = row_letter(nr) if letra else str(nr)
        labels.append('<text x="%.1f" y="%.1f" font-size="9" fill="#8a7a55" text-anchor="end" dominant-baseline="middle">%s</text>'
                      % (sv.pad - 10, sv.y(r), txt))
    sv.add("\n".join(labels))

    if spec.margin_holes:
        m = spec.margin_holes
        sv.add('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="none" '
               'stroke="#c0392b" stroke-dasharray="4 3" stroke-width="1"/>'
               % (sv.pad + (m - 0.5) * sv.s, sv.pad + (m - 0.5) * sv.s,
                  (spec.cols - 2 * m) * sv.s, (spec.rows - 2 * m) * sv.s))


def _draw_segments(sv, routes, kind, opacity, width_mul, dashed=False):
    """Desenha um tipo de segmento cru do roteador (jumper, via).

    Fio e ponte de solda NAO saem daqui: eles vem do plano de montagem, porque o
    roteador nao sabe onde a ponta de um fio termina e o estanho comeca.
    """
    out, alerta = [], []
    for r in routes:
        col = net_color(r["name"])
        incompleta = not r.get("ok", True)
        for seg in r["segments"]:
            if seg["type"] != kind:
                continue
            a, b = seg["from"], seg["to"]
            dash = ' stroke-dasharray="5 4"' if dashed else ""
            titulo = r["name"] + (" - REDE INCOMPLETA" if incompleta else "")
            out.append(
                '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="%.1f" '
                'stroke-linecap="round" opacity="%.2f"%s><title>%s</title></line>'
                % (sv.x(a[0]), sv.y(a[1]), sv.x(b[0]), sv.y(b[1]), col,
                   sv.s * width_mul, opacity, dash, html.escape(titulo))
            )
            if incompleta:
                alerta.append(
                    '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                    'stroke-width="%.1f" stroke-dasharray="3 5" opacity="0.9" '
                    'pointer-events="none"/>'
                    % (sv.x(a[0]), sv.y(a[1]), sv.x(b[0]), sv.y(b[1]), FAIL_COLOR,
                       max(1.6, sv.s * width_mul * 0.45))
                )
    sv.add("\n".join(out + alerta))


def _draw_bancada(sv, plano, face, opacity, width_mul, dashed=False):
    """Fio e ponte de solda, direto do plano de montagem.

    Sao desenhos distintos de proposito, porque sao trabalhos distintos:

    * ponte - traco curto e GROSSO, sem bolinhas. Dois furos vizinhos ligados so
      com estanho; nao leva fio nenhum.
    * fio reto - linha fina com uma bolinha em cada ponta. A linha e o fio; as
      bolinhas sao onde o ferro encosta.
    """
    if not plano:
        return
    out, solda, alerta = [], [], []
    dash = ' stroke-dasharray="5 4"' if dashed else ""

    for tipo in ("pontes", "fios"):
        for x in plano.get(tipo, ()):
            if x["face"] != face:
                continue
            a, b = x["de"], x["ate"]
            col = net_color(x["net"])
            incompleta = not x.get("ok", True)
            titulo = "%s - %s de %s a %s%s" % (
                x["net"],
                "ponte de solda (furos vizinhos, sem fio)" if tipo == "pontes"
                else "fio reto de %.1f mm" % x["mm"],
                x["de_label"], x["ate_label"],
                " - REDE INCOMPLETA" if incompleta else "")
            # Solda e fio precisam ser DISTINGUIVEIS de longe, nao so diferentes.
            # Duas pontes lado a lado formam uma linha continua e ficavam iguais a
            # um fio - foi assim que uma junta em T correta pareceu tres fios.
            #
            # Ponte: cordao gordo e arredondado, com brilho claro no meio, do jeito
            # que estanho escorrido parece. Fio: linha fina e limpa, com a solda
            # marcada so nas pontas.
            if tipo == "pontes":
                largura = sv.s * width_mul * 3.2
                out.append(
                    '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                    'stroke-width="%.1f" stroke-linecap="round" opacity="%.2f"%s>'
                    '<title>%s</title></line>'
                    % (sv.x(a[0]), sv.y(a[1]), sv.x(b[0]), sv.y(b[1]), col,
                       largura, opacity * 0.9, dash, html.escape(titulo))
                )
                out.append(
                    '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#fff" '
                    'stroke-width="%.1f" stroke-linecap="round" opacity="%.2f" '
                    'pointer-events="none"/>'
                    % (sv.x(a[0]), sv.y(a[1]), sv.x(b[0]), sv.y(b[1]),
                       max(0.8, largura * 0.22), opacity * 0.45)
                )
                continue

            largura = sv.s * width_mul * 0.78
            out.append(
                '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                'stroke-width="%.1f" stroke-linecap="round" opacity="%.2f"%s>'
                '<title>%s</title></line>'
                % (sv.x(a[0]), sv.y(a[1]), sv.x(b[0]), sv.y(b[1]), col,
                   largura, opacity, dash, html.escape(titulo))
            )
            # Ponta de fio NAO ganha marcador proprio. Depois das regras da
            # bancada ela sempre cai onde ja ha solda desenhada: uma ponte, uma
            # junta, ou uma travessia para o outro lado. Marcar de novo era inventar
            # um terceiro tipo de ponto que nao existe na montagem.
            if incompleta:
                alerta.append(
                    '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                    'stroke-width="%.1f" stroke-dasharray="3 5" opacity="0.9" '
                    'pointer-events="none"/>'
                    % (sv.x(a[0]), sv.y(a[1]), sv.x(b[0]), sv.y(b[1]), FAIL_COLOR,
                       max(1.6, largura * 0.45))
                )
    sv.add("\n".join(out + solda + alerta))


def _draw_juntas(sv, plano, face, opacity=0.95):
    """Marca as juntas vindas do plano de montagem - a mesma lista do guia.

    Junta e a quina do desenho, que na bancada e estanho e nao fio dobrado. Marcar
    isso e o que impede alguem de tentar dobrar um fio num ponto onde, na verdade,
    encosta o ferro.
    """
    out = []
    for j in plano.get("juntas", ()):
        if j["face"] != face:
            continue
        c, r = j["furo"]
        out.append(
            '<circle cx="%.1f" cy="%.1f" r="%.1f" fill="none" stroke="%s" '
            'stroke-width="%.1f" opacity="%.2f">'
            '<title>%s - junta de solda em %s (%s), alcancando %s</title></circle>'
            % (sv.x(c), sv.y(r), sv.s * 0.26, net_color(j["net"]),
               max(1.2, sv.s * 0.05), opacity, html.escape(j["net"]),
               html.escape(j["furo_label"]), j["formato"],
               html.escape(", ".join(j["toca_labels"])))
        )
    if out:
        sv.add("\n".join(out))


def _draw_components(sv, layout, faded=False):
    out = []
    op = 0.25 if faded else 1.0
    for ref in sorted(layout.placements):
        fp = layout.footprints.get(ref)
        if not fp or not fp.pins:
            continue
        bb = layout.body_bbox(ref) or layout.bbox(ref)
        if bb is None:
            continue
        x0, y0, x1, y1 = bb
        px = min(sv.x(x0), sv.x(x1)) - sv.s * 0.42
        py = sv.y(y0) - sv.s * 0.42
        w = (x1 - x0) * sv.s + sv.s * 0.84
        h = (y1 - y0) * sv.s + sv.s * 0.84
        pl = layout.placements[ref]
        out.append(
            '<g class="comp" data-ref="%s" data-locked="%d" data-rot="%d">'
            '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="4" '
            'fill="#2e3440" fill-opacity="%.2f" stroke="%s" stroke-width="%.1f"/>'
            % (html.escape(ref), 1 if pl.locked else 0, pl.rot, px, py, w, h,
               0.16 * op if faded else 0.82,
               "#e8b53a" if (pl.locked and not faded) else "#1b1f27",
               2.0 if (pl.locked and not faded) else 1.0)
        )
        out.append(
            '<text x="%.1f" y="%.1f" font-size="%.1f" fill="%s" text-anchor="middle" '
            'dominant-baseline="middle" pointer-events="none">%s</text></g>'
            % (px + w / 2, py + h / 2, min(13, sv.s * 0.5),
               "#8a8a8a" if faded else "#f2f2f2", html.escape(ref))
        )
    sv.add("\n".join(out))


def _draw_vias(sv, routes, faded=False):
    """Via = fio atravessando um furo livre, soldado nas duas faces."""
    out = []
    for r in routes:
        col = net_color(r["name"])
        for seg in r["segments"]:
            if seg["type"] != "via":
                continue
            c, rr = seg["from"]
            out.append(
                '<g pointer-events="none" opacity="%.2f">'
                '<circle cx="%.1f" cy="%.1f" r="%.1f" fill="none" stroke="%s" stroke-width="%.1f"/>'
                '<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s"/></g>'
                % (0.45 if faded else 1.0,
                   sv.x(c), sv.y(rr), sv.s * 0.34, col, max(1.6, sv.s * 0.09),
                   sv.x(c), sv.y(rr), sv.s * 0.11, col)
            )
    if out:
        sv.add("\n".join(out))


def _draw_pins(sv, layout, hole_nets, orphans=None):
    """orphans: {(col,row): rede} - pinos que ficaram sem ligacao real."""
    orphans = orphans or {}
    nome_do_furo = rotulador(layout.spec, sv.label_style)
    out, alerta = [], []
    for ref in sorted(layout.placements):
        for pin, (c, r) in sorted(layout.pin_holes(ref).items()):
            net = hole_nets.get("%d,%d" % (c, r), "")
            col = net_color(net) if net else "#777"
            solto = (c, r) in orphans
            out.append(
                '<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s" stroke="%s" stroke-width="%.1f">'
                '<title>%s pino %s - furo %s%s%s</title></circle>'
                % (sv.x(c), sv.y(r), sv.s * 0.24, col,
                   FAIL_COLOR if solto else "#fff", 2.2 if solto else 1.0,
                   html.escape(ref), html.escape(pin),
                   nome_do_furo(c, r),
                   (" - rede " + html.escape(net)) if net else "",
                   " - SEM LIGACAO" if solto else "")
            )
            if solto:
                x, y, k = sv.x(c), sv.y(r), sv.s * 0.42
                alerta.append(
                    '<g pointer-events="none" stroke="%s" stroke-width="2.2" stroke-linecap="round">'
                    '<circle cx="%.1f" cy="%.1f" r="%.1f" fill="none" opacity="0.9"/>'
                    '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                    '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/></g>'
                    % (FAIL_COLOR, x, y, k,
                       x - k * 0.62, y - k * 0.62, x + k * 0.62, y + k * 0.62,
                       x - k * 0.62, y + k * 0.62, x + k * 0.62, y - k * 0.62)
                )
    sv.add("\n".join(out + alerta))


def render_board(layout, routes, side: str = "top", scale: float = 26.0,
                 hole_nets=None, title: str = "", label_style: str = "letra",
                 plano=None) -> str:
    """Gera o SVG de um dos lados da placa.

    `plano` vem de `bancada.plano_de_montagem` e e a MESMA lista que o guia de
    montagem usa. Passar de fora, em vez de recontar aqui, e o que garante que o
    desenho e o texto nunca discordem.
    """
    hole_nets = hole_nets or {}
    orphans = {}
    for r in routes:
        for o in r.get("orphans", []):
            orphans[tuple(o["cell"])] = r["name"]
    sv = SvgBoard(layout.spec, scale=scale, side=side, label_style=label_style)
    _hole_grid(sv)

    if side == "top":
        # trilhas do lado da solda ficam por baixo: aparecem apagadas, so como referencia
        _draw_bancada(sv, plano, "solda", 0.26, 0.16, dashed=True)
        _draw_components(sv, layout)
        _draw_bancada(sv, plano, "componentes", 0.95, 0.20)
        _draw_segments(sv, routes, "jumper", 0.95, 0.13)
        if plano:
            _draw_juntas(sv, plano, "componentes")
        _draw_vias(sv, routes)
    else:
        _draw_components(sv, layout, faded=True)
        _draw_bancada(sv, plano, "componentes", 0.24, 0.12, dashed=True)
        _draw_segments(sv, routes, "jumper", 0.20, 0.10, dashed=True)
        _draw_bancada(sv, plano, "solda", 0.95, 0.20)
        if plano:
            _draw_juntas(sv, plano, "solda")
        _draw_vias(sv, routes, faded=True)

    _draw_pins(sv, layout, hole_nets, orphans)

    label = "Lado dos componentes" if side == "top" else "Lado da solda (espelhado)"
    sv.add('<text x="%.1f" y="%.1f" font-size="11" fill="#6b5f42">%s</text>'
           % (6, sv.height - 8, html.escape(title or label)))
    if orphans:
        sv.add('<text x="%.1f" y="%.1f" font-size="11" font-weight="bold" fill="%s" '
               'text-anchor="end">%d pino(s) marcado(s) com X estao SEM LIGACAO</text>'
               % (sv.width - 6, sv.height - 8, FAIL_COLOR, len(orphans)))
    return sv.render(title or label)
