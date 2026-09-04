"""Ponte para o nucleo em C do A*, com queda automatica para o Python.

O programa nunca depende disto: se a biblioteca nao estiver compilada, ou se falhar
ao carregar, `disponivel()` devolve False e o roteador usa a implementacao em Python.
A unica diferenca e velocidade.

Para compilar:
    cd native && gcc -O2 -shared -o perfboard.dll perfboard.c        (Windows)
    cd native && gcc -O2 -shared -fPIC -o perfboard.so perfboard.c   (Linux)
"""
from __future__ import annotations

import ctypes
import os
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASTA = os.path.join(RAIZ, "native")

# ligado por padrao; PERFBOARD_SEM_C=1 forca o caminho em Python (usado nos testes)
_DESLIGADO = os.environ.get("PERFBOARD_SEM_C") == "1"

BOTTOM, TOP = 0, 1
LIVRE = 4
TIPOS = {0: "start", 1: "trace", 2: "trace_top", 3: "jumper", 4: "via", 5: "lead"}


# Tem de casar com PB_ASTAR_ABI em native/perfboard.c.
ABI_ESPERADA = 3


# Versao da configuracao do posicionador em C. Sobe sempre que PbPlaceCfg muda:
# uma DLL de outra versao leria os campos trocados, e o resultado seria plausivel
# e errado. Na divergencia, o Python assume sozinho.
PLACE_ABI = 2


class PbPlaceCfg(ctypes.Structure):
    _fields_ = [
        ("w_overlap_final", ctypes.c_double), ("w_overlap_inicial", ctypes.c_double),
        ("w_outside", ctypes.c_double), ("w_corpo_fora", ctypes.c_double),
        ("w_desacopla", ctypes.c_double), ("edge_pull", ctypes.c_double),
        ("w_densidade", ctypes.c_double),
        ("sem_saida_0", ctypes.c_double), ("sem_saida_1", ctypes.c_double),
        ("sem_saida_2", ctypes.c_double),
        ("folga_desacopla", ctypes.c_int), ("regioes", ctypes.c_int),
        ("proibir_sobreposicao", ctypes.c_int),
        ("passos", ctypes.c_int),
        ("semente", ctypes.c_ulonglong),
    ]


class PbConfig(ctypes.Structure):
    _fields_ = [
        ("cols", ctypes.c_int), ("rows", ctypes.c_int), ("faces", ctypes.c_int),
        ("trace_cost", ctypes.c_double), ("top_trace_cost", ctypes.c_double),
        ("turn_cost", ctypes.c_double), ("via_cost", ctypes.c_double),
        ("jumper_base", ctypes.c_double), ("jumper_per_hole", ctypes.c_double),
        ("max_jumper", ctypes.c_int), ("allow_jumpers", ctypes.c_int),
        ("pres_weight", ctypes.c_double), ("pres", ctypes.c_double),
        ("soft", ctypes.c_int),
        ("trilha_em_cima", ctypes.c_int),
    ]


def _carrega():
    if _DESLIGADO:
        return None
    nomes = ["perfboard.dll"] if sys.platform == "win32" else ["perfboard.so", "libperfboard.so"]
    for nome in nomes:
        caminho = os.path.join(PASTA, nome)
        if not os.path.isfile(caminho):
            continue
        try:
            lib = ctypes.CDLL(caminho)
            lib.pb_versao.restype = ctypes.c_int
            if lib.pb_versao() != 1:
                continue
            # Binario antigo com ponte nova nao da erro: da comportamento
            # indefinido, e o programa devolve numeros ruins que parecem legitimos.
            # Entao a interface tem versao, e sem ela combinar seguimos em Python.
            try:
                lib.pb_astar_abi.restype = ctypes.c_int
                if lib.pb_astar_abi() != ABI_ESPERADA:
                    continue
            except AttributeError:
                continue        # .dll de antes da versao: nao da para confiar
            lib.pb_astar.restype = ctypes.c_int
            lib.pb_astar.argtypes = [
                ctypes.POINTER(PbConfig),
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float),
                # sob_peca, tem_pino, eh_alvo
                ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_ubyte),
                ctypes.POINTER(ctypes.c_ubyte),
                ctypes.POINTER(ctypes.c_int), ctypes.c_int,
                ctypes.POINTER(ctypes.c_int), ctypes.c_int, ctypes.c_int,
                ctypes.POINTER(ctypes.c_int), ctypes.c_int,
            ]
            # o posicionador em C e opcional dentro do opcional: biblioteca antiga
            # sem `pb_place` continua servindo para o A*
            try:
                lib.pb_place_versao.restype = ctypes.c_int
                # a versao tem que bater exatamente: a configuracao passa como
                # struct, entao DLL de outra versao leria os campos errados
                if lib.pb_place_versao() == PLACE_ABI:
                    P = ctypes.POINTER
                    lib.pb_place.restype = ctypes.c_int
                    lib.pb_place.argtypes = [
                        P(PbPlaceCfg), ctypes.c_int, ctypes.c_int, ctypes.c_int,
                        ctypes.c_int, P(ctypes.c_int), P(ctypes.c_int),
                        P(ctypes.c_int), P(ctypes.c_int), P(ctypes.c_int),
                        P(ctypes.c_int), P(ctypes.c_ubyte), P(ctypes.c_ubyte),
                        ctypes.c_int, P(ctypes.c_int), P(ctypes.c_int), P(ctypes.c_int),
                        ctypes.c_int, P(ctypes.c_int), P(ctypes.c_int),
                        ctypes.c_int,
                        P(ctypes.c_int), P(ctypes.c_int), P(ctypes.c_int),
                    ]
                    lib._tem_place = True
            except AttributeError:
                lib._tem_place = False
            return lib
        except OSError:
            continue
    return None


_LIB = _carrega()


def disponivel() -> bool:
    return _LIB is not None


def tem_posicionador() -> bool:
    return _LIB is not None and getattr(_LIB, "_tem_place", False)


def descricao() -> str:
    if _DESLIGADO:
        return "nucleo C desligado por PERFBOARD_SEM_C=1"
    if _LIB is None:
        return ("nucleo C nao compilado (rodando em Python puro) - "
                "para acelerar: cd native && gcc -O2 -shared -fPIC -o perfboard.so perfboard.c")
    return "nucleo C ativo"


class Buffers:
    """Vetores reaproveitados entre chamadas, para nao alocar a cada busca."""

    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows
        n = cols * rows
        self.n = n
        self.pad_fixed = (ctypes.c_int * (n * 2))()
        self.pad_outras = (ctypes.c_int * (n * 2))()
        self.edge_outras = (ctypes.c_int * (n * 4))()
        self.hist_pad = (ctypes.c_float * (n * 2))()
        self.hist_edge = (ctypes.c_float * (n * 4))()
        self.sob_peca = (ctypes.c_ubyte * n)()
        self.tem_pino = (ctypes.c_ubyte * n)()
        self.eh_alvo = (ctypes.c_ubyte * n)()
        self.saida = (ctypes.c_int * (n * 2 * 3 + 96))()

        # o que nao muda entre chamadas so e montado uma vez
        self.fixos_prontos = False
        self.hist_versao = -1

    def zera_por_rede(self):
        ctypes.memset(self.pad_outras, 0, ctypes.sizeof(self.pad_outras))
        ctypes.memset(self.edge_outras, 0, ctypes.sizeof(self.edge_outras))
        ctypes.memset(self.eh_alvo, 0, ctypes.sizeof(self.eh_alvo))


def _chave_aresta(cols, a, b):
    """(furo_base, eixo) da aresta entre dois furos vizinhos."""
    ia, ib = a[1] * cols + a[0], b[1] * cols + b[0]
    base = ia if ia < ib else ib
    eixo = 0 if a[1] == b[1] else 1
    return base, eixo


def astar(router, sources, goals, net, res):
    """Executa a busca no nucleo C. Devolve o mesmo formato do `_astar` em Python.

    Furos fora da placa sao ignorados: quando uma peca nao cabe, seus pinos caem em
    coordenadas negativas ou alem da borda. Em Python isso e inofensivo (o furo
    simplesmente nunca e alcancado); aqui viraria indice invalido no vetor.
    """
    lib = _LIB
    spec = router.spec
    cols, rows = spec.cols, spec.rows
    n = cols * rows

    def dentro(c, r):
        return 0 <= c < cols and 0 <= r < rows

    buf = getattr(router, "_buffers_c", None)
    if buf is None or buf.cols != cols or buf.rows != rows:
        buf = Buffers(cols, rows)
        router._buffers_c = buf

    cfg = router.cfg
    duas = 2 if cfg.two_sided else 1

    # --- ilhas fixas (terminais) e mascaras: constantes durante todo o roteamento ---
    ids = router._ids_rede_c
    if not buf.fixos_prontos:
        ctypes.memset(buf.pad_fixed, 0xFF, ctypes.sizeof(buf.pad_fixed))   # -1
        for (c, r, face), nome in res.fixed.items():
            if not dentro(c, r):
                continue
            i = (r * cols + c) * 2 + (face if duas == 2 else 0)
            buf.pad_fixed[i] = ids.setdefault(nome, len(ids))
        ctypes.memset(buf.sob_peca, 0, n)
        for (c, r) in router.under_parts:
            if dentro(c, r):
                buf.sob_peca[r * cols + c] = 1
        ctypes.memset(buf.tem_pino, 0, n)
        for (c, r) in router.pin_of_hole:
            if dentro(c, r):
                buf.tem_pino[r * cols + c] = 1
        buf.fixos_prontos = True

    buf.zera_por_rede()
    meu = ids.setdefault(net, len(ids))

    # --- quantas OUTRAS redes usam cada recurso ---
    for (c, r, face), usuarios in res.pad.items():
        if not dentro(c, r):
            continue
        outras = len(usuarios) - (1 if net in usuarios else 0)
        if outras:
            buf.pad_outras[(r * cols + c) * 2 + (face if duas == 2 else 0)] = outras
    for ((a, b), face), usuarios in res.edge.items():
        if not (dentro(*a) and dentro(*b)):
            continue
        outras = len(usuarios) - (1 if net in usuarios else 0)
        if outras:
            base, eixo = _chave_aresta(cols, a, b)
            buf.edge_outras[(base * 2 + (face if duas == 2 else 0)) * 2 + eixo] = outras

    # --- historico: so muda quando uma rede falha e o rancor e atualizado ---
    if buf.hist_versao != router._hist_versao:
        ctypes.memset(buf.hist_pad, 0, ctypes.sizeof(buf.hist_pad))
        ctypes.memset(buf.hist_edge, 0, ctypes.sizeof(buf.hist_edge))
        for (c, r, face), v in router.history_pad.items():
            if not dentro(c, r):
                continue
            buf.hist_pad[(r * cols + c) * 2 + (face if duas == 2 else 0)] = v
        for ((a, b), face), v in router.history_edge.items():
            if not (dentro(*a) and dentro(*b)):
                continue
            base, eixo = _chave_aresta(cols, a, b)
            buf.hist_edge[(base * 2 + (face if duas == 2 else 0)) * 2 + eixo] = v
        buf.hist_versao = router._hist_versao

    # destino fora da placa e inalcancavel: some da lista, e se nao sobrar nenhum a
    # rede simplesmente nao fecha - mesmo desfecho que o caminho em Python
    alvos = []
    for (c, r) in goals:
        if not dentro(c, r):
            continue
        cell = r * cols + c
        buf.eh_alvo[cell] = 1
        alvos.append(cell)
    if not alvos:
        return None

    fontes = []
    for no in sources:
        c, r, face, veio = no
        if not dentro(c, r):
            continue
        fontes.append(((r * cols + c) * 2 + face) * 5 + veio)
    if not fontes:
        return None

    conf = PbConfig(
        cols=cols, rows=rows, faces=duas,
        trace_cost=cfg.trace_cost, top_trace_cost=cfg.top_trace_cost,
        turn_cost=cfg.turn_cost, via_cost=cfg.via_cost,
        jumper_base=cfg.jumper_base, jumper_per_hole=cfg.jumper_per_hole,
        max_jumper=cfg.max_jumper, allow_jumpers=1 if cfg.allow_jumpers else 0,
        pres_weight=res.pres_weight, pres=res.pres, soft=1 if res.soft else 0,
        # a folga em si ja veio embutida no mapa `sob_peca`; aqui so dizemos
        # se a face de cima esta em uso
        trilha_em_cima=1 if cfg.usa_face_de_cima else 0,
    )

    arr_alvos = (ctypes.c_int * len(alvos))(*alvos)
    arr_fontes = (ctypes.c_int * len(fontes))(*fontes)

    passos = lib.pb_astar(
        ctypes.byref(conf), buf.pad_fixed, buf.pad_outras, buf.edge_outras,
        buf.hist_pad, buf.hist_edge, buf.sob_peca, buf.tem_pino, buf.eh_alvo,
        arr_alvos, len(alvos), arr_fontes, len(fontes), meu,
        buf.saida, len(buf.saida),
    )
    if passos < 0:
        return None

    caminho = []
    for i in range(passos):
        cell = buf.saida[i * 3]
        face = buf.saida[i * 3 + 1]
        tipo = TIPOS.get(buf.saida[i * 3 + 2], "trace")
        caminho.append(((cell % cols, cell // cols, face, LIVRE), tipo))
    return caminho


# ---------------------------------------------------------------- posicionador

def posiciona(estado, passos, semente, pesos):
    """Roda o recozimento simulado no nucleo C sobre um `_State` ja montado.

    Recebe o estado do posicionador em Python, empacota em vetores planos, chama o
    C e escreve as posicoes de volta. Nada de logica nova aqui: as regras vivem no
    C e no Python, e este arquivo so traduz entre os dois.

    Devolve True se rodou; False se a biblioteca nao tem o posicionador (aí quem
    chamou segue pelo caminho em Python).
    """
    lib = _LIB
    if lib is None or not getattr(lib, "_tem_place", False):
        return False

    layout = estado.layout
    spec = layout.spec
    refs = estado.refs
    if not refs or not estado.movable:
        return False

    n_comp = len(refs)
    indice = {r: i for i, r in enumerate(refs)}

    pin_ini = (ctypes.c_int * n_comp)()
    pin_qtd = (ctypes.c_int * n_comp)()
    corpo = (ctypes.c_int * (n_comp * 4))()
    movel = (ctypes.c_ubyte * n_comp)()
    borda = (ctypes.c_ubyte * n_comp)()
    col = (ctypes.c_int * n_comp)()
    row = (ctypes.c_int * n_comp)()
    rot = (ctypes.c_int * n_comp)()

    dx, dy, pnet = [], [], []
    ids_rede = {}
    global_de = {}      # (ref, pino) -> indice global do pino

    for i, ref in enumerate(refs):
        fp = layout.footprints[ref]
        pl = layout.placements[ref]
        pin_ini[i] = len(dx)
        for pin, (ox, oy) in estado.offsets[ref]:
            global_de[(ref, pin)] = len(dx)
            dx.append(ox)
            dy.append(oy)
            nome = estado.net_of_pin.get((ref, pin))
            if nome is None:
                pnet.append(-1)
            else:
                pnet.append(ids_rede.setdefault(nome, len(ids_rede)))
        pin_qtd[i] = len(dx) - pin_ini[i]

        bx0, by0, bx1, by1 = fp.body_extent
        corpo[i * 4 + 0] = bx0
        corpo[i * 4 + 1] = by0
        corpo[i * 4 + 2] = bx1
        corpo[i * 4 + 3] = by1
        movel[i] = 0 if pl.locked else 1
        borda[i] = 1 if estado.is_edge.get(ref) else 0
        col[i], row[i], rot[i] = pl.col, pl.row, pl.rot % 360

    n_pinos = len(dx)
    arr_dx = (ctypes.c_int * n_pinos)(*dx)
    arr_dy = (ctypes.c_int * n_pinos)(*dy)
    arr_net = (ctypes.c_int * n_pinos)(*pnet)

    # redes: so as que tem pelo menos dois pinos posicionados
    net_ini, net_qtd, net_pino = [], [], []
    for _nome, nodes in estado.nets:
        alvo = [global_de[k] for k in nodes if k in global_de]
        if len(alvo) < 2:
            continue
        net_ini.append(len(net_pino))
        net_qtd.append(len(alvo))
        net_pino.extend(alvo)
    n_net = len(net_ini)

    par_a, par_b = [], []
    for a, b in estado.pares:
        if a in global_de and b in global_de:
            par_a.append(global_de[a])
            par_b.append(global_de[b])
    n_par = len(par_a)

    vazio = (ctypes.c_int * 1)()
    cfg = PbPlaceCfg(
        w_overlap_final=pesos["overlap_final"],
        w_overlap_inicial=pesos["overlap_inicial"],
        w_outside=pesos["outside"], w_corpo_fora=pesos["corpo_fora"],
        w_desacopla=pesos["desacopla"], edge_pull=estado.edge_pull,
        w_densidade=pesos["densidade"], regioes=pesos["regioes"],
        sem_saida_0=pesos["sem_saida"][0], sem_saida_1=pesos["sem_saida"][1],
        sem_saida_2=pesos["sem_saida"][2],
        folga_desacopla=pesos["folga_desacopla"],
        proibir_sobreposicao=1 if pesos.get("proibir_sobreposicao") else 0,
        passos=passos,
        semente=(semente * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1),
    )

    ok = lib.pb_place(
        ctypes.byref(cfg), spec.cols, spec.rows, spec.margin_holes,
        n_comp, pin_ini, pin_qtd, arr_dx, arr_dy, arr_net, corpo, movel, borda,
        n_net, (ctypes.c_int * max(1, n_net))(*net_ini),
        (ctypes.c_int * max(1, n_net))(*net_qtd),
        (ctypes.c_int * max(1, len(net_pino)))(*net_pino) if net_pino else vazio,
        n_par, (ctypes.c_int * max(1, n_par))(*par_a) if par_a else vazio,
        (ctypes.c_int * max(1, n_par))(*par_b) if par_b else vazio,
        n_pinos, col, row, rot,
    )
    if ok < 0:
        return False

    for i, ref in enumerate(refs):
        pl = layout.placements[ref]
        if pl.locked:
            continue        # trava e trava: o C nao mexe, mas conferimos aqui tambem
        pl.col, pl.row, pl.rot = col[i], row[i], rot[i] % 360
    return True
