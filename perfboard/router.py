"""Roteamento automatico respeitando as restricoes fisicas de uma perfboard.

Modelo de recursos
------------------
Cada furo tem DUAS ilhas: a de baixo (lado da solda) e a de cima (lado dos
componentes). O que se pode fazer com elas depende do tipo de placa:

* `faces = 1` - **perfboard de 1 face**, cobre so de um lado. As duas ilhas de um
  furo sao, na pratica, o mesmo ponto: qualquer solda pelo lado de cima encosta
  na ilha de baixo. Logo o furo inteiro pertence a uma unica rede, e o lado dos
  componentes so aceita **jumper** (fio isolado que pousa apenas nas pontas).

* `faces = 2` - **perfboard de 2 faces**, sem furo metalizado ligando os lados.
  As ilhas de cima e de baixo sao eletricamente independentes, entao existe uma
  segunda camada de verdade: da para fazer **trilha no lado dos componentes** e
  **via** (pedaco de fio atravessando um furo livre, soldado dos dois lados).
  E assim que duas redes se cruzam sem se tocar.

Recursos disputados, em qualquer modo:

* ILHA `(coluna, linha, face)` - de uma rede so. O terminal de um componente
  atravessa o furo, entao ocupa as **duas** faces e ja liga uma na outra.
* ARESTA `(furo_a, furo_b, face)` - trilha entre dois furos ORTOGONALMENTE
  vizinhos, naquela face. De uma rede so.
* JUMPER - fio isolado em linha reta entre dois furos, passando por cima de tudo.
  As duas pontas precisam cair em furo LIVRE: o furo que ja tem o terminal de um
  componente nao aceita mais um fio atravessando junto. Na bancada isso se resolve
  puxando uma trilha curta do pino ate um furo vago ao lado e soldando o jumper ali
  - e exatamente esse caminho que o roteador constroi.

Como as trilhas so andam em segmentos ortogonais entre furos vizinhos, duas
trilhas da MESMA face so poderiam se cruzar dentro de um furo - e ilha e
exclusiva. Faces diferentes se cruzam livremente: e para isso que serve a via.

Algoritmo
---------
Cada rede e construida como arvore (estilo Prim): A* multi-origem/multi-destino
sobre o grafo `(coluna, linha, face)`. Em volta disso, duas fases:

1. **Negociacao de congestionamento (PathFinder).** Todas as redes pegam seu
   melhor caminho, mesmo pisando no do vizinho. A cada rodada o recurso disputado
   fica mais caro (fator de presenca) e acumula rancor permanente (historico), ate
   sobrar no maximo um dono por recurso.
2. **Legalizacao estrita.** Se a negociacao nao converge, roda rip-up & reroute
   com restricao dura - duas vezes, uma com o historico aprendido e outra do zero,
   ficando com a melhor. Assim a fase 1 nunca piora o resultado final.

Tentativas sao comparadas por **numero de pinos que ficam sem ligacao**, medido
pela auditoria independente (`_audit`), e nao por quantas redes o laco achou que
falharam - contar redes engana, porque uma solucao com menos redes quebradas pode
deixar mais pinos soltos, que e o que se sente na bancada.
"""
from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass, field

from . import nativo

PITCH_MM = 2.54

DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))

BOTTOM = 0   # lado da solda
TOP = 1      # lado dos componentes

# O no da busca carrega a direcao com que se chegou nele, para saber se o proximo
# passo e uma continuacao reta ou uma dobra. LIVRE = chegou por via, jumper ou
# comeco de fio, entao sair em qualquer direcao nao custa dobra.
LIVRE = 4

FACE_NAME = {BOTTOM: "solda", TOP: "componentes"}


# Numa PCB o que pesa e o comprimento do cobre. Numa perfboard o trabalho e todo
# manual, e o custo real esta na QUANTIDADE de operacoes: cada dobra, cada troca
# de face, cada jumper. Um fio reto de 8 furos da menos trabalho que um ziguezague
# de 4 furos com 3 quinas. Estes perfis traduzem isso em custos.
# Nao existe ponto otimo: dobra e jumper sao substitutos. Encarecer a dobra empurra
# o trabalho para os jumpers e vice-versa; o que da para escolher e ONDE ficar nessa
# troca. Os numeros abaixo sao pontos medidos num circuito real de 26 pecas / 17 redes
# numa placa 24x18, media de 4 a 5 sementes:
#
#   perfil          quinas  jumpers  fio
#   menos_jumpers     37.8      9.5  530 mm
#   equilibrado       27.8     13.0  455 mm
#   menos_quinas      17.5     19.2  432 mm
#
# Para referencia, o modelo antigo (sem custo de dobra) dava 46.8 quinas com 10.6
# jumpers e 486 mm: pior em dobra E em fio que qualquer um destes. Cobrar pela dobra
# e ganho liquido; o perfil so decide de que lado da troca voce quer ficar.
PERFIS = {
    "menos_jumpers": dict(trace_cost=5.0, top_trace_cost=7.0, turn_cost=30.0,
                          via_cost=60.0, jumper_base=240.0, jumper_per_hole=4.0),
    "equilibrado": dict(trace_cost=5.0, top_trace_cost=7.0, turn_cost=45.0,
                        via_cost=70.0, jumper_base=110.0, jumper_per_hole=4.0),
    "menos_quinas": dict(trace_cost=5.0, top_trace_cost=7.0, turn_cost=60.0,
                         via_cost=90.0, jumper_base=110.0, jumper_per_hole=4.0),
}

# nomes antigos, para projetos ja salvos nao quebrarem
ALIAS_PERFIS = {"fio": "menos_jumpers", "manual": "menos_quinas"}


@dataclass
class RouterConfig:
    faces: int = 1                  # 1 = perfboard comum; 2 = duas faces isoladas
    trace_cost: float = 5.0         # passo de trilha no lado da solda (1 furo)
    top_trace_cost: float = 7.0     # passo de trilha no lado dos componentes
    turn_cost: float = 45.0         # dobrar o fio 90 graus
    via_cost: float = 70.0          # trocar de face num furo livre
    jumper_base: float = 110.0      # custo fixo de instalar um jumper
    jumper_per_hole: float = 4.0    # custo por furo de comprimento do jumper
    max_jumper: int = 14            # comprimento maximo de um jumper, em furos
    allow_jumpers: bool = True
    # Trilha do lado dos componentes NAO passa por baixo do corpo das pecas: para
    # trocar uma peca voce teria de cortar a fiacao, e a solda fica inacessivel.
    # Nao e configuravel de proposito - o ganho de roteamento nao paga o preco.
    attempts: int = 6               # rodadas de rip-up & reroute
    history_gain: float = 14.0
    seed: int = 1

    @staticmethod
    def from_json(d: dict) -> "RouterConfig":
        d = d or {}
        cfg = RouterConfig()

        perfil = d.get("preset")
        perfil = ALIAS_PERFIS.get(perfil, perfil)
        if perfil in PERFIS:
            for k, v in PERFIS[perfil].items():
                setattr(cfg, k, v)

        # numeros explicitos mandam mais que o perfil
        for k in ("trace_cost", "top_trace_cost", "turn_cost", "via_cost",
                  "jumper_base", "jumper_per_hole", "history_gain"):
            if k in d:
                setattr(cfg, k, float(d[k]))
        for k in ("max_jumper", "attempts", "seed", "faces"):
            if k in d:
                setattr(cfg, k, int(d[k]))
        if "allow_jumpers" in d:
            cfg.allow_jumpers = bool(d["allow_jumpers"])
        cfg.faces = 2 if cfg.faces >= 2 else 1
        return cfg

    @property
    def two_sided(self) -> bool:
        return self.faces >= 2


@dataclass
class NetRoute:
    name: str
    ok: bool = True
    segments: list = field(default_factory=list)   # (tipo, a, b, face)
    holes: list = field(default_factory=list)
    reason: str = ""
    orphans: list = field(default_factory=list)    # pinos sem ligacao real

    def to_json(self):
        out = []
        for kind, a, b, face in self.segments:
            out.append({
                "type": kind,
                "from": list(a),
                "to": list(b),
                "face": FACE_NAME.get(face, ""),
                "layer": face if face is not None else -1,
                "length_mm": round(_dist(a, b) * PITCH_MM, 2),
            })
        return {
            "name": self.name,
            "ok": self.ok,
            "reason": self.reason,
            "segments": out,
            "holes": [list(h) for h in self.holes],
            "orphans": list(self.orphans),
        }


def _dist(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _seg_len(seg):
    return _dist(seg[1], seg[2])


def _edge_key(a, b):
    return (a, b) if a <= b else (b, a)


def expand_trace(a, b):
    """Furos percorridos por uma trilha reta, inclusive os intermediarios."""
    (ax, ay), (bx, by) = tuple(a), tuple(b)
    dx = (bx > ax) - (bx < ax)
    dy = (by > ay) - (by < ay)
    holes = [(ax, ay)]
    x, y = ax, ay
    while (x, y) != (bx, by):
        x, y = x + dx, y + dy
        holes.append((x, y))
    return holes


class _Resources:
    """Quem esta usando cada ilha e cada aresta.

    Tem dois regimes:

    * **estrito** (`soft=False`) - recurso ja tomado por outra rede simplesmente
      nao pode ser usado. Garante saida legal, mas uma rede azarada trava.
    * **negociado** (`soft=True`) - o recurso pode ser disputado, so que fica
      progressivamente mais caro (fator de presenca) e guarda rancor entre as
      rodadas (custo de historico). E o esquema do PathFinder: deixa todo mundo
      passar pelo melhor caminho e vai empurrando quem tem alternativa mais
      barata, ate sobrar no maximo um dono por recurso.
    """

    def __init__(self, faces: int, pin_pads: dict, hist_pad: dict, hist_edge: dict,
                 soft: bool = False, pres: float = 0.0, pres_weight: float = 45.0):
        self.faces = faces
        self.fixed = dict(pin_pads)   # terminais: bloqueio duro em qualquer regime
        self.pad = {}                 # (c, r, face) -> {rede: 1}
        self.edge = {}                # ((a, b), face) -> {rede: 1}
        self.hist_pad = hist_pad
        self.hist_edge = hist_edge
        self.soft = soft
        self.pres = pres
        self.pres_weight = pres_weight

    def key(self, cell, face):
        # numa placa de face unica as duas ilhas do furo sao o mesmo ponto
        return (cell[0], cell[1], face if self.faces >= 2 else BOTTOM)

    # ---------- consulta ----------

    def pad_blocked(self, cell, face, net):
        k = self.key(cell, face)
        dono = self.fixed.get(k)
        if dono is not None and dono != net:
            return True
        if self.soft:
            return False
        users = self.pad.get(k)
        return bool(users) and net not in users

    def edge_blocked(self, a, b, face, net):
        if self.soft:
            return False
        users = self.edge.get((_edge_key(a, b), face))
        return bool(users) and net not in users

    def pad_extra(self, cell, face, net):
        k = self.key(cell, face)
        custo = self.hist_pad.get(k, 0.0)
        if self.soft:
            users = self.pad.get(k)
            if users:
                alheios = len(users) - (1 if net in users else 0)
                custo += self.pres * self.pres_weight * alheios
        return custo

    def edge_extra(self, a, b, face, net):
        k = (_edge_key(a, b), face)
        custo = self.hist_edge.get(k, 0.0)
        if self.soft:
            users = self.edge.get(k)
            if users:
                alheios = len(users) - (1 if net in users else 0)
                custo += self.pres * self.pres_weight * alheios
        return custo

    # ---------- ocupacao ----------

    def take_pad(self, cell, face, net):
        self.pad.setdefault(self.key(cell, face), {})[net] = 1

    def take_edge(self, a, b, face, net):
        self.edge.setdefault((_edge_key(a, b), face), {})[net] = 1

    def release(self, net):
        for k in list(self.pad):
            users = self.pad[k]
            if net in users:
                del users[net]
                if not users:
                    del self.pad[k]
        for k in list(self.edge):
            users = self.edge[k]
            if net in users:
                del users[net]
                if not users:
                    del self.edge[k]

    def disputados(self):
        """Recursos com mais de uma rede em cima. Vazio = solucao legal."""
        pads = {k: len(v) for k, v in self.pad.items() if len(v) > 1}
        edges = {k: len(v) for k, v in self.edge.items() if len(v) > 1}
        return pads, edges

    def hole_nets(self):
        """(coluna,linha) -> rede, para colorir os furos no desenho."""
        out = {}
        for (c, r, _face), net in self.fixed.items():
            out.setdefault((c, r), net)
        for (c, r, _face), users in self.pad.items():
            if users:
                out.setdefault((c, r), next(iter(users)))
        return out


class Router:
    def __init__(self, layout, netlist, config: RouterConfig | None = None):
        self.layout = layout
        self.netlist = netlist
        self.cfg = config or RouterConfig()
        self.spec = layout.spec

        # (ref, pino) -> nome da rede
        self._net_index = {}
        for net in netlist.nets:
            for node in net.nodes:
                self._net_index[node] = net.name

        # furos ocupados por terminais: o terminal atravessa, entao pega as duas faces
        self.pin_pads = {}        # (c, r, face) -> rede
        self.pin_of_hole = {}     # (c, r) -> (ref, pino)
        self.pin_net = {}         # (c, r) -> rede
        self.shorts = []
        self.pin_cells = {}       # rede -> [furos]

        cell_owner = {}
        for ref in layout.placements:
            for pin, cell in layout.pin_holes(ref).items():
                net = self._net_name(ref, pin)
                if cell in cell_owner and cell_owner[cell][0] != net:
                    self.shorts.append({
                        "cell": list(cell),
                        "a": "%s.%s (%s)" % (cell_owner[cell][1], cell_owner[cell][2],
                                             cell_owner[cell][0]),
                        "b": "%s.%s (%s)" % (ref, pin, net),
                    })
                    continue
                cell_owner[cell] = (net, ref, pin)
                self.pin_of_hole[cell] = (ref, pin)
                if net:
                    self.pin_net[cell] = net
                    for face in (BOTTOM, TOP):
                        self.pin_pads[(cell[0], cell[1], face)] = net
                    self.pin_cells.setdefault(net, [])
                    if cell not in self.pin_cells[net]:
                        self.pin_cells[net].append(cell)

        # furos cobertos pelo corpo das pecas: trilha do lado de cima nao passa por ali
        self.under_parts = set()
        for ref in layout.placements:
            self.under_parts |= layout.body_cells(ref)
        self.under_parts -= set(self.pin_of_hole)

        self.history_pad = {}
        self.history_edge = {}
        # muda sempre que o rancor e atualizado; a ponte para o C usa isto para
        # remontar os vetores de historico so quando realmente precisa
        self._hist_versao = 0
        self._ids_rede_c = {}       # nome da rede -> inteiro, para o nucleo em C

        # Callback opcional de progresso. Uma busca dificil leva minutos; ficar mudo
        # esse tempo todo faz parecer travado, entao avisamos a cada rodada quantas
        # redes ainda faltam.
        self.progresso = None

    def _net_name(self, ref, pin):
        return self._net_index.get((ref, pin))

    def _avisa(self, **dados):
        if self.progresso is None:
            return
        try:
            self.progresso(dados)
        except Exception as exc:
            # nunca deixar o relato de progresso derrubar o roteamento - mas tambem
            # nao sumir com o erro, que foi como um bug meu ficou escondido aqui
            import sys
            print("aviso: relato de progresso falhou (%s: %s)" % (type(exc).__name__, exc),
                  file=sys.stderr)
            self.progresso = None

    # ------------------------------------------------------------------

    def route(self) -> dict:
        cfg = self.cfg
        rng = random.Random(cfg.seed)

        nets = [n for n in self.netlist.routable_nets()
                if len(self.pin_cells.get(n.name, [])) >= 2]
        skipped = [n.name for n in self.netlist.routable_nets()
                   if len(self.pin_cells.get(n.name, [])) < 2]

        def tightness(net):
            cells = self.pin_cells[net.name]
            xs = [c for c, _ in cells]
            ys = [r for _, r in cells]
            area = (max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1)
            return (-len(cells), area)

        order = sorted(nets, key=tightness)

        # Fase 1: negociacao de congestionamento. Todo mundo pega o melhor caminho,
        # mesmo pisando no do vizinho; quem tem alternativa mais barata vai cedendo.
        negociado = self._negociar(list(order))
        if negociado is not None:
            routes, res = negociado
            return self._report(routes, res, skipped)

        # Fase 2: nao convergiu. Modo estrito, que sempre devolve solucao legal.
        # Roda DUAS vezes: uma aproveitando o rancor que a negociacao aprendeu e
        # outra do zero. O rancor as vezes ajuda e as vezes atrapalha - deixar as
        # duas correrem e ficar com a melhor evita que a fase 1 piore o resultado.
        aprendido = (dict(self.history_pad), dict(self.history_edge))
        best = None
        for hist in (aprendido, ({}, {})):
            self.history_pad, self.history_edge = dict(hist[0]), dict(hist[1])
            r = self._estrito(list(order), rng)
            if best is None or r[0] < best[0]:
                best = r
            if best[0][0] == 0:
                break

        _score, routes, res = best
        return self._report(routes, res, skipped)

    def _orfaos(self, routes) -> int:
        """Quantos pinos ficam de fato sem ligacao, pela auditoria independente.

        E por este numero que comparamos tentativas: contar *redes* que falharam
        engana, porque uma solucao com menos redes quebradas pode deixar mais
        pinos soltos - que e o que o usuario sente na bancada.
        """
        total = 0
        for name, r in routes.items():
            rr = NetRoute(name=name, ok=r.ok, segments=merge_collinear(r.segments))
            self._audit(rr)
            total += len(rr.orphans)
        return total

    def _estrito(self, order, rng):
        """Rip-up & reroute com restricao dura. Nunca devolve layout ilegal."""
        cfg = self.cfg
        best = None
        for _attempt in range(max(1, cfg.attempts)):
            res = _Resources(cfg.faces, self.pin_pads, self.history_pad, self.history_edge)
            routes = {}
            failed = []

            for net in order:
                r = self._route_net(net, res)
                routes[net.name] = r
                if not r.ok:
                    failed.append(net)

            score = (self._orfaos(routes), len(failed),
                     sum(_seg_len(s) for r in routes.values() for s in r.segments))
            if best is None or score < best[0]:
                best = (score, routes, res)
            self._avisa(fase="legalizando", rodada=_attempt + 1, de=cfg.attempts,
                        soltos=score[0], melhor=best[0][0])
            if not failed:
                break

            for net in failed:
                self._penalize_area(net, routes)
            rest = [n for n in order if n not in failed]
            rng.shuffle(rest)
            order = failed + rest
        return best

    def _negociar(self, order):
        """PathFinder: rodadas com custo de presenca crescente ate ninguem se sobrepor.

        Devolve `(routes, res)` quando chega a uma solucao legal, ou None se nao
        convergir dentro do orcamento de rodadas.
        """
        cfg = self.cfg
        pres = 0.35
        res = _Resources(cfg.faces, self.pin_pads, self.history_pad, self.history_edge,
                         soft=True, pres=pres)
        routes = {}
        rodadas = max(6, cfg.attempts * 3)

        for rodada in range(rodadas):
            res.pres = pres
            for net in order:
                if net.name in routes:
                    res.release(net.name)
                r = self._route_net(net, res)
                routes[net.name] = r

            pads, edges = res.disputados()
            orfaos = self._orfaos(routes)
            self._avisa(fase="negociando", rodada=rodada + 1, de=rodadas,
                        disputados=len(pads) + len(edges), soltos=orfaos)
            if not pads and not edges and all(r.ok for r in routes.values()) and orfaos == 0:
                return routes, res

            # rancor: recurso disputado fica permanentemente mais caro
            g = cfg.history_gain
            for k, n in pads.items():
                self.history_pad[k] = self.history_pad.get(k, 0.0) + g * (n - 1)
            for k, n in edges.items():
                self.history_edge[k] = self.history_edge.get(k, 0.0) + g * (n - 1)
            self._hist_versao += 1
            pres *= 1.7

            if rodada == rodadas - 1:
                break
        return None

    # ------------------------------------------------------------------

    def _penalize_area(self, net, routes):
        cells = self.pin_cells[net.name]
        xs = [c for c, _ in cells]
        ys = [r for _, r in cells]
        pad = 2
        x0, x1 = min(xs) - pad, max(xs) + pad
        y0, y1 = min(ys) - pad, max(ys) + pad
        g = self.cfg.history_gain
        for r in routes.values():
            if r.name == net.name:
                continue
            for node in r.holes:
                c, rr = node[0], node[1]
                if x0 <= c <= x1 and y0 <= rr <= y1:
                    self.history_pad[node] = self.history_pad.get(node, 0.0) + g
            for kind, a, b, face in r.segments:
                if kind not in ("trace", "trace_top"):
                    continue
                if x0 <= a[0] <= x1 and y0 <= a[1] <= y1:
                    k = (_edge_key(a, b), face)
                    self.history_edge[k] = self.history_edge.get(k, 0.0) + g
        self._hist_versao += 1

    # ------------------------------------------------------------------

    def _route_net(self, net, res: _Resources) -> NetRoute:
        name = net.name
        targets = list(self.pin_cells[name])
        out = NetRoute(name=name)

        def anexa(node):
            """Marca um no como conectado.

            Entra sempre com direcao LIVRE: de uma junta ja soldada da para sair
            para qualquer lado sem que isso conte como dobra. Em furo de terminal,
            o proprio terminal ja liga as duas faces, entao as duas entram.
            """
            cell = (node[0], node[1])
            faces = (BOTTOM, TOP) if self.pin_net.get(cell) == name else (node[2],)
            for face in faces:
                tree.add((cell[0], cell[1], face, LIVRE))
                k = (cell[0], cell[1], face)
                if k not in out.holes:
                    out.holes.append(k)

        tree = set()
        anexa((targets[0][0], targets[0][1], BOTTOM, LIVRE))
        remaining = set(targets[1:])

        while remaining:
            path = self._astar(tree, remaining, name, res)
            if path is None:
                out.ok = False
                faltando = sorted("%s.%s" % self.pin_of_hole.get(t, ("?", "?"))
                                  for t in remaining)
                out.reason = "sem caminho livre ate %s" % ", ".join(faltando[:4])
                return out

            for i in range(len(path) - 1):
                a = path[i][0]
                b, kind = path[i + 1]
                ca, cb = (a[0], a[1]), (b[0], b[1])
                if kind == "lead":
                    # troca de face pelo proprio terminal do componente: nada a construir
                    res.take_pad(ca, a[2], name)
                    res.take_pad(cb, b[2], name)
                elif kind == "via":
                    out.segments.append(("via", ca, cb, None))
                    res.take_pad(ca, a[2], name)
                    res.take_pad(cb, b[2], name)
                else:
                    out.segments.append((kind, ca, cb, b[2]))
                    res.take_pad(ca, a[2], name)
                    res.take_pad(cb, b[2], name)
                    if kind in ("trace", "trace_top"):
                        res.take_edge(ca, cb, b[2], name)
                anexa(a)
                anexa(b)

            fim = path[-1][0]
            remaining.discard((fim[0], fim[1]))
            remaining -= {(n[0], n[1]) for n in tree}
        return out

    # ------------------------------------------------------------------

    def _astar(self, sources, goals, net, res: _Resources):
        # O nucleo em C faz a mesma busca, so que muito mais rapido. Se nao estiver
        # compilado, segue pelo caminho em Python logo abaixo - o resultado e
        # equivalente, ha teste diferencial comparando os dois.
        if nativo.disponivel():
            return nativo.astar(self, sources, goals, net, res)
        return self._astar_py(sources, goals, net, res)

    def _astar_py(self, sources, goals, net, res: _Resources):
        cfg = self.cfg
        goal_cells = set(goals)
        passo_min = min([cfg.trace_cost]
                        + ([cfg.top_trace_cost] if cfg.two_sided else [])
                        + ([cfg.jumper_per_hole] if cfg.allow_jumpers else []))
        passo_min = max(0.01, passo_min)

        def h(node):
            c, r = node[0], node[1]
            return passo_min * min(abs(c - gc) + abs(r - gr) for gc, gr in goal_cells)

        # a heuristica so pode contar o passo mais barato por furo, nunca a dobra,
        # senao deixa de ser admissivel e o A* pode devolver caminho pior

        openq = []
        came = {}
        gscore = {}
        for s in sources:
            gscore[s] = 0.0
            came[s] = None
            heapq.heappush(openq, (h(s), 0.0, s, "start"))

        closed = set()
        while openq:
            _f, g, cur, _kind = heapq.heappop(openq)
            if cur in closed:
                continue
            closed.add(cur)
            if (cur[0], cur[1]) in goal_cells:
                return self._unwind(came, cur)

            for nxt, nkind, cost in self._neighbors(cur, net, res, goal_cells):
                if nxt in closed:
                    continue
                ng = g + cost
                if ng < gscore.get(nxt, float("inf")) - 1e-9:
                    gscore[nxt] = ng
                    came[nxt] = (cur, nkind)
                    heapq.heappush(openq, (ng + h(nxt), ng, nxt, nkind))
        return None

    def _unwind(self, came, node):
        path = []
        cur = node
        while cur is not None:
            prev = came.get(cur)
            if prev is None:
                path.append((cur, "start"))
                break
            p, kind = prev
            path.append((cur, kind))
            cur = p
        path.reverse()
        return path

    def _top_blocked(self, cell, net):
        """Trilha do lado dos componentes nao passa por baixo do corpo das pecas."""
        return cell in self.under_parts

    def _neighbors(self, node, net, res, goal_cells):
        cfg = self.cfg
        spec = self.spec
        c, r, face, veio = node
        cell = (c, r)
        out = []

        # 1) trilha, andando furo a furo na mesma face
        passo = cfg.trace_cost if face == BOTTOM else cfg.top_trace_cost
        kind = "trace" if face == BOTTOM else "trace_top"
        if face == BOTTOM or cfg.two_sided:
            for i, (dc, dr) in enumerate(DIRS):
                viz = (c + dc, r + dr)
                if not spec.contains(*viz):
                    continue
                if face == TOP and self._top_blocked(viz, net):
                    continue
                if res.pad_blocked(viz, face, net) or res.edge_blocked(cell, viz, face, net):
                    continue
                # dobrar o fio custa: numa perfboard a quina e trabalho manual,
                # e por isso um trecho reto e longo vence um desvio curto e torto
                dobra = cfg.turn_cost if (veio != LIVRE and veio != i) else 0.0
                out.append(((viz[0], viz[1], face, i), kind,
                            passo + dobra
                            + res.pad_extra(viz, face, net)
                            + res.edge_extra(cell, viz, face, net)))

        # 2) via: troca de face dentro do mesmo furo
        if cfg.two_sided:
            outra = TOP if face == BOTTOM else BOTTOM
            if not res.pad_blocked(cell, outra, net):
                if not (outra == TOP and self._top_blocked(cell, net)):
                    # no furo de um terminal desta rede a troca ja vem de graca pelo proprio terminal
                    gratis = self.pin_net.get(cell) == net
                    out.append(((c, r, outra, LIVRE), "lead" if gratis else "via",
                                (0.0 if gratis else cfg.via_cost)
                                + res.pad_extra(cell, outra, net)))

        if not cfg.allow_jumpers:
            return out

        # O furo que ja tem terminal de componente nao comporta a ponta de um jumper:
        # ou o furo tem o pino, ou tem o fio. Entao o jumper so parte de furo livre e
        # so chega em furo livre - do pino ate esse furo vai uma trilha curta, que e
        # como se faz na bancada.
        if cell in self.pin_of_hole:
            return out

        # 3) jumper reto, pulando obstaculos sem tocar nos furos do meio
        for dc, dr in DIRS:
            for k in range(2, cfg.max_jumper + 1):
                alvo = (c + dc * k, r + dr * k)
                if not spec.contains(*alvo):
                    break
                if alvo in self.pin_of_hole:
                    continue
                if res.pad_blocked(alvo, face, net):
                    continue
                out.append(((alvo[0], alvo[1], face, LIVRE), "jumper",
                            cfg.jumper_base + cfg.jumper_per_hole * k
                            + res.pad_extra(alvo, face, net)))

        # 4) jumper direto ate a vizinhanca de um destino da propria rede
        for gx, gy in goal_cells:
            for dc, dr in DIRS:
                alvo = (gx + dc, gy + dr)
                if alvo == cell or alvo in self.pin_of_hole:
                    continue
                if not spec.contains(*alvo):
                    continue
                d = math.hypot(alvo[0] - c, alvo[1] - r)
                if d < 2 or d > cfg.max_jumper:
                    continue
                if res.pad_blocked(alvo, face, net):
                    continue
                out.append(((alvo[0], alvo[1], face, LIVRE), "jumper",
                            cfg.jumper_base + cfg.jumper_per_hole * d
                            + res.pad_extra(alvo, face, net)))

        return out

    # ------------------------------------------------------------------

    def _audit(self, route: NetRoute):
        """Confere de verdade se todos os pinos da rede caem no mesmo grafo.

        Nao confia na contabilidade do laco de roteamento: reconstroi as ligacoes
        a partir dos segmentos que serao efetivamente soldados.
        """
        adj = {}

        def liga(a, b):
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)

        for kind, a, b, face in route.segments:
            if kind == "via":
                liga((a[0], a[1], BOTTOM), (a[0], a[1], TOP))
            elif kind in ("trace", "trace_top"):
                holes = expand_trace(a, b)
                for i in range(len(holes) - 1):
                    liga((holes[i][0], holes[i][1], face),
                         (holes[i + 1][0], holes[i + 1][1], face))
            else:  # jumper
                liga((a[0], a[1], face), (b[0], b[1], face))

        pins = list(self.pin_cells.get(route.name, []))
        if len(pins) < 2:
            return
        # o terminal do componente atravessa o furo e liga as duas faces
        for cell in pins:
            liga((cell[0], cell[1], BOTTOM), (cell[0], cell[1], TOP))

        inicio = (pins[0][0], pins[0][1], BOTTOM)
        seen = {inicio}
        stack = [inicio]
        while stack:
            cur = stack.pop()
            for nxt in adj.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)

        soltos = [c for c in pins[1:]
                  if (c[0], c[1], BOTTOM) not in seen and (c[0], c[1], TOP) not in seen]
        if not soltos:
            return

        route.ok = False
        route.orphans = []
        for cell in soltos:
            ref, pin = self.pin_of_hole.get(cell, ("?", "?"))
            route.orphans.append({"ref": ref, "pin": pin, "cell": list(cell)})
        nomes = ", ".join("%s.%s" % (o["ref"], o["pin"]) for o in route.orphans[:6])
        route.reason = "sem ligacao ate %s" % nomes

    def _report(self, routes, res, skipped):
        merged = {}
        for name, r in routes.items():
            rr = NetRoute(name=name, ok=r.ok, reason=r.reason, holes=r.holes)
            rr.segments = merge_collinear(r.segments)
            self._audit(rr)
            merged[name] = rr

        def total(kind):
            return sum(_seg_len(s) * PITCH_MM
                       for r in merged.values() for s in r.segments if s[0] == kind)

        def contar(kind):
            return sum(1 for r in merged.values() for s in r.segments if s[0] == kind)

        def quinas(route):
            """Furos onde o fio da rede muda de eixo: cada um e uma dobra a fazer."""
            eixos = {}
            for kind, a, b, face in route.segments:
                if kind not in ("trace", "trace_top"):
                    continue
                eixo = "h" if a[1] == b[1] else "v"
                for ponta in (a, b):
                    eixos.setdefault((ponta, face), set()).add(eixo)
            return sum(1 for e in eixos.values() if len(e) > 1)

        n_quinas = sum(quinas(r) for r in merged.values())
        corridas = contar("trace") + contar("trace_top")
        trace_mm = total("trace")
        top_mm = total("trace_top")
        jumper_mm = total("jumper")
        failed = [r.name for r in merged.values() if not r.ok]
        orphan_pins = [
            {"net": r.name, "ref": o["ref"], "pin": o["pin"], "cell": o["cell"]}
            for r in merged.values() for o in r.orphans
        ]

        return {
            "routes": [merged[k].to_json() for k in sorted(merged)],
            "stats": {
                "faces": self.cfg.faces,
                "nets_routed": len(merged) - len(failed),
                "nets_failed": len(failed),
                "failed": failed,
                "orphan_pins": orphan_pins,
                "skipped": skipped,
                "jumpers": contar("jumper"),
                "vias": contar("via"),
                "quinas": n_quinas,
                "corridas": corridas,
                "operacoes": n_quinas + contar("via") + contar("jumper") + corridas,
                "trace_mm": round(trace_mm, 1),
                "top_trace_mm": round(top_mm, 1),
                "jumper_mm": round(jumper_mm, 1),
                "total_mm": round(trace_mm + top_mm + jumper_mm, 1),
                "holes_used": len(res.hole_nets()),
            },
            "shorts": self.shorts,
            "occupancy": {
                "holes": {"%d,%d" % k: v for k, v in res.hole_nets().items()},
            },
        }


def merge_collinear(segments):
    """Junta passos consecutivos de trilha na mesma direcao e na mesma face."""
    saida = []
    for kind in ("trace", "trace_top"):
        for face in (BOTTOM, TOP):
            edges = {_edge_key(a, b) for k, a, b, f in segments if k == kind and f == face}
            if not edges:
                continue
            usadas = set()
            for chave in sorted(edges):
                if chave in usadas:
                    continue
                a, b = chave
                dc = (b[0] > a[0]) - (b[0] < a[0])
                dr = (b[1] > a[1]) - (b[1] < a[1])
                inicio = a
                while True:
                    ant = (inicio[0] - dc, inicio[1] - dr)
                    k = _edge_key(ant, inicio)
                    if k in edges and k not in usadas:
                        inicio = ant
                    else:
                        break
                fim = inicio
                while True:
                    prox = (fim[0] + dc, fim[1] + dr)
                    k = _edge_key(fim, prox)
                    if k in edges and k not in usadas:
                        usadas.add(k)
                        fim = prox
                    else:
                        break
                saida.append((kind, inicio, fim, face))

    vistos = set()
    for seg in segments:
        if seg[0] in ("trace", "trace_top"):
            continue
        if seg[0] == "via":
            chave = ("via", seg[1])
            if chave in vistos:
                continue
            vistos.add(chave)
        saida.append(seg)
    return saida
