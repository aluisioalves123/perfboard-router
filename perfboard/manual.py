"""Roteiro de montagem: o que fazer, em que ordem, um passo por vez.

O plano de bancada diz O QUE existe na placa. Este modulo diz em que ORDEM fazer,
que e a parte que economiza retrabalho.

A ordem NAO e "monte tudo e depois solde". Espaco e recurso que se gasta: cada peca
montada tira lugar para as trilhas de cima e para as vias, que sao justamente as
ligacoes que precisam de espaco para existir. Montando tudo primeiro, voce chega na
fiacao com a placa cheia e as ligacoes dificeis sem para onde ir.

Entao o roteiro cresce de dentro para fora:

* comeca pela peca mais CONECTADA - e nela que a fiacao briga por espaco, e num
  circuito com CI ela costuma estar no meio mesmo;
* a cada passo entra a peca mais proxima do que ja esta montado, preferindo quem
  compartilha rede com ele;
* logo apos cada peca, faz-se toda a ligacao que ela fecha com as anteriores -
  enquanto ainda ha espaco em volta;
* as peças de borda ficam por ultimo, onde as ligacoes sao mais faceis mesmo com a
  placa cheia.

Entre duas pecas igualmente candidatas, entra a mais baixa: voce vira a placa para
soldar e ela precisa assentar.

E acima de tudo isso manda uma regra de ferro: **cada furo e soldado uma vez so**.
Ponte de solda nao e uma operacao separada - e o estanho da propria junta puxado
ate a ilha vizinha. Mandar fazer a ponte e depois passar um fio pelo mesmo furo
obriga a reaquecer e limpar o furo: retrabalho puro, e o erro que este modulo
existe para nao cometer.
"""
from __future__ import annotations

import math


def _chave_ref(ref):
    """R2 antes de R10: o numero conta como numero, nao como texto."""
    letras = "".join(c for c in ref if not c.isdigit())
    digitos = "".join(c for c in ref if c.isdigit())
    return (letras, int(digitos) if digitos else 0)


def _centro_de(layout, ref):
    furos = list(layout.pin_holes(ref).values())
    if not furos:
        return None
    return (sum(c for c, _ in furos) / len(furos),
            sum(r for _, r in furos) / len(furos))


def _redes_de(plano, layout, netlist):
    """ref -> conjunto de redes que passam por ele."""
    do_ref = {}
    for net in netlist.nets:
        nome = net.name
        for ref, _pino in net.nodes:
            do_ref.setdefault(ref, set()).add(nome)
    return do_ref


def _ordem_do_meio_para_fora(layout, netlist):
    """Pecas na ordem de montagem: do miolo dificil para as bordas faceis.

    Comeca pela peca com mais ligacoes - e ali que a fiacao disputa espaco, e essa
    disputa se resolve melhor com a placa vazia. Depois entra sempre a mais proxima
    do que ja esta montado, preferindo quem compartilha rede: assim cada peca nova
    ja fecha ligacoes em vez de so ocupar lugar. Empate resolve pela mais baixa, que
    e a que deixa a placa assentar quando voce vira para soldar.
    """
    refs = [r for r in layout.placements
            if layout.footprints.get(r) and layout.footprints[r].pins]
    if not refs:
        return []

    centro_placa = ((layout.spec.cols - 1) / 2.0, (layout.spec.rows - 1) / 2.0)
    pos = {r: _centro_de(layout, r) for r in refs}
    redes = _redes_de(None, layout, netlist)
    altura = {r: getattr(layout.footprints[r], "altura", 4) for r in refs}

    def dist(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])

    # A primeira e a peca com MAIS ligacoes, nao a mais central por geometria: e
    # nela que a fiacao briga por espaco, e e essa briga que queremos resolver com
    # a placa ainda vazia. Num circuito com CI isso quase sempre da o CI, que
    # tambem costuma estar no meio. Empate resolve por centralidade e altura.
    restantes = set(refs)

    def dificuldade(r):
        return (-len(redes.get(r, set())),
                round(dist(pos[r], centro_placa), 3), altura[r], _chave_ref(r))

    ordem = [min(restantes, key=dificuldade)]
    restantes.discard(ordem[0])

    while restantes:
        montadas = set(ordem)
        redes_montadas = set()
        for r in montadas:
            redes_montadas |= redes.get(r, set())

        def nota(r):
            # 0 se compartilha rede com o que ja esta montado: essa fecha ligacao
            liga = 0 if (redes.get(r, set()) & redes_montadas) else 1
            perto = min(dist(pos[r], pos[m]) for m in montadas)
            return (liga, round(perto, 3), altura[r], _chave_ref(r))

        proxima = min(restantes, key=nota)
        ordem.append(proxima)
        restantes.discard(proxima)
    return ordem


def _dono_da_ligacao(elemento, pos_pino, ordem_de):
    """Em que passo esta ligacao pode ser feita.

    Uma ligacao so existe quando as duas pontas ja tem componente. Procuramos o
    componente mais proximo de cada ponta e devolvemos o mais TARDIO dos dois: e o
    momento em que aquela solda passa a fazer sentido.
    """
    tardio = -1
    for ponta in (tuple(elemento["de"]), tuple(elemento["ate"])):
        melhor, melhor_d = None, None
        for ref, furos in pos_pino.items():
            for furo in furos:
                d = abs(furo[0] - ponta[0]) + abs(furo[1] - ponta[1])
                if melhor_d is None or d < melhor_d:
                    melhor, melhor_d = ref, d
        if melhor is not None:
            tardio = max(tardio, ordem_de.get(melhor, 0))
    return tardio


def _furos_ocupados(de, ate):
    """Todos os furos que um fio ocupa, de ponta a ponta.

    O fio nao encosta so nas pontas: ele deita sobre cada furo do caminho, e num
    deles pode chegar um ramo. Quem for soldar aquele furo precisa do fio ja no
    lugar - por isso o caminho inteiro conta, nao so as extremidades.
    """
    (c0, r0), (c1, r1) = tuple(de), tuple(ate)
    dc, dr = c1 - c0, r1 - r0
    if dc and dr:               # jumper voa em diagonal: so as duas pontas contam
        return [(c0, r0), (c1, r1)]
    n = max(abs(dc), abs(dr))
    uc = (dc > 0) - (dc < 0)
    ur = (dr > 0) - (dr < 0)
    return [(c0 + uc * k, r0 + ur * k) for k in range(n + 1)]


def _sequencia_de_itens(layout, plano, rotas, ordem, ordem_de, pos_pino):
    """A ordem exata das acoes na bancada, uma lista so.

    Existe para responder a pergunta que decide tudo: *qual acao encosta neste furo
    por ultimo?* Raciocinar por peca nao bastava - dentro da mesma peca o pino e
    feito antes do fio, entao uma ponte ancorada no pino ainda saia antes do fio
    que cai no furo do lado. So a sequencia achatada da a resposta certa.

    Devolve `(tipo, carga, encosta, enfia)`, com `tipo` em "peca", "fio" e "jumper".
    `encosta` sao todos os furos que o item cobre; `enfia` so os que ele OCUPA - o
    pino e a ponta de fio entram no furo, o fio de passagem so deita por cima. A
    diferenca decide quando o furo pode ser soldado: estanho num furo ocupado nao
    atrapalha ninguem, mas estanho num furo que ainda vai receber um terminal
    entope o furo e obriga a desfazer a solda.
    """
    fios_de, jumpers_de, vias_de = {}, {}, {}
    for f in plano.get("fios", ()):
        fios_de.setdefault(_dono_da_ligacao(f, pos_pino, ordem_de), []).append(f)
    for rota in rotas:
        for seg in rota["segments"]:
            if seg["type"] != "jumper":
                continue
            falso = {"de": seg["from"], "ate": seg["to"]}
            i = _dono_da_ligacao(falso, pos_pino, ordem_de)
            jumpers_de.setdefault(i, []).append((rota["name"], seg))

    # A VIA e um objeto de verdade: um pedaco de fio atravessando o furo, soldado
    # dos dois lados. Quando um fio da rede ja passa pelo furo, ele mesmo e a via -
    # e por isso que o texto dele diz "atravessa para o outro lado". Mas quando os
    # dois lados sao so pontes de solda nao sobra fio nenhum, e a via nao existia em
    # passo nenhum do roteiro: o furo virava uma ilha nua para onde se mandava puxar
    # estanho sem dizer o que fazer com ela.
    pontas_de_fio = set()
    for f in plano.get("fios", ()):
        pontas_de_fio.add(tuple(f["de"]))
        pontas_de_fio.add(tuple(f["ate"]))
    for rota in rotas:
        for seg in rota["segments"]:
            if seg["type"] != "via":
                continue
            furo = tuple(seg["from"])
            if furo in pontas_de_fio:
                continue        # o proprio fio da rede ja atravessa o furo
            falso = {"de": furo, "ate": furo}
            i = _dono_da_ligacao(falso, pos_pino, ordem_de)
            vias_de.setdefault(i, {})[furo] = rota["name"]

    itens = []
    for i, ref in enumerate(ordem):
        pinos = [tuple(c) for c in layout.pin_holes(ref).values()]
        itens.append(("peca", (i, ref), pinos, pinos))
        # a via vem antes de tudo no grupo: nada mais pode ser soldado no furo dela
        # enquanto o toco de fio nao estiver no lugar
        for furo, net in sorted(vias_de.get(i, {}).items()):
            itens.append(("via", (i, furo, net), [furo], [furo]))
        for f in sorted(fios_de.get(i, ()), key=lambda f: -f["furos"]):
            pontas = [tuple(f["de"]), tuple(f["ate"])]
            itens.append(("fio", (i, f), _furos_ocupados(f["de"], f["ate"]), pontas))
        for nome, seg in sorted(jumpers_de.get(i, ()), key=lambda x: x[1]["length_mm"]):
            pontas = [tuple(seg["from"]), tuple(seg["to"])]
            itens.append(("jumper", (i, nome, seg),
                          _furos_ocupados(seg["from"], seg["to"]), pontas))
    return itens


def monta_roteiro(layout, plano, rotas, nome_do_furo, stats=None, netlist=None):
    """Lista de passos numerados, na ordem de executar na bancada."""
    passos = []

    def passo(titulo, detalhe="", grupo="", itens=None):
        passos.append({"n": len(passos) + 1, "grupo": grupo, "titulo": titulo,
                       "detalhe": detalhe, "itens": itens or []})

    ordem = _ordem_do_meio_para_fora(layout, netlist) if netlist else []
    ordem_de = {ref: i for i, ref in enumerate(ordem)}
    pos_pino = {ref: list(layout.pin_holes(ref).values()) for ref in ordem}

    if ordem:
        passo("Separe as peças e confira a placa",
              "%d peças. A placa é de %d x %d furos. Vamos montar do MEIO para as "
              "bordas: cada peça tira espaço das trilhas de cima e das vias, então "
              "as ligações difíceis se fazem primeiro, enquanto ainda há folga. E "
              "cada furo é soldado UMA vez só: a ponte de solda sai da própria "
              "junta, não é um passo separado."
              % (len(ordem), layout.spec.cols, layout.spec.rows),
              grupo="Antes de começar")

    itens = _sequencia_de_itens(layout, plano, rotas, ordem, ordem_de, pos_pino)

    # Cada item vira exatamente um passo, na ordem, logo depois da abertura. Isso
    # deixa o numero do passo conhecido antes de escrever qualquer texto - e por
    # isso um item consegue apontar para outro la na frente.
    base = 2 if ordem else 1
    numero_do_item = {k: base + k for k in range(len(itens))}

    # O que ENTOPE o furo e o que entra nele: o terminal do componente ou a ponta
    # do fio. Fio de passagem so deita sobre a ilha. Por isso o furo pertence a
    # quem o ocupa - e so na falta de ocupante ao ultimo que encosta.
    enfia, encosta = {}, {}
    for k, (_tipo, _carga, cobre, ocupa) in enumerate(itens):
        for furo in cobre:
            encosta[furo] = max(encosta.get(furo, -1), k)
        for furo in ocupa:
            enfia[furo] = max(enfia.get(furo, -1), k)

    def dono(furo):
        return enfia.get(furo, encosta.get(furo, -1))

    # item de cada peca, para quem nao tem furo conhecido cair junto dela
    item_da_peca = {}
    for k, (tipo, carga, _cobre, _ocupa) in enumerate(itens):
        if tipo == "peca":
            item_da_peca[carga[0]] = k

    # ------------------------------------------------------------------
    # Pontes ligadas entre si sao UM CORDAO DE SOLDA, feito de uma vez.
    #
    # Ancorar cada ponte olhando so os dois furos dela nao bastava. Em F18-G18-H18
    # pela face de cima, a ponte H18+G18 caia no passo da via e a F18+G18 no passo
    # do fio: o guia mandava soldar G18 duas vezes, e a segunda so depois que o fio
    # chegasse. Na bancada isso e reaquecer o que ja estava pronto.
    #
    # Entao pontes da MESMA face que compartilham furo viram um bloco, e o bloco
    # inteiro espera o ultimo furo dele ficar pronto. Faces diferentes sao blocos
    # diferentes: sao duas ilhas e duas soldas, uma de cada lado da placa.
    # ------------------------------------------------------------------
    pai = {}

    def raiz(x):
        pai.setdefault(x, x)
        while pai[x] != x:
            pai[x] = pai[pai[x]]
            x = pai[x]
        return x

    def junta(x, y):
        rx, ry = raiz(x), raiz(y)
        if rx != ry:
            pai[rx] = ry

    pontes = list(plano.get("pontes", ()))
    for b in pontes:
        face = b.get("face", "solda")
        junta((face, tuple(b["de"])), (face, tuple(b["ate"])))

    blocos = {}
    for b in pontes:
        face = b.get("face", "solda")
        blocos.setdefault(raiz((face, tuple(b["de"]))), []).append(b)

    pontes_do_item = {}
    for bloco in blocos.values():
        furos = set()
        for b in bloco:
            furos.add(tuple(b["de"]))
            furos.add(tuple(b["ate"]))
        k = max(dono(f) for f in furos)
        if k < 0:
            # Nenhum furo do bloco tem pino, ponta de fio ou via: e uma corrente de
            # pontes so por ilhas nuas. Descartar era o que fazia 8 das 121 pontes
            # desta placa nao aparecerem em passo nenhum - quem montasse pelo guia
            # deixava a ligacao aberta. Na duvida cai no dono geografico.
            peca = _dono_da_ligacao(bloco[0], pos_pino, ordem_de)
            k = item_da_peca.get(peca, 0)
        lado = ("por cima, lado dos componentes"
                if bloco[0].get("face") == "componentes" else "por baixo, lado da solda")
        pontes_do_item.setdefault(k, []).append({
            "lado": lado,
            "net": bloco[0]["net"],
            "furos": furos,
            "pares": sorted((b["de_label"], b["ate_label"]) for b in bloco),
        })

    def avisos(k, cobre, ocupa):
        """O que dizer sobre os furos deste item: solda com ponte, ou nao solde.

        O aviso de nao soldar so sai quando o furo ainda vai receber um TERMINAL ou
        uma PONTA de fio depois. Estanho nesse furo agora significa furo entupido e
        solda a desfazer - foi exatamente o erro que este roteiro cometia.
        """
        linhas = []
        for bloco in sorted(pontes_do_item.get(k, ()), key=lambda x: x["pares"]):
            pares = bloco["pares"]
            se_meus = [f for f in bloco["furos"] if f in set(ocupa)]
            if len(pares) == 1 and se_meus:
                onde = nome_do_furo(*se_meus[0])
                a, z = pares[0]
                alvo = z if onde == a else a
                linhas.append("na MESMA solda de %s, puxe a ponte até %s %s (rede %s)"
                              % (onde, alvo, bloco["lado"], bloco["net"]))
            else:
                linhas.append("de uma vez só, %s: %s (rede %s)"
                              % (bloco["lado"],
                                 ", ".join("%s+%s" % par for par in pares),
                                 bloco["net"]))
        ocupa = set(ocupa)
        for furo in sorted(set(cobre)):
            depois = enfia.get(furo, -1)
            if depois <= k:
                continue
            porque = ("o mesmo fio atravessa para o outro lado" if furo in ocupa
                      else "ainda entra fio nele")
            linhas.append("NÃO solde %s agora — %s, no passo %d"
                          % (nome_do_furo(*furo), porque, numero_do_item[depois]))
        return linhas

    for k, (tipo, carga, cobre, ocupa) in enumerate(itens):
        i = carga[0]
        grupo = "Peça %d de %d — %s" % (i + 1, len(ordem), ordem[i])

        if tipo == "peca":
            ref = carga[1]
            fp = layout.footprints[ref]
            pl = layout.placements[ref]
            lista = ["pino %s no furo %s" % (pino, nome_do_furo(*cel))
                     for pino, cel in sorted(layout.pin_holes(ref).items(),
                                             key=lambda kv: _chave_ref(kv[0]))]
            detalhe = fp.label
            if pl.rot:
                detalhe += " · girado %d°" % pl.rot
            if getattr(fp, "pin_note", ""):
                detalhe += " · " + fp.pin_note
            passo("Coloque %s e solde os terminais" % ref, detalhe,
                  grupo=grupo, itens=lista + avisos(k, cobre, ocupa))

        elif tipo == "via":
            _i, furo, net = carga
            passo("Via no furo %s — rede %s" % (nome_do_furo(*furo), net),
                  "A rede troca de face aqui e não há fio passando. Enfie uma sobra "
                  "de terminal no furo e corte rente dos dois lados: é ela que liga "
                  "a face de cima à de baixo. A solda de cada lado vem junto com o "
                  "cordão daquele lado — neste passo ou mais adiante.",
                  grupo=grupo, itens=avisos(k, cobre, ocupa))

        elif tipo == "fio":
            f = carga[1]
            de = f["de_label"] + (" (vem do outro lado)" if f.get("de_atravessa") else "")
            ate = f["ate_label"] + (" (atravessa para o outro lado)"
                                    if f.get("ate_atravessa") else "")
            passo("Fio de %.1f mm — rede %s" % (f["mm"], f["net"]),
                  "Corte reto, %d furos, de %s a %s%s."
                  % (f["furos"], de, ate,
                     " — lado dos componentes" if f["face"] == "componentes" else ""),
                  grupo=grupo, itens=avisos(k, cobre, ocupa))

        else:
            _i, nome, seg = carga
            # O jumper VOA por cima, mas e soldado numa face so - e ela pode ser a
            # de baixo. Sem dizer qual, metade deles sai do lado errado.
            lado = ("do lado dos componentes" if seg.get("layer") == 1
                    else "do lado da solda")
            passo("Jumper de %.1f mm — rede %s" % (seg["length_mm"], nome),
                  "Fio isolado sobrevoando, de %s a %s, soldado %s nas duas pontas. "
                  "Corte uns 8 mm a mais."
                  % (nome_do_furo(*seg["from"]), nome_do_furo(*seg["to"]), lado),
                  grupo=grupo, itens=avisos(k, cobre, ocupa))

    soltos = (stats or {}).get("orphan_pins") or []
    if soltos:
        passo("Ligue à mão os pinos que ficaram sem rota",
              "O programa não conseguiu fechar estes:", grupo="Para terminar",
              itens=["%s.%s no furo %s (rede %s)"
                     % (o["ref"], o["pin"], o["label"], o["net"]) for o in soltos])
    passo("Conferência final",
          "Meça continuidade rede a rede e olhe o lado da solda contra a luz "
          "procurando ponte que não devia existir.", grupo="Para terminar")
    return passos
