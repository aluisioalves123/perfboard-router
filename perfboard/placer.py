"""Posicionamento automatico dos componentes na grade da perfboard.

Estrategia: empacotamento inicial guloso + recozimento simulado (simulated
annealing) minimizando comprimento estimado das ligacoes (HPWL), sobreposicao
de corpos e furos fora da placa. Componentes travados nunca se movem.

O custo e mantido de forma incremental: cada movimento so recalcula as redes e
as celulas do componente que mudou.
"""
from __future__ import annotations

import math
import random

from . import nativo
from .board import Layout, Placement, ROTATIONS, rotate

EFFORT_STEPS = {"rapido": 150, "normal": 600, "alto": 2500}

# Ha dois tipos de custo aqui, e confundi-los ja causou bug: PREFERENCIA (fio mais
# curto, laco menor, conector na borda) e IMPOSSIBILIDADE FISICA (duas pecas no mesmo
# espaco, pino fora da placa). Impossibilidade tem que valer MUITO mais que qualquer
# preferencia, senao o posicionador "compra" o impossivel para ganhar no resto - foi
# o que aconteceu quando o premio do desacoplamento (45/furo) superou a penalidade de
# sobreposicao (40/furo) e o capacitor foi parar em cima do CI.
# Restricao fisica vira penalidade ALTA no fim - mas se ela ja nascer alta, vira um
# muro no meio do terreno de busca: o recozimento precisa poder atravessar uma regiao
# ruim para chegar na boa do outro lado. Medido: com peso fixo em 400 o circuito de
# teste parava em 1 pino solto; com 40 ele fechava, so que entregando peca sobreposta.
# Solucao: comeca frouxo (explora) e endurece ate o fim (entrega valido).
W_OVERLAP = 400.0          # peso FINAL; o inicial e uma fracao disto
W_OVERLAP_INICIAL = 30.0
W_OUTSIDE = 400.0     # pino fora da placa: nao existe
W_CORPO_FORA = 60.0   # corpo passando da borda: ruim, mas as vezes aceitavel

# Capacitor de desacoplamento: o que importa nao e o comprimento do fio, e a AREA
# DO LACO alimentacao -> capacitor -> terra -> CI. Peso alto, bem acima do custo
# normal de fiacao - mas deliberadamente MUITO abaixo de W_OVERLAP, porque aproximar
# o capacitor jamais justifica coloca-lo dentro de outra peca.
W_DESACOPLA = 45.0
FOLGA_DESACOPLA = 1   # ficar a 1 furo ja e o ideal pratico; nao adianta cobrar mais

# Pino cercado por pinos de outras redes nao tem por onde sair: nenhuma trilha
# consegue deixar o furo, em nenhuma das faces. Vale muito evitar isso.
DIRS4 = ((1, 0), (-1, 0), (0, 1), (0, -1))
W_SEM_SAIDA = {0: 90.0, 1: 14.0, 2: 3.0}


def _fp_span(fp, rot):
    cells = [rotate(dx, dy, rot) for dx, dy in fp.pins.values()]
    xs = [c[0] for c in cells] or [0]
    ys = [c[1] for c in cells] or [0]
    return min(xs), min(ys), max(xs), max(ys)


def _body_span(fp, rot):
    """Retangulo do corpo depois de girado, em furos."""
    x0, y0, x1, y1 = fp.body_extent
    cantos = [rotate(x0, y0, rot), rotate(x1, y0, rot),
              rotate(x0, y1, rot), rotate(x1, y1, rot)]
    xs = [c[0] for c in cantos]
    ys = [c[1] for c in cantos]
    return min(xs), min(ys), max(xs), max(ys)


def _random_position(spec, fp, rot, rng):
    mnx, mny, mxx, mxy = _body_span(fp, rot)
    x0, y0, x1, y1 = spec.usable_bounds()
    lo_c, hi_c = x0 - mnx, x1 - mxx
    lo_r, hi_r = y0 - mny, y1 - mxy
    hi_c = max(hi_c, lo_c)
    hi_r = max(hi_r, lo_r)
    return rng.randint(lo_c, hi_c), rng.randint(lo_r, hi_r)


class _State:
    """Estado do recozimento com custo incremental."""

    def __init__(self, layout: Layout, netlist, edge_pull: float = 0.6, decoupling=None):
        self.layout = layout
        self.spec = layout.spec
        self.edge_pull = edge_pull

        self.refs = [r for r in layout.placements
                     if layout.footprints.get(r) and layout.footprints[r].pins]
        self.movable = [r for r in self.refs if not layout.placements[r].locked]

        # offsets originais por componente, para nao reconsultar o footprint
        self.offsets = {r: list(layout.footprints[r].pins.items()) for r in self.refs}

        # Retangulo do CORPO (nao o dos pinos) na orientacao original. E o corpo que
        # decide colisao: um borne ocupa bem mais espaco que o vao dos terminais.
        self.body_local = {r: layout.footprints[r].body_extent for r in self.refs}

        # redes relevantes e indice reverso
        self.nets = []          # (nome, [(ref, pino), ...])
        self.ref_nets = {r: set() for r in self.refs}
        for net in netlist.routable_nets():
            nodes = [(ref, pin) for ref, pin in net.nodes if ref in self.ref_nets]
            if len(nodes) < 2:
                continue
            idx = len(self.nets)
            self.nets.append((net.name, nodes))
            for ref, _ in nodes:
                self.ref_nets[ref].add(idx)

        self.is_edge = {}
        for r in self.refs:
            fp = layout.footprints[r]
            self.is_edge[r] = r[:1] in ("J", "P") or "onn" in (fp.key or "")

        # rede de cada pino, para saber quem sufoca quem
        self.net_of_pin = {}
        for net in netlist.nets:
            for node in net.nodes:
                self.net_of_pin[node] = net.name

        # estado derivado
        self.pin_net = {}       # (c, r) -> rede do pino que ocupa o furo
        self.owned = {}         # ref -> [(celula, rede)] que ele registrou
        self.pen = {}           # (c, r) -> penalidade de sufocamento
        self._afetadas = set()  # reaproveitado a cada movimento
        self.pin_cell = {}      # (ref, pino) -> (c, r)
        # Ocupacao por celula. Ja tentei trocar por comparacao de retangulos aos
        # pares (parecia mais barato: ~25 contas de inteiro contra ~20 acessos de
        # dicionario) e MEDI 8% mais lento, mesmo com a aritmetica inlinada - em
        # Python o acesso a dicionario e mais barato do que parece e o laco sobre
        # as outras pecas nao compensa. Fica a grade.
        self.cells = {}         # ref -> celulas do corpo
        self.occ = {}           # celula -> contagem
        self.overlap = 0
        self.outside = {}       # ref -> furos de PINO fora da placa
        self.corpo_fora = {}    # ref -> furos do CORPO fora da placa
        # Somas correntes. `cost` e consultado duas vezes por movimento e varria
        # estes tres dicionarios inteiros a cada consulta - com ~100 mil movimentos
        # por posicionamento, isso era milhoes de somas para nada.
        self.tot_outside = 0
        self.tot_corpo_fora = 0
        self.tot_edge = 0.0
        self.edge = {}          # ref -> custo de borda
        self.net_cost = [0.0] * len(self.nets)
        # peso corrente da sobreposicao; sobe ao longo do recozimento
        self.w_overlap = W_OVERLAP

        # pares (pino_a, pino_b) que precisam ficar colados: desacoplamento
        self.pares = []
        self.pares_de_ref = {r: set() for r in self.refs}
        for d in (decoupling or []):
            cap, ci = d.get("cap"), d.get("ic")
            if cap not in self.ref_nets or ci not in self.ref_nets:
                continue
            for pa, pb in ((d.get("cap_pin_pwr"), d.get("ic_pin_pwr")),
                           (d.get("cap_pin_gnd"), d.get("ic_pin_gnd"))):
                if pa is None or pb is None:
                    continue
                idx = len(self.pares)
                self.pares.append(((cap, str(pa)), (ci, str(pb))))
                self.pares_de_ref[cap].add(idx)
                self.pares_de_ref[ci].add(idx)
        self.par_cost = [0.0] * len(self.pares)

        for r in self.refs:
            self._install(r)
        for i in range(len(self.nets)):
            self.net_cost[i] = self._calc_net(i)
        self.wire = sum(self.net_cost)
        self._repen(set(self.pin_net))
        self.trapped = sum(self.pen.values())
        for i in range(len(self.pares)):
            self.par_cost[i] = self._calc_par(i)
        self.laco = sum(self.par_cost)

    # ---------- manutencao ----------

    def _install(self, ref):
        pl = self.layout.placements[ref]
        pts = []
        marcados = []
        for pin, (dx, dy) in self.offsets[ref]:
            rx, ry = rotate(dx, dy, pl.rot)
            cell = (pl.col + rx, pl.row + ry)
            self.pin_cell[(ref, pin)] = cell
            pts.append(cell)
            net = self.net_of_pin.get((ref, pin))
            if net is not None and cell not in self.pin_net:
                self.pin_net[cell] = net
                marcados.append((cell, net))
        self.owned[ref] = marcados

        # retangulo do corpo, rotacionado junto com a peca
        bx0, by0, bx1, by1 = self.body_local[ref]
        cantos = [rotate(bx0, by0, pl.rot), rotate(bx1, by0, pl.rot),
                  rotate(bx0, by1, pl.rot), rotate(bx1, by1, pl.rot)]
        cx0 = pl.col + min(c[0] for c in cantos)
        cx1 = pl.col + max(c[0] for c in cantos)
        cy0 = pl.row + min(c[1] for c in cantos)
        cy1 = pl.row + max(c[1] for c in cantos)

        body = {(c, r) for r in range(cy0, cy1 + 1) for c in range(cx0, cx1 + 1)}
        self.cells[ref] = body
        fora_corpo = 0
        for c in body:
            n = self.occ.get(c, 0)
            if n >= 1:
                self.overlap += 1
            self.occ[c] = n + 1
            if not self.spec.contains(*c):
                fora_corpo += 1

        # pino fora da placa e erro grave; corpo passando da borda e so ruim
        fora_pinos = sum(0 if self.spec.contains(c, r) else 1 for c, r in pts)
        self.tot_outside += fora_pinos - self.outside.get(ref, 0)
        self.tot_corpo_fora += fora_corpo - self.corpo_fora.get(ref, 0)
        self.outside[ref] = fora_pinos
        self.corpo_fora[ref] = fora_corpo

        if self.edge_pull and self.is_edge[ref]:
            x0, y0, x1, y1 = self.spec.usable_bounds()
            cx = (cx0 + cx1) / 2.0
            cy = (cy0 + cy1) / 2.0
            novo_edge = min(cx - x0, x1 - cx, cy - y0, y1 - cy) * self.edge_pull
        else:
            novo_edge = 0.0
        self.tot_edge += novo_edge - self.edge.get(ref, 0.0)
        self.edge[ref] = novo_edge

    def _uninstall(self, ref):
        for cell, net in self.owned.pop(ref, ()):
            if self.pin_net.get(cell) == net:
                del self.pin_net[cell]
        for c in self.cells.get(ref, ()):
            n = self.occ.get(c, 0)
            if n >= 2:
                self.overlap -= 1
            if n <= 1:
                self.occ.pop(c, None)
            else:
                self.occ[c] = n - 1
        self.cells[ref] = set()

    def _pen_cell(self, cell):
        """Quantas saidas livres o furo tem. Sem saida = trilha nenhuma escapa dali."""
        net = self.pin_net.get(cell)
        if net is None:
            return 0.0
        livres = 0
        for dc, dr in DIRS4:
            viz = (cell[0] + dc, cell[1] + dr)
            if not self.spec.contains(*viz):
                continue
            dono = self.pin_net.get(viz)
            if dono is None or dono == net:
                livres += 1
        return W_SEM_SAIDA.get(livres, 0.0)

    def _repen(self, cells):
        """Recalcula a penalidade das celulas dadas (e devolve a variacao)."""
        delta = 0.0
        for cell in cells:
            antes = self.pen.get(cell, 0.0)
            agora = self._pen_cell(cell)
            if agora:
                self.pen[cell] = agora
            else:
                self.pen.pop(cell, None)
            delta += agora - antes
        return delta

    def _vizinhanca(self, cells, destino=None):
        """Celulas dadas mais os quatro vizinhos de cada uma.

        Aceita um conjunto de destino para nao alocar um novo a cada movimento.
        """
        out = set() if destino is None else destino
        for cell in cells:
            out.add(cell)
            c, r = cell
            out.add((c + 1, r))
            out.add((c - 1, r))
            out.add((c, r + 1))
            out.add((c, r - 1))
        return out

    def _calc_par(self, idx):
        """Distancia entre os dois pinos de um par que deve ficar colado."""
        a, b = self.pares[idx]
        ca, cb = self.pin_cell.get(a), self.pin_cell.get(b)
        if ca is None or cb is None:
            return 0.0
        d = abs(ca[0] - cb[0]) + abs(ca[1] - cb[1])
        return W_DESACOPLA * max(0, d - FOLGA_DESACOPLA)

    def _calc_net(self, idx):
        _, nodes = self.nets[idx]
        xs, ys = [], []
        for key in nodes:
            cell = self.pin_cell.get(key)
            if cell is not None:
                xs.append(cell[0])
                ys.append(cell[1])
        if len(xs) < 2:
            return 0.0
        return float((max(xs) - min(xs)) + (max(ys) - min(ys)))

    def apply(self, ref, col, row, rot):
        pl = self.layout.placements[ref]
        afetadas = self._afetadas
        afetadas.clear()
        self._vizinhanca((c for c, _ in self.owned.get(ref, ())), afetadas)
        self._uninstall(ref)
        pl.col, pl.row, pl.rot = col, row, rot
        self._install(ref)
        self._vizinhanca((c for c, _ in self.owned.get(ref, ())), afetadas)
        self.trapped += self._repen(afetadas)
        for i in self.ref_nets[ref]:
            new = self._calc_net(i)
            self.wire += new - self.net_cost[i]
            self.net_cost[i] = new
        for i in self.pares_de_ref.get(ref, ()):
            new = self._calc_par(i)
            self.laco += new - self.par_cost[i]
            self.par_cost[i] = new

    @property
    def cost(self):
        """Custo usado para ACEITAR movimentos: sobreposicao pesa `self.w_overlap`,
        que cresce ao longo da busca."""
        return (self.wire
                + self.w_overlap * self.overlap
                + W_OUTSIDE * sum(self.outside.values())
                + W_CORPO_FORA * sum(self.corpo_fora.values())
                + self.trapped
                + self.laco
                + sum(self.edge.values()))

    def estrito_de(self, custo_corrente):
        """Converte um custo ja calculado para o peso final de sobreposicao.

        Aceitar com peso frouxo e julgar com peso rigido e o que permite explorar
        por regioes invalidas sem guardar uma delas como resposta. Recebe o custo
        pronto para nao recalcular tudo de novo a cada movimento aceito.
        """
        return custo_corrente + (W_OVERLAP - self.w_overlap) * self.overlap

    @property
    def custo_estrito(self):
        return self.estrito_de(self.cost)

    # ---------- snapshots ----------

    def snapshot(self, refs):
        p = self.layout.placements
        return [(r, p[r].col, p[r].row, p[r].rot) for r in refs]

    def restore(self, snap):
        for ref, c, r, rot in snap:
            pl = self.layout.placements[ref]
            if (pl.col, pl.row, pl.rot) != (c, r, rot):
                self.apply(ref, c, r, rot)


def initial_pack(layout: Layout, netlist, rng: random.Random):
    """Empacota em fileiras, do maior/mais conectado para o menor."""
    spec = layout.spec
    x0, y0, x1, y1 = spec.usable_bounds()

    degree = {}
    for net in netlist.routable_nets():
        for ref, _ in net.nodes:
            degree[ref] = degree.get(ref, 0) + 1

    pending = [r for r, p in layout.placements.items()
               if not p.locked and layout.footprints.get(r) and layout.footprints[r].pins]
    pending.sort(key=lambda r: (-layout.footprints[r].size[0] * layout.footprints[r].size[1],
                                -degree.get(r, 0), r))

    cur_c, cur_r, row_h = x0, y0, 0
    for ref in pending:
        fp = layout.footprints[ref]
        w, h = fp.body_size
        mnx, mny = fp.body_extent[0], fp.body_extent[1]
        p = layout.placements[ref]
        if cur_c + w - 1 > x1:
            cur_c = x0
            cur_r += row_h + 1
            row_h = 0
        if cur_r + h - 1 > y1:
            c, r = _random_position(spec, fp, 0, rng)
            p.col, p.row, p.rot = c, r, 0
            continue
        p.col, p.row, p.rot = cur_c - mnx, cur_r - mny, 0
        cur_c += w + 1
        row_h = max(row_h, h)


def auto_place(layout: Layout, netlist, *, seed: int = 1, effort: str = "normal",
               keep_existing: bool = False, edge_pull: float = 0.6,
               decoupling=None) -> dict:
    """Posiciona os componentes nao travados. Altera `layout` no lugar."""
    rng = random.Random(seed)

    for ref in layout.footprints:
        layout.placements.setdefault(ref, Placement(ref=ref))

    if not keep_existing:
        initial_pack(layout, netlist, rng)

    st = _State(layout, netlist, edge_pull=edge_pull, decoupling=decoupling)
    if not st.movable:
        return {"moved": 0, "steps": 0, "cost": round(st.cost, 1),
                "note": "nenhum componente livre para mover"}

    steps = min(EFFORT_STEPS.get(effort, EFFORT_STEPS["normal"]) * max(6, len(st.movable)), 600000)

    # O recozimento em C faz o mesmo trabalho ~10x mais rapido. Ele e heuristico,
    # entao nao produz o MESMO resultado do Python - produz um equivalente. As
    # invariantes (nada sobreposto, nada fora da placa, travadas imoveis) valem nos
    # dois caminhos e sao verificadas por teste.
    if nativo.tem_posicionador():
        pesos = {
            "overlap_final": W_OVERLAP, "overlap_inicial": W_OVERLAP_INICIAL,
            "outside": W_OUTSIDE, "corpo_fora": W_CORPO_FORA,
            "desacopla": W_DESACOPLA, "folga_desacopla": FOLGA_DESACOPLA,
            "sem_saida": (W_SEM_SAIDA[0], W_SEM_SAIDA[1], W_SEM_SAIDA[2]),
        }
        if nativo.posiciona(st, steps, seed, pesos):
            final = _State(layout, netlist, edge_pull=edge_pull, decoupling=decoupling)
            return {
                "moved": len(st.movable), "steps": steps, "motor": "C",
                "cost": round(final.cost, 1), "wire_estimate": round(final.wire, 1),
                "overlaps": final.overlap, "outside": final.tot_outside,
                "corpo_fora": final.tot_corpo_fora,
                "pinos_sufocados": sum(1 for v in final.pen.values() if v >= W_SEM_SAIDA[1]),
                "desacoplamento_ok": all(c <= 0 for c in final.par_cost),
            }

    st.w_overlap = W_OVERLAP_INICIAL
    cost = st.cost
    t0 = max(4.0, cost / max(1, len(st.movable)) * 0.6)
    t_end = 0.05
    best_cost = st.custo_estrito
    best = st.snapshot(st.movable)
    accepted = 0

    for i in range(steps):
        frac = i / steps
        t = t0 * (t_end / t0) ** frac
        # endurece a sobreposicao junto com o resfriamento, chegando ao peso final
        # antes do fim para que o trecho final ja procure so solucoes validas
        antes = st.w_overlap
        st.w_overlap = W_OVERLAP_INICIAL + (W_OVERLAP - W_OVERLAP_INICIAL) * min(1.0, frac / 0.8)
        if st.overlap and antes != st.w_overlap:
            cost += (st.w_overlap - antes) * st.overlap
        ref = rng.choice(st.movable)
        pl = layout.placements[ref]
        fp = layout.footprints[ref]
        kind = rng.random()

        if kind < 0.62:                      # deslocamento local
            snap = st.snapshot([ref])
            d = 1 if t < t0 * 0.2 else 3
            st.apply(ref, pl.col + rng.randint(-d, d), pl.row + rng.randint(-d, d), pl.rot)
        elif kind < 0.78:                    # rotacao
            snap = st.snapshot([ref])
            st.apply(ref, pl.col, pl.row, rng.choice(ROTATIONS))
        elif kind < 0.90:                    # realocacao aleatoria
            snap = st.snapshot([ref])
            rot = rng.choice(ROTATIONS)
            c, r = _random_position(layout.spec, fp, rot, rng)
            st.apply(ref, c, r, rot)
        else:                                # troca de lugar com outro componente
            other = rng.choice(st.movable)
            if other == ref:
                continue
            snap = st.snapshot([ref, other])
            po = layout.placements[other]
            oc, orow, orot = po.col, po.row, po.rot
            pc, prow, prot = pl.col, pl.row, pl.rot
            st.apply(other, pc, prow, orot)
            st.apply(ref, oc, orow, prot)

        new_cost = st.cost
        delta = new_cost - cost
        if delta <= 0 or rng.random() < math.exp(-min(60.0, delta / max(t, 1e-6))):
            cost = new_cost
            accepted += 1
            estrito = st.estrito_de(new_cost)
            if estrito < best_cost - 1e-9:
                best_cost = estrito
                best = st.snapshot(st.movable)
        else:
            st.restore(snap)

    st.w_overlap = W_OVERLAP
    st.restore(best)
    return {
        "moved": len(st.movable),
        "steps": steps,
        "accepted": accepted,
        "cost": round(st.cost, 1),
        "wire_estimate": round(st.wire, 1),
        "overlaps": st.overlap,
        "outside": sum(st.outside.values()),
        "corpo_fora": sum(st.corpo_fora.values()),
        "pinos_sufocados": sum(1 for v in st.pen.values() if v >= W_SEM_SAIDA[1]),
        "desacoplamento_ok": all(c <= 0 for c in st.par_cost),
        "motor": "Python",
    }
