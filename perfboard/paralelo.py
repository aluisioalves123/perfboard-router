"""Roda varias tentativas ao mesmo tempo, uma por nucleo.

Cada tentativa e um sorteio independente: posiciona com uma semente propria e
roteia. Nada e compartilhado entre elas, entao espalhar por processos e quase de
graca - e a maquina tem 8, 12, 16 nucleos parados enquanto um so trabalha.

Por que processo e nao thread: o trabalho e CPU puro em Python, e o GIL faria as
threads se revezarem no mesmo nucleo. Nao adiantaria nada.

O laco continua em `project.solve()`, que so chama `tentativas()` e recebe os
resultados na ordem em que ficam prontos. Assim a regra de parada (fechou 100%,
estacionou, usuario mandou parar) fica num lugar so.
"""
from __future__ import annotations

import multiprocessing
import os
import queue

from . import footprints as fpmod
from .board import BoardSpec
from .netlist import parse_netlist
from .placer import auto_place
from .router import Router, RouterConfig

# Estado por processo trabalhador. Netlist e biblioteca de footprints sao iguais em
# todas as tentativas, entao sao montadas uma vez na largada de cada processo.
_CTX = {}


def nucleos_padrao() -> int:
    """Quantos processos usar sem sufocar a maquina.

    Deixa dois nucleos livres: um para o servidor responder e outro para o usuario
    continuar mexendo no computador sem travar.
    """
    try:
        n = multiprocessing.cpu_count()
    except NotImplementedError:
        return 1
    return max(1, min(12, n - 2))


def _abre(netlist_text, board_json, overrides, router_json, placer_json,
          placements, decoupling):
    global _CTX
    nl = parse_netlist(netlist_text)
    _CTX = {
        "nl": nl,
        "lib": fpmod.build_library(nl, overrides or {}),
        "spec": BoardSpec.from_json(board_json or {}),
        "rcfg": RouterConfig.from_json(router_json or {}),
        "pcfg": placer_json or {},
        "placements": placements,
        "decoupling": decoupling or [],
    }
    # Um trabalhador nao precisa de prioridade: quem esta na frente do computador
    # precisa. Em Windows e Linux o nome da constante muda, dai o getattr.
    try:
        baixo = getattr(os, "IDLE_PRIORITY_CLASS", None)
        if baixo is not None:
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(), baixo)
        elif hasattr(os, "nice"):
            os.nice(5)
    except Exception:
        pass        # prioridade e conforto, nao requisito


def _uma(semente):
    """Uma tentativa completa: posiciona, roteia, devolve o que interessa.

    Devolve as posicoes (e nao a semente) porque a mesma semente NAO reproduz o
    mesmo layout: a ordem de iteracao de conjuntos de strings muda a cada processo
    do Python. Ja tomamos esse susto - o unico jeito seguro de guardar um resultado
    e guardar as coordenadas.
    """
    from .project import build_layout          # tardio: evita import circular
    c = _CTX
    layout = build_layout(c["nl"], c["spec"], c["lib"], c["placements"])
    relatorio = auto_place(
        layout, c["nl"], seed=semente,
        effort=c["pcfg"].get("effort", "alto"),
        keep_existing=bool(c["pcfg"].get("keep_existing", False)),
        edge_pull=float(c["pcfg"].get("edge_pull", 0.6)),
        decoupling=c["decoupling"],
    )
    resultado = Router(layout, c["nl"], c["rcfg"]).route()
    return {
        "semente": semente,
        "placements": [p.to_json() for p in layout.placements.values()],
        "resultado": resultado,
        "relatorio": relatorio,
        "problemas": layout.problems(),
    }


class Bando:
    """Pool de processos que entrega tentativas na ordem em que ficam prontas.

    Uso:
        with Bando(...) as bando:
            for r in bando.resultados(semente_inicial):
                ...        # sair do for encerra tudo
    """

    def __init__(self, netlist_text, board_json, overrides, router_json,
                 placer_json, placements, decoupling, nucleos=None):
        self.nucleos = max(1, int(nucleos or nucleos_padrao()))
        self._args = (netlist_text, board_json, overrides, router_json,
                      placer_json, placements, decoupling)
        self._pool = None

    def __enter__(self):
        ctx = multiprocessing.get_context("spawn")
        self._pool = ctx.Pool(self.nucleos, initializer=_abre, initargs=self._args)
        return self

    def __exit__(self, *_):
        if self._pool is not None:
            self._pool.terminate()      # nao esperar: quem saiu do laco quer parar ja
            self._pool.join()
            self._pool = None
        return False

    def resultados(self, semente0):
        """Gera resultados sem fim, um por tentativa, na ordem de conclusao.

        A busca nao tem tamanho definido - ela para quando fecha, estaciona ou o
        usuario manda parar. Por isso alimentamos o pool com uma JANELA de tarefas
        em voo em vez de despejar a lista toda: `imap_unordered` sobre um gerador
        infinito enfileiraria tarefas para sempre e comeria a memoria.

        A janela e o dobro dos nucleos: sobra trabalho engatilhado para ninguem
        ficar ocioso entre uma entrega e outra, sem acumular tentativas jogadas
        fora quando o laco parar.
        """
        fila = queue.Queue()
        janela = self.nucleos * 2
        prox = semente0
        em_voo = 0
        try:
            while True:
                while em_voo < janela:
                    self._pool.apply_async(
                        _uma, (prox,),
                        callback=lambda r: fila.put(("ok", r)),
                        error_callback=lambda e: fila.put(("erro", e)))
                    prox += 1
                    em_voo += 1
                marca, carga = fila.get()
                em_voo -= 1
                if marca == "erro":
                    raise carga
                yield carga
        except GeneratorExit:
            pass        # quem consumia saiu do laco; __exit__ derruba o pool
