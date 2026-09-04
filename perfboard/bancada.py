"""Traduz o roteamento para o vocabulario de quem monta a placa.

O desenho mostra caminhos; a bancada tem tres coisas, e so tres:

* **Ponte de solda** - dois furos VIZINHOS ligados so com estanho. Nao precisa de
  fio: encosta um no outro e pronto. E o passo mais barato que existe.

* **Fio reto** - um pedaco de fio nu, cortado no tamanho e soldado nas duas pontas.
  Reto porque dobrar fio fino em angulo certo, no lugar certo, e briga perdida.

* **Junta** - o furo onde as coisas se encontram, ligado a estanho.

A regra que faz tudo isso fechar: **um furo recebe a ponta de UM fio, nao de dois**.
Nao da para enfiar duas pontas no mesmo furo e soldar limpo. Entao uma quina nao e
a fusao de dois fios: o primeiro fio vai ate a quina, o segundo comeca no furo
VIZINHO, e uma ponte de solda liga os dois. Quem monta faz exatamente isso; quem
desenha e que costumava inventar a fusao.
"""
from __future__ import annotations

PASSO_MM = 2.54

# O estanho alcanca o furo do meio e os vizinhos ortogonais. Mais que isso nao e
# junta, e bolha - e a montagem vira ginastica.
MAX_VIZINHOS_NA_JUNTA = 4

TIPOS_DE_TRILHA = ("trace", "trace_top")
FACES = ((0, "solda"), (1, "componentes"))


def _vao(a, b):
    """Distancia em passos entre dois furos alinhados."""
    return max(abs(b[0] - a[0]), abs(b[1] - a[1]))


def _passo_unitario(de, para):
    """Direcao de `de` para `para`, um furo por vez."""
    dx = (para[0] > de[0]) - (para[0] < de[0])
    dy = (para[1] > de[1]) - (para[1] < de[1])
    return dx, dy


def _anda(furo, direcao, n=1):
    return (furo[0] + direcao[0] * n, furo[1] + direcao[1] * n)


def _formato(direcoes):
    """Que desenho o estanho faz no furo: ponta, linha, L, T ou cruz."""
    n = len(direcoes)
    if n >= 4:
        return "cruz"
    if n == 3:
        return "T"
    if n == 2:
        (ax, ay), (bx, by) = sorted(direcoes)
        return "linha" if (ax == -bx and ay == -by) else "L"
    return "ponta"


def _funde_colineares(trechos):
    """Junta pedacos em linha reta num fio so.

    O roteador quebra o caminho onde a rede se ramifica, entao uma reta que passa
    por uma derivacao chega aqui como dois pedacos. Montar assim seria cortar dois
    fios e emendar no meio - ninguem faz isso. Numa junta em T o que se faz e UM
    fio comprido passando reto, e o outro encontrando ele ali.

    Fundir tambem tira pontas de fio de circulacao: menos ponta, menos disputa por
    furo, menos ponte para separar.
    """
    trechos = [tuple(t) for t in trechos]
    mudou = True
    while mudou:
        mudou = False
        pontas = {}
        for i, (a, b) in enumerate(trechos):
            pontas.setdefault(a, []).append(i)
            pontas.setdefault(b, []).append(i)

        for furo, indices in pontas.items():
            if len(indices) < 2:
                continue
            # Procura um PAR colinear entre os que chegam aqui. Exigir que so haja
            # dois seria justamente perder a derivacao em T - que e onde fundir mais
            # importa: la o certo e um fio passando reto e o outro encontrando ele.
            par = None
            for pos_i in range(len(indices)):
                for pos_j in range(pos_i + 1, len(indices)):
                    i, j = indices[pos_i], indices[pos_j]
                    if i == j:
                        continue
                    (a1, b1), (a2, b2) = trechos[i], trechos[j]
                    fora1 = b1 if a1 == furo else a1
                    fora2 = b2 if a2 == furo else a2
                    # mesmo eixo, sentidos opostos: os dois formam uma reta so
                    if _passo_unitario(furo, fora1) == _passo_unitario(fora2, furo):
                        par = (i, j, fora2, fora1)
                        break
                if par:
                    break
            if not par:
                continue
            i, j, ini, fim = par
            trechos = [t for k, t in enumerate(trechos) if k not in (i, j)]
            trechos.append((ini, fim))
            mudou = True
            break
    return trechos


def _separa_pontas(trechos, pinos=(), atravessa=()):
    """Resolve tudo o que nao existe na bancada, um caso por vez.

    Duas regras, e as duas produzem PONTE DE SOLDA onde havia contato direto:

    1. **Duas pontas de fio no mesmo furo.** Nao cabem. O mais comprido fica com o
       furo, o outro recua um passo e o passo cedido vira ponte.

    2. **Ponta de fio num furo de terminal.** Nada conecta ao pino a nao ser
       estanho: o fio para um furo antes e a ponte faz a ligacao.

    Fio que PASSA por um furo onde outro termina esta liberado - e a junta em T que
    se faz de verdade. Ponta que cai num furo de troca de face tambem fica: ali o
    fio nao termina, atravessa.

    O laco repete ate estabilizar porque cada correcao pode criar a proxima: recuar
    uma ponta pode joga-la em cima de outra, ou em cima de um pino. Cada rodada
    encurta um fio, entao termina.
    """
    pinos = set(pinos)
    atravessa = set(atravessa)
    pontes = [tuple(t) for t in trechos if _vao(*t) == 1]
    fios = [tuple(t) for t in trechos if _vao(*t) >= 2]

    def cede(i, furo):
        """Recua uma ponta um passo; o passo cedido vira ponte de solda."""
        a, b = fios[i]
        direcao = _passo_unitario(a, b) if furo == a else _passo_unitario(b, a)
        vizinho = _anda(furo, direcao)
        pontes.append((furo, vizinho))
        resto = (vizinho, b) if furo == a else (a, vizinho)
        vao = _vao(*resto)
        if vao >= 2:
            fios[i] = resto
        else:
            if vao == 1:
                pontes.append(resto)
            fios.pop(i)

    for _ in range(400):        # limite de seguranca; na pratica sao poucas rodadas
        # 1) ponta encostando em terminal
        alvo = None
        for i, (a, b) in enumerate(fios):
            for furo in (a, b):
                if furo in pinos and furo not in atravessa:
                    alvo = (i, furo)
                    break
            if alvo:
                break
        if alvo:
            cede(*alvo)
            continue

        # 2) ponta caindo em furo que JA tem outro fio - terminando ou so passando.
        #    Nao da para soldar a ponta de um fio em cima de outro fio deitado na
        #    ilha: e o mesmo caso do terminal. Para um furo antes, ponte liga.
        ocupado = {}
        for i, (a, b) in enumerate(fios):
            for c in _celulas(a, b):
                ocupado.setdefault(c, set()).add(i)

        conflito = None
        for i, (a, b) in enumerate(fios):
            for furo in (a, b):
                if furo in atravessa:
                    continue        # ali o fio nao termina: passa para o outro lado
                outros = ocupado.get(furo, set()) - {i}
                if outros:
                    # quem cede e o mais curto; fio longo e caro de refazer
                    candidatos = sorted(outros | {i}, key=lambda k: -_vao(*fios[k]))
                    if candidatos[0] != i or _vao(a, b) <= min(_vao(*fios[k])
                                                               for k in outros):
                        conflito = (i, furo)
                    else:
                        conflito = (candidatos[-1], furo)
                    break
            if conflito:
                break
        if conflito is None:
            break
        i, furo = conflito
        # so faz sentido recuar quem realmente TERMINA neste furo
        if furo not in fios[i]:
            for k in sorted(ocupado.get(furo, set()), key=lambda k: _vao(*fios[k])):
                if furo in fios[k]:
                    i = k
                    break
            else:
                break
        cede(i, furo)

    return fios, pontes


def _arruma(brutos, pinos=(), atravessa=(), voltas=6):
    """Funde e separa ate estabilizar.

    As duas operacoes se alimentam: separar pontas CRIA pontes, e duas pontes
    coladas em linha sao tres ilhas soldadas em fila - o que ninguem faz, porque
    vira bolha. Fundir de volta transforma a fila num fio, o que por sua vez pode
    criar um conflito de pontas novo.

    Uma passada de cada, como estava, deixava 15 correntes dessas na placa do
    usuario. Alternar ate parar de mudar resolve, e converge porque cada fusao
    reduz o numero de trechos.
    """
    trechos = [tuple(t) for t in brutos]
    fios, pontes = [], []
    for _ in range(voltas):
        fios, pontes = _separa_pontas(_funde_colineares(trechos), pinos, atravessa)
        antes = trechos
        trechos = fios + pontes
        if sorted(trechos) == sorted(antes):
            break
    return fios, pontes


def _celulas(a, b):
    """Todos os furos cobertos por um trecho reto, pontas incluidas."""
    d = _passo_unitario(a, b)
    return [_anda(a, d, k) for k in range(_vao(a, b) + 1)]


def _travessias(rota):
    """Furos onde esta rede troca de face - o fio passa pelo buraco e continua."""
    return {tuple(s["from"]) for s in rota["segments"] if s["type"] in ("via", "lead")}


def plano_de_montagem(rotas, nome_do_furo, pinos=None):
    """Fios, pontes e juntas de todas as redes, no vocabulario da bancada.

    `nome_do_furo(col, row)` da o nome que a placa de quem monta usa - o guia tem
    de citar o mesmo furo que o desenho.

    `pinos` mapeia furo -> (ref, pino). Furo de pino tocado pela fiacao tambem e
    ponto de solda: e ali que o componente entra na rede. Sem essa lista, o guia
    marcava a quina e esquecia o terminal.
    """
    pinos = pinos or {}
    fios, pontes, juntas, avisos = [], [], [], []

    for rota in rotas:
        for face, rotulo in FACES:
            brutos = [(tuple(s["from"]), tuple(s["to"])) for s in rota["segments"]
                      if s.get("layer") == face and s["type"] in TIPOS_DE_TRILHA]
            if not brutos:
                continue

            atravessa = _travessias(rota)
            meus_fios, minhas_pontes = _arruma(brutos, pinos, atravessa)

            def item(a, b):
                vao = _vao(a, b)
                return {"net": rota["name"], "face": rotulo,
                        "ok": bool(rota.get("ok", True)),
                        "de": list(a), "ate": list(b),
                        "de_label": nome_do_furo(*a), "ate_label": nome_do_furo(*b),
                        "furos": vao + 1, "mm": round(vao * PASSO_MM, 1),
                        # Ponta que cai num furo de troca de face nao TERMINA ali:
                        # o fio passa pelo buraco e segue do outro lado. Marcar isso
                        # evita desenhar duas pontas de fio no mesmo furo - que era o
                        # que acontecia quando a via contava como objeto a parte.
                        "de_atravessa": a in atravessa,
                        "ate_atravessa": b in atravessa}

            fios.extend(item(a, b) for a, b in meus_fios)
            pontes.extend(item(a, b) for a, b in minhas_pontes)

            # Junta: furo onde mais de uma coisa chega. Depois da separacao, e
            # sempre uma ponta de fio mais estanho - nunca dois fios fundidos.
            encontros = {}
            for a, b in meus_fios + minhas_pontes:
                encontros.setdefault(a, set()).add(_passo_unitario(a, b))
                encontros.setdefault(b, set()).add(_passo_unitario(b, a))

            # Todo furo de PINO por onde a fiacao passa e ponto de solda, mesmo
            # que nada se encontre ali: e a ligacao do componente com a rede.
            for a, b in meus_fios + minhas_pontes:
                for furo in _celulas(a, b):
                    if furo in pinos:
                        encontros.setdefault(furo, set())

            for furo, direcoes in sorted(encontros.items()):
                if len(direcoes) < 2 and furo not in pinos:
                    continue
                vizinhos = [_anda(furo, d) for d in sorted(direcoes)]
                ref_pino = pinos.get(furo)
                juntas.append({
                    "net": rota["name"], "face": rotulo,
                    "furo": list(furo), "furo_label": nome_do_furo(*furo),
                    "toca": [list(v) for v in vizinhos],
                    "toca_labels": [nome_do_furo(*v) for v in vizinhos],
                    "formato": "pino" if ref_pino else _formato(direcoes),
                    "pino": ("%s.%s" % ref_pino) if ref_pino else None,
                })
                if len(direcoes) > MAX_VIZINHOS_NA_JUNTA:
                    avisos.append(
                        "junta em %s da rede %s precisaria alcancar %d vizinhos; o "
                        "estanho so faz cruz (4)"
                        % (nome_do_furo(*furo), rota["name"], len(direcoes)))

    fios.sort(key=lambda f: (-f["furos"], f["net"]))
    pontes.sort(key=lambda p: (p["net"], p["de"]))
    juntas.sort(key=lambda j: (j["net"], j["furo"]))

    return {
        "fios": fios,
        "pontes": pontes,
        "juntas": juntas,
        "avisos": avisos,
        "totais": {
            "fios": len(fios),
            "travessias": sum(1 for f in fios
                              if f.get("de_atravessa") or f.get("ate_atravessa")),
            "fio_mm": round(sum(f["mm"] for f in fios), 1),
            "fio_maior_furos": max([f["furos"] for f in fios], default=0),
            "pontes": len(pontes),
            "juntas": len(juntas),
            "pino": sum(1 for j in juntas if j["formato"] == "pino"),
            "linha": sum(1 for j in juntas if j["formato"] == "linha"),
            "L": sum(1 for j in juntas if j["formato"] == "L"),
            "T": sum(1 for j in juntas if j["formato"] == "T"),
            "cruz": sum(1 for j in juntas if j["formato"] == "cruz"),
        },
    }


def confere(plano, pinos=None, rotas=None):
    """Confere o plano contra as regras da bancada e devolve o que estiver errado.

    Existe porque achar isso a olho, no desenho, e trabalho de quem monta - e ele
    ja tem trabalho demais. Toda regra aqui saiu de uma coisa que nao deu para
    soldar de verdade.
    """
    pinos = pinos or {}
    problemas = []
    atravessa = set()
    for rota in (rotas or ()):
        atravessa |= _travessias(rota)

    # 1) uma ponta de fio por furo
    pontas = {}
    for f in plano["fios"]:
        for ponta, cruza in ((tuple(f["de"]), f.get("de_atravessa")),
                             (tuple(f["ate"]), f.get("ate_atravessa"))):
            if cruza:
                continue
            chave = (f["face"], ponta)
            pontas.setdefault(chave, []).append(f["net"])
    for (face, furo), redes in sorted(pontas.items()):
        if len(redes) > 1:
            problemas.append("furo %s (%s) tem %d pontas de fio: %s"
                             % (furo, face, len(redes), ", ".join(redes)))

    # 1b) ponta de fio caindo em furo que ja tem outro fio, passando ou terminando
    for face in ("solda", "componentes"):
        daqui = [f for f in plano["fios"] if f["face"] == face]
        ocupa = {}
        for i, f in enumerate(daqui):
            for c in _celulas(tuple(f["de"]), tuple(f["ate"])):
                ocupa.setdefault(c, set()).add(i)
        for i, f in enumerate(daqui):
            for ponta, cruza, rot in ((tuple(f["de"]), f.get("de_atravessa"), f["de_label"]),
                                      (tuple(f["ate"]), f.get("ate_atravessa"), f["ate_label"])):
                if cruza:
                    continue
                if ocupa.get(ponta, set()) - {i}:
                    problemas.append(
                        "fio da rede %s termina em %s, onde ja passa outro fio: "
                        "deveria parar um furo antes e ligar com solda" % (f["net"], rot))

    # 2) fio nao termina em terminal
    for f in plano["fios"]:
        for ponta, cruza, rot in ((tuple(f["de"]), f.get("de_atravessa"), f["de_label"]),
                                  (tuple(f["ate"]), f.get("ate_atravessa"), f["ate_label"])):
            if not cruza and ponta in pinos:
                problemas.append("fio da rede %s termina no terminal %s (%s.%s): "
                                 "deveria parar um furo antes"
                                 % (f["net"], rot, *pinos[ponta]))

    # 3) ponte so entre vizinhos
    for b in plano["pontes"]:
        if _vao(tuple(b["de"]), tuple(b["ate"])) != 1:
            problemas.append("ponte de solda de %s a %s nao e entre furos vizinhos"
                             % (b["de_label"], b["ate_label"]))

    # 4) junta nao passa de uma cruz
    for j in plano["juntas"]:
        if len(j["toca"]) > MAX_VIZINHOS_NA_JUNTA:
            problemas.append("junta em %s alcancaria %d furos; o estanho so faz cruz"
                             % (j["furo_label"], len(j["toca"])))

    # 5) todo pino tocado pela fiacao tem solda marcada
    marcados = {tuple(j["furo"]) for j in plano["juntas"] if j["formato"] == "pino"}
    for x in plano["fios"] + plano["pontes"]:
        for c in _celulas(tuple(x["de"]), tuple(x["ate"])):
            if c in pinos and c not in marcados and c not in atravessa:
                problemas.append("fiacao encosta no terminal %s.%s sem solda marcada"
                                 % pinos[c])
    return sorted(set(problemas))
