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

E acima de tudo isso manda uma regra de ferro: **cada ilha e soldada uma vez so**.
Ponte de solda nao e uma operacao separada - e o estanho da propria junta puxado
ate a ilha vizinha, e pontes ligadas entre si sao um cordao unico, feito de uma
vez. Mandar soldar duas vezes o mesmo cobre e retrabalho puro.

ILHA, nao furo: o furo tem duas, uma de cada lado da placa, e sao cobres
separados. Um fio deitado em cima nao atrapalha nada embaixo. O que atravessa a
placa - terminal, via, fio de travessia - e que enche o furo e obriga a esperar.
Confundir as duas coisas gera proibicao inventada, e proibicao inventada custa a
confianca no manual inteiro.
"""
from __future__ import annotations

import math


CIMA, BAIXO = 1, 0


def _face(rotulo):
    """A face onde a solda e feita, como numero."""
    return CIMA if rotulo == "componentes" else BAIXO


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

    Devolve `(tipo, carga, ilhas, enche)`, com `tipo` em "peca", "via", "fio" e
    "jumper".

    A distincao que importa e entre ILHA e FURO. O furo tem duas ilhas, uma de cada
    lado da placa, e elas sao cobres separados: um fio deitado em cima reivindica a
    ilha de cima e deixa a de baixo livre. So ENCHE o furo o que atravessa a placa -
    o terminal do componente, a via, e o fio que passa pelo buraco para continuar do
    outro lado. Estanho num furo que ainda vai receber um desses e furo entupido e
    solda a desfazer; estanho na ilha oposta nao atrapalha nada.

    `ilhas` e a lista de `(furo, face)` que o item reivindica; `enche`, os furos que
    ele atravessa.
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
        furos = [tuple(c) for c in layout.pin_holes(ref).values()]
        # O terminal atravessa a placa, entao enche o furo. A ilha de cima so conta
        # se o corpo nao estiver por cima dela: sob capacitor ou CI a ceramica tampa
        # o furo e so a ilha de baixo e soldavel.
        ilhas = [(f, BAIXO) for f in furos]
        fp = layout.footprints.get(ref)
        if fp is not None and not getattr(fp, "estorva", True):
            ilhas += [(f, CIMA) for f in furos]
        itens.append(("peca", (i, ref), ilhas, furos))

        # a via vem antes de tudo no grupo: nada mais pode ser soldado no furo dela
        # enquanto o toco de fio nao estiver no lugar
        for furo, net in sorted(vias_de.get(i, {}).items()):
            itens.append(("via", (i, furo, net),
                          [(furo, BAIXO), (furo, CIMA)], [furo]))

        for f in sorted(fios_de.get(i, ()), key=lambda f: -f["furos"]):
            face = _face(f.get("face"))
            ilhas = [(h, face) for h in _furos_ocupados(f["de"], f["ate"])]
            # so a ponta marcada como travessia passa pelo buraco; a outra fica
            # deitada na ilha e nao atrapalha o outro lado da placa
            enche = []
            if f.get("de_atravessa"):
                enche.append(tuple(f["de"]))
            if f.get("ate_atravessa"):
                enche.append(tuple(f["ate"]))
            itens.append(("fio", (i, f), ilhas, enche))

        for nome, seg in sorted(jumpers_de.get(i, ()), key=lambda x: x[1]["length_mm"]):
            # o jumper e isolado e passa pelo buraco para ser soldado do outro lado:
            # as duas pontas dele enchem o furo
            face = CIMA if seg.get("layer") == 1 else BAIXO
            pontas = [tuple(seg["from"]), tuple(seg["to"])]
            itens.append(("jumper", (i, nome, seg),
                          [(h, face) for h in pontas], pontas))
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
              "cada ilha de cobre é soldada UMA vez só: a ponte sai da própria "
              "junta, não é um passo separado. As duas faces de um furo são ilhas "
              "diferentes — soldar de um lado não atrapalha o outro."
              % (len(ordem), layout.spec.cols, layout.spec.rows),
              grupo="Antes de começar")

    itens = _sequencia_de_itens(layout, plano, rotas, ordem, ordem_de, pos_pino)

    # Cada item vira exatamente um passo, na ordem, logo depois da abertura. Isso
    # deixa o numero do passo conhecido antes de escrever qualquer texto - e por
    # isso um item consegue apontar para outro la na frente.
    base = 2 if ordem else 1
    numero_do_item = {k: base + k for k in range(len(itens))}

    # Duas contas separadas, porque sao duas perguntas diferentes:
    #
    # `enche` responde "quando esse FURO fica atravessado" - e o que impede soldar
    # antes, porque estanho no furo entope a passagem do terminal ou do fio.
    #
    # `na_ilha` responde "quando esse cobre fica pronto" - e o que decide quando a
    # ponte pode ser feita. Vale por ilha: o que acontece do outro lado da placa
    # nao muda nada deste lado.
    enche, na_ilha = {}, {}
    for k, (_tipo, _carga, ilhas, furos) in enumerate(itens):
        for chave in ilhas:
            na_ilha[chave] = max(na_ilha.get(chave, -1), k)
        for furo in furos:
            enche[furo] = max(enche.get(furo, -1), k)

    def dono(furo, face):
        return max(enche.get(furo, -1), na_ilha.get((furo, face), -1))

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
        face = _face(bloco[0].get("face"))
        k = max(dono(f, face) for f in furos)
        if k < 0:
            # Nenhum furo do bloco tem pino, ponta de fio ou via: e uma corrente de
            # pontes so por ilhas nuas. Descartar era o que fazia 8 das 121 pontes
            # desta placa nao aparecerem em passo nenhum - quem montasse pelo guia
            # deixava a ligacao aberta. Na duvida cai no dono geografico.
            peca = _dono_da_ligacao(bloco[0], pos_pino, ordem_de)
            k = item_da_peca.get(peca, 0)
        lado = ("por cima, lado dos componentes" if face == CIMA
                else "por baixo, lado da solda")
        pontes_do_item.setdefault(k, []).append({
            "lado": lado,
            "face": face,
            "net": bloco[0]["net"],
            "furos": furos,
            "pares": sorted((b["de_label"], b["ate_label"]) for b in bloco),
        })

    def avisos(k, ilhas, furos):
        """O que dizer sobre os furos deste item: solda com ponte, ou nao solde.

        O aviso de nao soldar so sai quando alguma coisa ainda vai ATRAVESSAR aquele
        furo - terminal, via ou fio de travessia. Estanho ali agora entope a
        passagem e obriga a desfazer a solda.

        Nao sai por causa de fio na outra face: as duas ilhas do furo sao cobres
        separados, e soldar de um lado nao atrapalha o outro. Era dai que vinham os
        avisos falsos.
        """
        minhas = set(ilhas)
        linhas = []
        for bloco in sorted(pontes_do_item.get(k, ()), key=lambda x: x["pares"]):
            pares = bloco["pares"]
            se_meus = [f for f in bloco["furos"] if (f, bloco["face"]) in minhas]
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
        meus_furos = set(furos)
        for furo in sorted({f for f, _face in ilhas} | meus_furos):
            depois = enche.get(furo, -1)
            if depois <= k:
                continue
            porque = ("o mesmo fio atravessa para o outro lado" if furo in meus_furos
                      else "ainda vai passar coisa por dentro desse furo")
            linhas.append("NÃO solde %s agora — %s, no passo %d"
                          % (nome_do_furo(*furo), porque, numero_do_item[depois]))
        return linhas

    for k, (tipo, carga, ilhas, furos) in enumerate(itens):
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
                  grupo=grupo, itens=lista + avisos(k, ilhas, furos))

        elif tipo == "via":
            _i, furo, net = carga
            passo("Via no furo %s — rede %s" % (nome_do_furo(*furo), net),
                  "A rede troca de face aqui e não há fio passando. Enfie uma sobra "
                  "de terminal no furo e corte rente dos dois lados: é ela que liga "
                  "a face de cima à de baixo. A solda de cada lado vem junto com o "
                  "cordão daquele lado — neste passo ou mais adiante.",
                  grupo=grupo, itens=avisos(k, ilhas, furos))

        elif tipo == "fio":
            f = carga[1]
            de = f["de_label"] + (" (vem do outro lado)" if f.get("de_atravessa") else "")
            ate = f["ate_label"] + (" (atravessa para o outro lado)"
                                    if f.get("ate_atravessa") else "")
            passo("Fio de %.1f mm — rede %s" % (f["mm"], f["net"]),
                  "Corte reto, %d furos, de %s a %s%s."
                  % (f["furos"], de, ate,
                     " — lado dos componentes" if f["face"] == "componentes" else ""),
                  grupo=grupo, itens=avisos(k, ilhas, furos))

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
                  grupo=grupo, itens=avisos(k, ilhas, furos))

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
