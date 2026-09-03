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

import atexit
import multiprocessing
import os
import queue
import threading

import json

from . import footprints as fpmod
from .board import BoardSpec
from .netlist import parse_netlist
from .placer import auto_place
from .router import Router, RouterConfig

# Estado por processo trabalhador. Netlist e biblioteca de footprints sao iguais em
# todas as tentativas da MESMA busca, entao cada processo monta uma vez e guarda.
# A chave e a assinatura do pedido: mudou o projeto, remonta; nao mudou, reaproveita.
_CTX = {}

# Numero da busca que esta valendo agora, visto por todos os processos. O
# trabalhador compara com o numero que veio na tarefa: se nao bate, a busca que
# pediu aquilo ja acabou e o resultado nao interessa mais a ninguem.
_GERACAO = None


def nucleos_padrao() -> int:
    """Quantos processos usar: a maquina inteira.

    Cada tentativa e um sorteio independente, entao mais processos e literalmente
    mais chances por segundo de fechar o circuito. Usamos todos os nucleos logicos.

    Deixar nucleo de reserva parecia educado, mas nao se sustenta: o trabalho vem
    em tentativas de menos de um segundo, o sistema operacional intercala sozinho, e
    a interface fica esperando resposta de qualquer jeito. `PERFBOARD_NUCLEOS`
    limita, para quem quiser usar a maquina para outra coisa ao mesmo tempo.
    """
    forcado = os.environ.get("PERFBOARD_NUCLEOS")
    if forcado:
        try:
            return max(1, int(forcado))
        except ValueError:
            pass
    try:
        return max(1, multiprocessing.cpu_count())
    except NotImplementedError:
        return 1


def _guarda_geracao(compartilhada):
    """Guarda no processo trabalhador o contador vindo do pai."""
    global _GERACAO
    _GERACAO = compartilhada


def _prepara(assinatura, args):
    """Monta (ou reaproveita) o contexto deste processo trabalhador."""
    global _CTX
    if _CTX.get("assinatura") == assinatura:
        return _CTX
    netlist_text, board_json, overrides, router_json, placer_json, placements, decoupling = args
    nl = parse_netlist(netlist_text)
    _CTX = {
        "assinatura": assinatura,
        "nl": nl,
        "lib": fpmod.build_library(nl, overrides or {}),
        "spec": BoardSpec.from_json(board_json or {}),
        "rcfg": RouterConfig.from_json(router_json or {}),
        "pcfg": placer_json or {},
        "placements": placements,
        "decoupling": decoupling or [],
    }
    return _CTX


def _uma(tarefa):
    """Uma tentativa completa: posiciona, roteia, devolve o que interessa.

    Devolve as posicoes (e nao a semente) porque a mesma semente NAO reproduz o
    mesmo layout: a ordem de iteracao de conjuntos de strings muda a cada processo
    do Python. Ja tomamos esse susto - o unico jeito seguro de guardar um resultado
    e guardar as coordenadas.
    """
    from .project import build_layout          # tardio: evita import circular
    assinatura, args, semente, geracao, pesos_de_rede = tarefa
    # A busca que pediu esta tarefa ja terminou? Entao nem comeca: sao 0,8s de CPU
    # que a busca atual precisa mais do que este resultado.
    if _GERACAO is not None and geracao != _GERACAO.value:
        return None
    c = _prepara(assinatura, args)
    layout = build_layout(c["nl"], c["spec"], c["lib"], c["placements"])
    relatorio = auto_place(
        layout, c["nl"], seed=semente,
        effort=c["pcfg"].get("effort", "alto"),
        keep_existing=bool(c["pcfg"].get("keep_existing", False)),
        edge_pull=float(c["pcfg"].get("edge_pull", 0.6)),
        decoupling=c["decoupling"],
        # o que as tentativas anteriores aprenderam sobre quais redes nao fecham;
        # vem por tarefa, e nao no contexto, porque muda a cada rodada
        pesos_de_rede=pesos_de_rede,
    )
    resultado = Router(layout, c["nl"], c["rcfg"]).route()
    return {
        "semente": semente,
        "placements": [p.to_json() for p in layout.placements.values()],
        "resultado": resultado,
        "relatorio": relatorio,
        "problemas": layout.problems(),
    }


# Pool compartilhado, vivo entre buscas. Abrir 10 processos custa ~2,3s e fecha-los
# outros ~2,5s; pagar isso a cada busca era a maior parte da espera numa placa que
# fecha em duas tentativas. Ele morre junto com o processo (atexit).
_TRAVA = threading.Lock()
_POOL = None
_POOL_N = 0


def proxima_geracao():
    """Marca o comeco de uma busca nova e devolve o numero dela."""
    with _TRAVA:
        if _GERACAO is None:
            return 0
        _GERACAO.value += 1
        return _GERACAO.value


def _pool_de(nucleos):
    """Devolve o pool compartilhado, criando ou redimensionando se precisar."""
    global _POOL, _POOL_N
    with _TRAVA:
        if _POOL is not None and _POOL_N == nucleos:
            return _POOL
        if _POOL is not None:
            _POOL.terminate()       # so acontece quando o tamanho muda: e raro
            _POOL.join()
        ctx = multiprocessing.get_context("spawn")
        global _GERACAO
        if _GERACAO is None:
            _GERACAO = ctx.Value("q", 0)
        _POOL = ctx.Pool(nucleos, initializer=_guarda_geracao, initargs=(_GERACAO,))
        _POOL_N = nucleos
        return _POOL


def encerra_pool():
    """Derruba o pool compartilhado. Chamado no fim do processo."""
    global _POOL, _POOL_N
    with _TRAVA:
        if _POOL is not None:
            _POOL.terminate()
            _POOL.join()
            _POOL = None
            _POOL_N = 0


atexit.register(encerra_pool)


class Bando:
    """Entrega tentativas na ordem em que ficam prontas, usando o pool compartilhado.

    Uso:
        with Bando(...) as bando:
            for r in bando.resultados(semente_inicial):
                ...        # sair do for para de pedir tentativas
    """

    def __init__(self, netlist_text, board_json, overrides, router_json,
                 placer_json, placements, decoupling, nucleos=None):
        self.nucleos = max(1, int(nucleos or nucleos_padrao()))
        self._args = (netlist_text, board_json, overrides, router_json,
                      placer_json, placements, decoupling)
        # Assinatura do pedido: os trabalhadores remontam o contexto so quando ela
        # muda. Basta ser estavel e barata - nao precisa ser criptografica.
        self._pesos = {}
        self._assinatura = hash((netlist_text, json.dumps(board_json, sort_keys=True),
                                 json.dumps(overrides or {}, sort_keys=True),
                                 json.dumps(router_json or {}, sort_keys=True),
                                 json.dumps(placer_json or {}, sort_keys=True),
                                 json.dumps(placements or [], sort_keys=True),
                                 json.dumps(decoupling or [], sort_keys=True)))
        self._pool = None

    def __enter__(self):
        self._pool = _pool_de(self.nucleos)
        self._geracao = proxima_geracao()
        return self

    def __exit__(self, *_):
        # O pool fica de pe para a proxima busca. Virar a geracao faz as tentativas
        # ainda engatilhadas voltarem vazias em vez de gastarem CPU - so as que ja
        # estavam rodando terminam, e sao no maximo uma por nucleo.
        proxima_geracao()
        self._pool = None
        return False

    def pesos(self, novos):
        """Atualiza o que o posicionador vai saber nas proximas tentativas."""
        self._pesos = dict(novos or {})

    def resultados(self, semente0):
        """Gera resultados sem fim, um por tentativa, na ordem de conclusao.

        A busca nao tem tamanho definido - ela para quando fecha, estaciona ou o
        usuario manda parar. Por isso alimentamos o pool com uma JANELA de tarefas
        em voo em vez de despejar a lista toda: `imap_unordered` sobre um gerador
        infinito enfileiraria tarefas para sempre e comeria a memoria.

        A janela e um pouco maior que o numero de nucleos: sobra trabalho engatilhado
        para ninguem ficar ocioso entre uma entrega e outra, sem encher a fila de
        tentativas que serao descartadas quando a busca acabar.
        """
        fila = queue.Queue()
        janela = self.nucleos + max(2, self.nucleos // 3)
        prox = semente0
        em_voo = 0
        try:
            while True:
                while em_voo < janela:
                    self._pool.apply_async(
                        _uma, ((self._assinatura, self._args, prox, self._geracao,
                                dict(self._pesos)),),
                        callback=lambda r: fila.put(("ok", r)),
                        error_callback=lambda e: fila.put(("erro", e)))
                    prox += 1
                    em_voo += 1
                marca, carga = fila.get()
                em_voo -= 1
                if marca == "erro":
                    raise carga
                if carga is None:
                    continue        # tarefa de uma busca ja encerrada
                yield carga
        except GeneratorExit:
            pass        # quem consumia saiu do laco; __exit__ derruba o pool
